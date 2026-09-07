from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from eth_account import Account
from eth_account.messages import encode_defunct
from privana import PrivanaClient

from src.core.config import load_settings

_client: Optional[PrivanaClient] = None
_authed_client: Optional[PrivanaClient] = None


def get_privana_client() -> PrivanaClient:
    """Unauthenticated client, for endpoints that carry their own signature."""
    global _client
    if _client is None:
        settings = load_settings()
        _client = PrivanaClient(base_url=settings.privana_api_base_url)
    return _client


def reset_privana_client() -> None:
    global _client, _authed_client
    _client = None
    _authed_client = None


async def get_authenticated_privana_client() -> PrivanaClient:
    """Client that acts as the LP/pool address on endpoints which infer the
    user from the bearer token, e.g. ``get_balance(token_id)``.

    The SDK owns the token: it logs in on first use, logs in again before the
    token expires, and replays a request once if the server rejects a token
    early. Callers just make their call. The token lives on this instance, so
    a client built for another identity keeps its own.
    """
    global _authed_client
    if _authed_client is None:
        settings = load_settings()
        _authed_client = PrivanaClient(
            base_url=settings.privana_api_base_url,
            token_provider=_siwe_login_as_lp,
        )
    return _authed_client


async def _siwe_login_as_lp() -> tuple[str, int]:
    """Sign in as the LP over SIWE, returning the token and its lifetime.

    Runs on the unauthenticated client: the nonce and login endpoints need no
    bearer token, and borrowing the authenticated one would re-enter the very
    login this is serving.
    """
    settings = load_settings()
    if not settings.liquidity_provider_secret_key:
        raise RuntimeError(
            "privana SIWE auth requires LIQUIDITY_PROVIDER_SECRET_KEY to be set"
        )

    client = get_privana_client()
    lp_address = settings.liquidity_provider_address
    nonce = (await client.get_siwe_nonce(lp_address)).nonce

    now = datetime.now(timezone.utc)
    base_url = settings.privana_api_base_url
    domain = base_url.replace("https://", "").replace("http://", "").rstrip("/")
    message = (
        f"{domain} wants you to sign in with your Ethereum account:\n"
        f"{lp_address}\n\nSign in to Privana on chain {settings.accounting_chain_id}\n\n"
        f"URI: {base_url}\n"
        f"Version: 1\nChain ID: {settings.accounting_chain_id}\nNonce: {nonce}\n"
        f"Issued At: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"Expiration Time: {(now + timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )

    account = Account.from_key(settings.liquidity_provider_secret_key)
    signed = account.sign_message(encode_defunct(text=message))
    signature = f"0x{signed.signature.hex()}"

    login = await client.login_with_siwe(message, signature)
    return login.jwt_access_token, login.jwt_expires_in


__all__ = [
    "get_privana_client",
    "get_authenticated_privana_client",
    "reset_privana_client",
]
