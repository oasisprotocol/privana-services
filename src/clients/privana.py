from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from eth_account import Account
from eth_account.messages import encode_defunct
from privana import PrivanaClient

from src.core.config import load_settings

_client: Optional[PrivanaClient] = None
_swap_lp_client: Optional[PrivanaClient] = None
_earn_pool_client: Optional[PrivanaClient] = None


def get_privana_client() -> PrivanaClient:
    """Unauthenticated client, for endpoints that carry their own signature."""
    global _client
    if _client is None:
        settings = load_settings()
        _client = PrivanaClient(base_url=settings.privana_api_base_url)
    return _client


def reset_privana_client() -> None:
    global _client, _swap_lp_client, _earn_pool_client
    _client = None
    _swap_lp_client = None
    _earn_pool_client = None


async def get_swap_lp_privana_client() -> PrivanaClient:
    """Client that acts as the swap liquidity provider on endpoints which infer
    the user from the bearer token, e.g. ``get_balance(token_id)``.

    Swap only. Earn pools hold their funds in a different account and read them
    through ``get_earn_pool_privana_client``.

    The SDK owns the token: it logs in on first use, logs in again before the
    token expires, and replays a request once if the server rejects a token
    early. Callers just make their call. The token lives on this instance, so
    a client built for another identity keeps its own.
    """
    global _swap_lp_client
    if _swap_lp_client is None:
        settings = load_settings()
        _swap_lp_client = PrivanaClient(
            base_url=settings.privana_api_base_url,
            token_provider=_siwe_login_as_lp,
        )
    return _swap_lp_client


async def get_earn_pool_privana_client() -> PrivanaClient:
    """Client that acts as the earn pool account.

    Separate instance from the LP client on purpose: ``get_balance`` answers
    for whoever the bearer token belongs to, so reading a pool's balance
    through the LP's client would report the LP's funds, swap float included,
    as the pool's backing. Each client holds its own token (privana-sdk).
    """
    global _earn_pool_client
    if _earn_pool_client is None:
        settings = load_settings()
        _earn_pool_client = PrivanaClient(
            base_url=settings.privana_api_base_url,
            token_provider=_siwe_login_as_earn_pool,
        )
    return _earn_pool_client


async def _siwe_login_as_lp() -> tuple[str, int]:
    settings = load_settings()
    return await _siwe_login(
        settings.liquidity_provider_secret_key,
        settings.liquidity_provider_address,
        "LIQUIDITY_PROVIDER_SECRET_KEY",
    )


async def _siwe_login_as_earn_pool() -> tuple[str, int]:
    settings = load_settings()
    return await _siwe_login(
        settings.earn_pool_secret_key,
        settings.earn_pool_address,
        "EARN_POOL_SECRET_KEY",
    )


async def _siwe_login(
    secret_key: str, address: str, setting_name: str
) -> tuple[str, int]:
    """Sign in as ``address`` over SIWE, returning the token and its lifetime.

    Runs on the unauthenticated client: the nonce and login endpoints need no
    bearer token, and borrowing the authenticated one would re-enter the very
    login this is serving.
    """
    settings = load_settings()
    if not secret_key:
        raise RuntimeError(f"privana SIWE auth requires {setting_name} to be set")

    client = get_privana_client()
    nonce = (await client.get_siwe_nonce(address)).nonce

    now = datetime.now(timezone.utc)
    base_url = settings.privana_api_base_url
    domain = base_url.replace("https://", "").replace("http://", "").rstrip("/")
    message = (
        f"{domain} wants you to sign in with your Ethereum account:\n"
        f"{address}\n\nSign in to Privana on chain {settings.accounting_chain_id}\n\n"
        f"URI: {base_url}\n"
        f"Version: 1\nChain ID: {settings.accounting_chain_id}\nNonce: {nonce}\n"
        f"Issued At: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"Expiration Time: {(now + timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )

    account = Account.from_key(secret_key)
    signed = account.sign_message(encode_defunct(text=message))
    signature = f"0x{signed.signature.hex()}"

    login = await client.login_with_siwe(message, signature)
    return login.jwt_access_token, login.jwt_expires_in


__all__ = [
    "get_privana_client",
    "get_swap_lp_privana_client",
    "get_earn_pool_privana_client",
    "reset_privana_client",
]
