from unittest.mock import AsyncMock, MagicMock

import httpx

from src.clients.http import get_retry


async def test_a_dropped_connection_is_retried(monkeypatch):
    monkeypatch.setattr("src.clients.http._TRANSIENT_HTTP_BACKOFF_SEC", 0.0)
    ok = MagicMock(status_code=200)
    client = MagicMock(
        get=AsyncMock(side_effect=[httpx.RemoteProtocolError("Server disconnected"), ok])
    )

    assert await get_retry(client, "https://example.test") is ok
    assert client.get.await_count == 2
