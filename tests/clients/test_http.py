from unittest.mock import AsyncMock, MagicMock

import httpx

from src.clients.http import send_with_retry


async def test_a_dropped_connection_is_retried(monkeypatch):
    monkeypatch.setattr("src.clients.http.RETRY_DELAY", 0.0)
    ok = MagicMock(status_code=200)
    send = AsyncMock(side_effect=[httpx.RemoteProtocolError("Server disconnected"), ok])

    assert await send_with_retry(send) is ok
    assert send.await_count == 2
