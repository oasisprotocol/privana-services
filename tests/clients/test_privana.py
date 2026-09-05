import asyncio
from dataclasses import dataclass, replace
from unittest.mock import AsyncMock, patch

import pytest
from privana import PrivanaClient

from src.clients.privana import (
    get_authenticated_privana_client,
    get_privana_client,
    invalidate_privana_auth,
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
async def test_authenticate_signs_siwe_and_sets_bearer_token():
    reset_privana_client()
    settings = replace(
        load_settings(),
        privana_api_base_url="https://accounting.example",
        liquidity_provider_secret_key=LP_PRIVATE_KEY,
        liquidity_provider_address=LP_ADDRESS,
        accounting_chain_id=23295,
    )

    with patch("src.clients.privana.load_settings", return_value=settings):
        client = get_privana_client()
        client.get_siwe_nonce = AsyncMock(
            return_value=_SiweNonce(address=LP_ADDRESS, nonce="abc123"),
        )
        client.login_with_siwe = AsyncMock(
            return_value=_SiweLogin(siwe_token="siwe", jwt_access_token="jwt-token"),
        )

        authed = await get_authenticated_privana_client()

    assert authed is client
    client.get_siwe_nonce.assert_awaited_once_with(LP_ADDRESS)
    client.login_with_siwe.assert_awaited_once()
    siwe_msg, signature = client.login_with_siwe.await_args.args
    assert "wants you to sign in" in siwe_msg
    assert LP_ADDRESS in siwe_msg
    assert "Nonce: abc123" in siwe_msg
    assert signature.startswith("0x")
    assert client._http.get_header("Authorization") == "Bearer jwt-token"
    reset_privana_client()


@pytest.mark.asyncio
async def test_authenticate_caches_token_across_calls():
    reset_privana_client()
    settings = replace(
        load_settings(),
        privana_api_base_url="https://accounting.example",
        liquidity_provider_secret_key=LP_PRIVATE_KEY,
        liquidity_provider_address=LP_ADDRESS,
        accounting_chain_id=23295,
    )

    with patch("src.clients.privana.load_settings", return_value=settings):
        client = get_privana_client()
        client.get_siwe_nonce = AsyncMock(
            return_value=_SiweNonce(address=LP_ADDRESS, nonce="abc123"),
        )
        client.login_with_siwe = AsyncMock(
            return_value=_SiweLogin(siwe_token="siwe", jwt_access_token="jwt-token"),
        )

        first = await get_authenticated_privana_client()
        second = await get_authenticated_privana_client()

    assert first is second
    assert client.login_with_siwe.await_count == 1
    reset_privana_client()


@pytest.mark.asyncio
async def test_reauthenticates_when_token_nears_expiry():
    reset_privana_client()
    settings = replace(
        load_settings(),
        privana_api_base_url="https://accounting.example",
        liquidity_provider_secret_key=LP_PRIVATE_KEY,
        liquidity_provider_address=LP_ADDRESS,
        accounting_chain_id=23295,
    )
    clock = {"now": 1_000_000.0}

    with patch("src.clients.privana.load_settings", return_value=settings), \
         patch("src.clients.privana.time") as mock_time:
        mock_time.monotonic = lambda: clock["now"]
        client = get_privana_client()
        client.get_siwe_nonce = AsyncMock(
            return_value=_SiweNonce(address=LP_ADDRESS, nonce="abc123"),
        )
        client.login_with_siwe = AsyncMock(
            side_effect=[
                _SiweLogin(siwe_token="siwe", jwt_access_token="jwt-one", jwt_expires_in=3600),
                _SiweLogin(siwe_token="siwe", jwt_access_token="jwt-two", jwt_expires_in=3600),
            ],
        )

        await get_authenticated_privana_client()
        assert client._http.get_header("Authorization") == "Bearer jwt-one"

        # Inside the lifetime minus the refresh margin: cached token is reused.
        clock["now"] += 3600 - 300 - 1
        await get_authenticated_privana_client()
        assert client.login_with_siwe.await_count == 1

        # Past the refresh deadline: the client logs in again on its own.
        clock["now"] += 2
        await get_authenticated_privana_client()
        assert client.login_with_siwe.await_count == 2
        assert client._http.get_header("Authorization") == "Bearer jwt-two"

    reset_privana_client()


@pytest.mark.asyncio
async def test_invalidate_forces_relogin():
    reset_privana_client()
    settings = replace(
        load_settings(),
        privana_api_base_url="https://accounting.example",
        liquidity_provider_secret_key=LP_PRIVATE_KEY,
        liquidity_provider_address=LP_ADDRESS,
        accounting_chain_id=23295,
    )

    with patch("src.clients.privana.load_settings", return_value=settings):
        client = get_privana_client()
        client.get_siwe_nonce = AsyncMock(
            return_value=_SiweNonce(address=LP_ADDRESS, nonce="abc123"),
        )
        client.login_with_siwe = AsyncMock(
            return_value=_SiweLogin(siwe_token="siwe", jwt_access_token="jwt-token"),
        )

        await get_authenticated_privana_client()
        invalidate_privana_auth()
        await get_authenticated_privana_client()

    assert client.login_with_siwe.await_count == 2
    reset_privana_client()


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_login():
    reset_privana_client()
    settings = replace(
        load_settings(),
        privana_api_base_url="https://accounting.example",
        liquidity_provider_secret_key=LP_PRIVATE_KEY,
        liquidity_provider_address=LP_ADDRESS,
        accounting_chain_id=23295,
    )

    async def slow_login(*args):
        await asyncio.sleep(0.01)
        return _SiweLogin(siwe_token="siwe", jwt_access_token="jwt-token")

    with patch("src.clients.privana.load_settings", return_value=settings):
        client = get_privana_client()
        client.get_siwe_nonce = AsyncMock(
            return_value=_SiweNonce(address=LP_ADDRESS, nonce="abc123"),
        )
        client.login_with_siwe = AsyncMock(side_effect=slow_login)

        await asyncio.gather(*[get_authenticated_privana_client() for _ in range(5)])

    assert client.login_with_siwe.await_count == 1
    reset_privana_client()


@pytest.mark.asyncio
async def test_authed_read_recovers_from_early_revocation():
    from privana.client.errors import AccountingApiError

    from src.clients.privana import authed_read

    reset_privana_client()
    settings = replace(
        load_settings(),
        privana_api_base_url="https://accounting.example",
        liquidity_provider_secret_key=LP_PRIVATE_KEY,
        liquidity_provider_address=LP_ADDRESS,
        accounting_chain_id=23295,
    )

    with patch("src.clients.privana.load_settings", return_value=settings):
        client = get_privana_client()
        client.get_siwe_nonce = AsyncMock(
            return_value=_SiweNonce(address=LP_ADDRESS, nonce="abc123"),
        )
        client.login_with_siwe = AsyncMock(
            side_effect=[
                _SiweLogin(siwe_token="siwe", jwt_access_token="jwt-one"),
                _SiweLogin(siwe_token="siwe", jwt_access_token="jwt-two"),
            ],
        )

        calls = {"n": 0}

        async def read(c):
            calls["n"] += 1
            if calls["n"] == 1:
                raise AccountingApiError("unauthorized", 401, "token revoked")
            return "ok"

        result = await authed_read(read)

    assert result == "ok"
    assert calls["n"] == 2
    # The first token was invalidated and a second login ran.
    assert client.login_with_siwe.await_count == 2
    reset_privana_client()


@pytest.mark.asyncio
async def test_authed_read_does_not_retry_other_errors():
    from privana.client.errors import AccountingApiError

    from src.clients.privana import authed_read

    reset_privana_client()
    settings = replace(
        load_settings(),
        privana_api_base_url="https://accounting.example",
        liquidity_provider_secret_key=LP_PRIVATE_KEY,
        liquidity_provider_address=LP_ADDRESS,
        accounting_chain_id=23295,
    )

    with patch("src.clients.privana.load_settings", return_value=settings):
        client = get_privana_client()
        client.get_siwe_nonce = AsyncMock(
            return_value=_SiweNonce(address=LP_ADDRESS, nonce="abc123"),
        )
        client.login_with_siwe = AsyncMock(
            return_value=_SiweLogin(siwe_token="siwe", jwt_access_token="jwt-one"),
        )

        calls = {"n": 0}

        async def read(c):
            calls["n"] += 1
            raise AccountingApiError("bad request", 400, "pool paused")

        with pytest.raises(AccountingApiError):
            await authed_read(read)

    # A 400 is not an auth problem, so no re-login and no replay.
    assert calls["n"] == 1
    assert client.login_with_siwe.await_count == 1
    reset_privana_client()


@pytest.mark.asyncio
async def test_authenticate_requires_lp_secret_key():
    reset_privana_client()
    settings = replace(
        load_settings(),
        privana_api_base_url="https://accounting.example",
        liquidity_provider_secret_key="",
        liquidity_provider_address=LP_ADDRESS,
    )

    with patch("src.clients.privana.load_settings", return_value=settings):
        with pytest.raises(RuntimeError, match="LIQUIDITY_PROVIDER_SECRET_KEY"):
            await get_authenticated_privana_client()

    reset_privana_client()
