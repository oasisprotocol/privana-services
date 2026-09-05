from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional, TypeVar

from eth_account import Account
from eth_account.messages import encode_defunct
from privana import PrivanaClient
from privana.client.errors import AccountingApiError

from src.core.config import load_settings

T = TypeVar("T")

_client: Optional[PrivanaClient] = None
_auth_token: Optional[str] = None
_auth_refresh_deadline: float = 0.0
_auth_generation: int = 0
_auth_lock: Optional[asyncio.Lock] = None

# Re-login this long before the JWT's stated lifetime runs out. The server's
# clock, not ours, decides when the token actually dies, so the margin absorbs
# skew between the two. Capped to a fifth of the lifetime so short-lived
# tokens don't degenerate into a login per request.
_REFRESH_MARGIN_SEC = 300


def get_privana_client() -> PrivanaClient:
    global _client
    if _client is None:
        settings = load_settings()
        _client = PrivanaClient(base_url=settings.privana_api_base_url)
    return _client


def reset_privana_client() -> None:
    global _client, _auth_token, _auth_refresh_deadline, _auth_lock, _auth_generation
    _client = None
    _auth_token = None
    _auth_refresh_deadline = 0.0
    _auth_generation += 1
    _auth_lock = None


def invalidate_privana_auth() -> None:
    """Drop the cached token so the next authenticated call logs in again.

    For callers that see the API reject a token before its stated expiry
    (revocation, server restart): invalidate, then retry the read once.
    The generation bump also voids any login already in flight, so its
    result cannot silently override the invalidation.
    """
    global _auth_token, _auth_refresh_deadline, _auth_generation
    _auth_token = None
    _auth_refresh_deadline = 0.0
    _auth_generation += 1


def _token_is_fresh() -> bool:
    # monotonic, not wall clock: a backwards clock step must never stretch a
    # token's perceived lifetime past what the server granted.
    return _auth_token is not None and time.monotonic() < _auth_refresh_deadline


def _store_auth(token: str, expires_in: int) -> None:
    global _auth_token, _auth_refresh_deadline
    lifetime = max(expires_in, 1)
    margin = min(_REFRESH_MARGIN_SEC, lifetime // 5)
    _auth_token = token
    _auth_refresh_deadline = time.monotonic() + max(lifetime - margin, 1)


async def get_authenticated_privana_client() -> PrivanaClient:
    """Return the singleton PrivanaClient with a bearer token still inside
    its stated lifetime, re-running the SIWE login when the cached one is
    missing or about to expire.

    The login response's ``jwt_expires_in`` sets the deadline. Caching the
    token without it meant a long-running service kept sending a token the
    API had expired, and every authenticated read failed until a restart.
    The lock serializes concurrent refreshes so a burst of callers produces
    one login, not one each.
    """
    global _auth_lock
    client = get_privana_client()
    if _token_is_fresh():
        return client
    if _auth_lock is None:
        _auth_lock = asyncio.Lock()
    async with _auth_lock:
        # Loop until a login completes with no concurrent invalidation: an
        # invalidation that lands mid-login bumps the generation, and handing
        # back the client without a fresh store would leave its bearer header
        # on the very token the invalidator condemned.
        while not _token_is_fresh():
            generation = _auth_generation
            token, expires_in = await _authenticate_as_lp(client)
            if generation == _auth_generation:
                _store_auth(token, expires_in)
                client.set_bearer_token(token)
    return client


async def authed_read(call: Callable[[PrivanaClient], Awaitable[T]]) -> T:
    """Run an authenticated, idempotent SDK read, recovering once if the token
    is rejected before its deadline.

    The deadline-based refresh handles expiry, but a token can also be voided
    early (the accounting service restarting or revoking it). That surfaces as
    a 401/403, so we drop the cached token and log in again for a single retry.
    Only safe for reads: a mutating call must never be replayed blindly.
    """
    client = await get_authenticated_privana_client()
    try:
        return await call(client)
    except AccountingApiError as exc:
        if exc.status_code not in (401, 403):
            raise
        invalidate_privana_auth()
        client = await get_authenticated_privana_client()
        return await call(client)


async def _authenticate_as_lp(client: PrivanaClient) -> tuple[str, int]:
    settings = load_settings()
    if not settings.liquidity_provider_secret_key:
        raise RuntimeError(
            "privana SIWE auth requires LIQUIDITY_PROVIDER_SECRET_KEY to be set"
        )

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
    "authed_read",
    "get_privana_client",
    "get_authenticated_privana_client",
    "invalidate_privana_auth",
    "reset_privana_client",
]
