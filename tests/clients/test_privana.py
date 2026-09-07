from dataclasses import dataclass, replace
from unittest.mock import AsyncMock, patch

import pytest
from privana import PrivanaClient

from src.clients.privana import (
    _siwe_login_as_lp,
    get_authenticated_privana_client,
    get_privana_client,
    reset_privana_client,
)
from src.core.config import load_settings

LP_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
LP_ADDRESS = "0xd8991364507FAfC256EafF950d28618735753476"


@dataclass
class _SiweNonce:
    address: str
    nonce: str
    expires_in: int = 300


@dataclass
class _SiweLogin:
    siwe_token: str
    jwt_access_token: str
    jwt_refresh_token: str = ""
    address: str = LP_ADDRESS
    jwt_expires_in: int = 3600
    jwt_refresh_expires_in: int = 86400


def _settings():
    return replace(
        load_settings(),
        privana_api_base_url="https://accounting.example",
        liquidity_provider_secret_key=LP_PRIVATE_KEY,
        liquidity_provider_address=LP_ADDRESS,
        accounting_chain_id=23295,
    )


def test_get_privana_client_returns_singleton():
    reset_privana_client()
    settings = replace(load_settings(), privana_api_base_url="https://example.test")

    with patch("src.clients.privana.load_settings", return_value=settings):
        client_a = get_privana_client()
        client_b = get_privana_client()

    assert isinstance(client_a, PrivanaClient)
    assert client_a is client_b
    reset_privana_client()


def test_get_privana_client_uses_configured_base_url():
    reset_privana_client()
    settings = replace(load_settings(), privana_api_base_url="https://accounting.example/")

    with patch("src.clients.privana.load_settings", return_value=settings):
        client = get_privana_client()

    assert client._http._client.base_url == "https://accounting.example"
    reset_privana_client()


def test_reset_privana_client_clears_singleton():
    reset_privana_client()
    settings_one = replace(load_settings(), privana_api_base_url="https://one.example")
    settings_two = replace(load_settings(), privana_api_base_url="https://two.example")

    with patch("src.clients.privana.load_settings", return_value=settings_one):
        first = get_privana_client()

    reset_privana_client()

    with patch("src.clients.privana.load_settings", return_value=settings_two):
        second = get_privana_client()

    assert first is not second
    reset_privana_client()


@pytest.mark.asyncio
async def test_authenticated_client_is_a_separate_singleton():
    """The authed client carries a token provider, so it must not be the same
    instance as the plain one used for signature-authed endpoints.
    """
    reset_privana_client()

    with patch("src.clients.privana.load_settings", return_value=_settings()):
        authed = await get_authenticated_privana_client()
        again = await get_authenticated_privana_client()
        plain = get_privana_client()

    assert authed is again
    assert authed is not plain
    # The SDK owns refreshing it; the service only supplies the login.
    assert authed._http._token_provider is not None
    assert plain._http._token_provider is None
    reset_privana_client()


@pytest.mark.asyncio
async def test_login_signs_siwe_and_returns_the_lifetime():
    reset_privana_client()

    with patch("src.clients.privana.load_settings", return_value=_settings()):
        client = get_privana_client()
        client.get_siwe_nonce = AsyncMock(
            return_value=_SiweNonce(address=LP_ADDRESS, nonce="abc123"),
        )
        client.login_with_siwe = AsyncMock(
            return_value=_SiweLogin(siwe_token="siwe", jwt_access_token="jwt-token"),
        )

        token, expires_in = await _siwe_login_as_lp()

    assert token == "jwt-token"
    assert expires_in == 3600
    client.get_siwe_nonce.assert_awaited_once_with(LP_ADDRESS)
    siwe_msg, signature = client.login_with_siwe.await_args.args
    assert "wants you to sign in" in siwe_msg
    assert LP_ADDRESS in siwe_msg
    assert "Nonce: abc123" in siwe_msg
    assert signature.startswith("0x")
    reset_privana_client()


@pytest.mark.asyncio
async def test_login_uses_the_unauthenticated_client():
    """Signing in through the authed client would re-enter the login it is
    serving, so the nonce and login calls must go to the plain one.
    """
    reset_privana_client()

    with patch("src.clients.privana.load_settings", return_value=_settings()):
        plain = get_privana_client()
        plain.get_siwe_nonce = AsyncMock(
            return_value=_SiweNonce(address=LP_ADDRESS, nonce="abc123"),
        )
        plain.login_with_siwe = AsyncMock(
            return_value=_SiweLogin(siwe_token="siwe", jwt_access_token="jwt-token"),
        )
        authed = await get_authenticated_privana_client()
        authed.get_siwe_nonce = AsyncMock(side_effect=AssertionError("used authed client"))

        await _siwe_login_as_lp()

    plain.get_siwe_nonce.assert_awaited_once()
    reset_privana_client()


@pytest.mark.asyncio
async def test_login_requires_lp_secret_key():
    reset_privana_client()
    settings = replace(_settings(), liquidity_provider_secret_key="")

    with patch("src.clients.privana.load_settings", return_value=settings):
        with pytest.raises(RuntimeError, match="LIQUIDITY_PROVIDER_SECRET_KEY"):
            await _siwe_login_as_lp()

    reset_privana_client()
