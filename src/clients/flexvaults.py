from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from eth_account import Account
from eth_account.messages import encode_defunct
from flexvaults import FlexvaultsClient

from src.core.config import load_settings

_client: Optional[FlexvaultsClient] = None
_auth_token: Optional[str] = None


def get_flexvaults_client() -> FlexvaultsClient:
    global _client
    if _client is None:
        settings = load_settings()
        _client = FlexvaultsClient(base_url=settings.accounting_api_base_url)
    return _client


def reset_flexvaults_client() -> None:
    global _client, _auth_token
    _client = None
    _auth_token = None


async def get_authenticated_flexvaults_client() -> FlexvaultsClient:
    """Return the singleton FlexvaultsClient, lazily authenticated as the
    LP/pool address via SIWE. Required before any endpoint that infers the
    user from the auth context, e.g. `get_balance(token_id)`.
    """
    global _auth_token
    client = get_flexvaults_client()
    if _auth_token is None:
        _auth_token = await _authenticate_as_lp(client)
        client.set_bearer_token(_auth_token)
    return client


async def _authenticate_as_lp(client: FlexvaultsClient) -> str:
    settings = load_settings()
    if not settings.liquidity_provider_secret_key:
        raise RuntimeError(
            "flexvaults SIWE auth requires LIQUIDITY_PROVIDER_SECRET_KEY to be set"
        )

    lp_address = settings.liquidity_provider_address
    nonce = (await client.get_siwe_nonce(lp_address)).nonce

    now = datetime.now(timezone.utc)
    base_url = settings.accounting_api_base_url
    domain = base_url.replace("https://", "").replace("http://", "").rstrip("/")
    message = (
        f"{domain} wants you to sign in with your Ethereum account:\n"
        f"{lp_address}\n\nSign in to FlexVaults\n\n"
        f"URI: {base_url}\n"
        f"Version: 1\nChain ID: {settings.accounting_chain_id}\nNonce: {nonce}\n"
        f"Issued At: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"Expiration Time: {(now + timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )

    account = Account.from_key(settings.liquidity_provider_secret_key)
    signed = account.sign_message(encode_defunct(text=message))
    signature = f"0x{signed.signature.hex()}"

    login = await client.login_with_siwe(message, signature)
    return login.jwt_access_token


__all__ = [
    "get_flexvaults_client",
    "get_authenticated_flexvaults_client",
    "reset_flexvaults_client",
]
