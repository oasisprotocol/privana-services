import asyncio
import logging
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 1.0
TRANSIENT_HTTP_ERRORS = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.TimeoutException,
)


async def send_with_retry(send: Callable[[], Awaitable[httpx.Response]]) -> httpx.Response:
    """Send a read request, retrying dropped connections, timeouts and 5xx responses.

    Only for reads: a write could land and still look dropped, so retrying it could
    repeat it. Returns the last response; callers check its status as before.
    """
    for attempt in range(1, MAX_RETRIES):
        try:
            response = await send()
        except TRANSIENT_HTTP_ERRORS as exc:
            logger.warning(f"HTTP request failed (attempt {attempt}), retrying: {exc!r}")
        else:
            if response.status_code < 500:
                return response
            logger.warning(
                f"HTTP request returned {response.status_code} (attempt {attempt}), retrying"
            )
        await asyncio.sleep(RETRY_DELAY * attempt)
    return await send()
