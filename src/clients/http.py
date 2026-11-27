import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

_TRANSIENT_HTTP_ERRORS = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.TimeoutException,
)

_TRANSIENT_HTTP_ATTEMPTS = 3
_TRANSIENT_HTTP_BACKOFF_SEC = 1.0


async def get_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    return await _retry(client.get, url, **kwargs)


async def post_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """Only for POSTs that read (e.g. a route quote): a write could land and still look dropped."""
    return await _retry(client.post, url, **kwargs)


async def _retry(send, url: str, **kwargs) -> httpx.Response:
    """Retry dropped connections, timeouts and 5xx; return the last response for the caller to check."""
    for attempt in range(1, _TRANSIENT_HTTP_ATTEMPTS + 1):
        try:
            response = await send(url, **kwargs)
        except _TRANSIENT_HTTP_ERRORS as exc:
            if attempt == _TRANSIENT_HTTP_ATTEMPTS:
                raise
            failure = exc
        else:
            if response.status_code < 500 or attempt == _TRANSIENT_HTTP_ATTEMPTS:
                return response
            failure = f"HTTP {response.status_code}"
        logger.warning(
            "Transient request failure for %s (attempt %d/%d), retrying: %s",
            url,
            attempt,
            _TRANSIENT_HTTP_ATTEMPTS,
            failure,
        )
        await asyncio.sleep(_TRANSIENT_HTTP_BACKOFF_SEC * attempt)
