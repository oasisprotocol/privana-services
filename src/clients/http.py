import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

TRANSIENT_HTTP_ERRORS = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.TimeoutException,
)

_TRANSIENT_HTTP_ATTEMPTS = 3
_TRANSIENT_HTTP_BACKOFF_SEC = 1.0


async def request_with_transient_retry(factory, op: str):
    """Run an idempotent HTTP read, retrying dropped connections, timeouts and 5xx.

    A write could land and still look dropped, so only reads go through here.
    Returns the last response; callers check its status as before.
    """
    for attempt in range(1, _TRANSIENT_HTTP_ATTEMPTS + 1):
        try:
            response = await factory()
        except TRANSIENT_HTTP_ERRORS as exc:
            if attempt == _TRANSIENT_HTTP_ATTEMPTS:
                raise
            failure = exc
        else:
            if response.status_code < 500 or attempt == _TRANSIENT_HTTP_ATTEMPTS:
                return response
            failure = f"HTTP {response.status_code}"
        logger.warning(
            "Transient %s request failure (attempt %d/%d), retrying: %s",
            op,
            attempt,
            _TRANSIENT_HTTP_ATTEMPTS,
            failure,
        )
        await asyncio.sleep(_TRANSIENT_HTTP_BACKOFF_SEC * attempt)
