from unittest.mock import AsyncMock, MagicMock

import httpx

from src.clients.http import request_with_transient_retry


async def test_a_dropped_connection_is_retried(monkeypatch):
    monkeypatch.setattr("src.clients.http._TRANSIENT_HTTP_BACKOFF_SEC", 0.0)
    ok = MagicMock(status_code=200)
    send = AsyncMock(side_effect=[httpx.RemoteProtocolError("Server disconnected"), ok])

    assert await request_with_transient_retry(send, "test") is ok
    assert send.await_count == 2
