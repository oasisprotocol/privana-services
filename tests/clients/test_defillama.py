from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.clients.defillama import DefiLlamaClient

POOL_UUID = "7e0661bf-8cf3-45e6-9424-31916d4c7b84"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

SAMPLE_CHART = {
    "status": "success",
    "data": [
        {"timestamp": "2026-07-11T23:01:20.944Z", "apy": 3.2, "tvlUsd": 100},
        {"timestamp": "2026-07-12T23:01:20.944Z", "apy": 3.14677, "tvlUsd": 120},
    ],
}

SAMPLE_META = {
    "data": [
        {
            "pool": POOL_UUID,
            "chain": "Base",
            "project": "aave-v3",
            "symbol": "USDC",
            "underlyingTokens": [USDC_BASE],
        }
    ]
}


def _response(payload: dict) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


@pytest.fixture
def client():
    c = DefiLlamaClient()
    c.client = AsyncMock(spec=httpx.AsyncClient)
    return c


class TestGetPoolChart:
    async def test_converts_percent_to_bps(self, client):
        client.client.get.return_value = _response(SAMPLE_CHART)

        points = await client.get_pool_chart(POOL_UUID)

        # DefiLlama speaks percent floats; everything downstream speaks integer bps.
        assert [p.apy_bps for p in points] == [320, 315]

    async def test_returns_points_oldest_first(self, client):
        client.client.get.return_value = _response(
            {
                "status": "success",
                "data": [
                    {"timestamp": "2026-07-12T23:01:20.944Z", "apy": 3.14677},
                    {"timestamp": "2026-07-11T23:01:20.944Z", "apy": 3.2},
                ],
            }
        )

        points = await client.get_pool_chart(POOL_UUID)

        assert [p.apy_bps for p in points] == [320, 315]
        assert points[0].timestamp < points[1].timestamp

    async def test_accepts_a_raw_epoch_timestamp(self, client):
        # A source that switches from ISO-8601 to epoch must not silently drop
        # every point.
        client.client.get.return_value = _response(
            {"status": "success", "data": [{"timestamp": 1752278480, "apy": 3.2}]}
        )

        points = await client.get_pool_chart(POOL_UUID)

        assert [(p.timestamp, p.apy_bps) for p in points] == [(1752278480, 320)]

    async def test_skips_points_missing_apy_or_timestamp(self, client):
        client.client.get.return_value = _response(
            {
                "status": "success",
                "data": [
                    {"timestamp": "2026-07-11T23:01:20.944Z", "apy": 3.2},
                    {"timestamp": "2026-07-12T23:01:20.944Z", "apy": None},
                    {"timestamp": "not-a-date", "apy": 4.0},
                ],
            }
        )

        points = await client.get_pool_chart(POOL_UUID)

        assert [p.apy_bps for p in points] == [320]

    async def test_caches_between_calls(self, client):
        client.client.get.return_value = _response(SAMPLE_CHART)

        await client.get_pool_chart(POOL_UUID)
        await client.get_pool_chart(POOL_UUID)

        # Second call is served from cache: DefiLlama publishes one point per
        # day, so re-fetching per request would be pure waste.
        assert client.client.get.call_count == 1

    async def test_expired_cache_refetches(self, client):
        client.client.get.return_value = _response(SAMPLE_CHART)
        await client.get_pool_chart(POOL_UUID)

        # Age the entry past its TTL rather than sleeping.
        stored_at, value = client._cache[f"chart:{POOL_UUID}"]
        client._cache[f"chart:{POOL_UUID}"] = (stored_at - 7200, value)

        await client.get_pool_chart(POOL_UUID)
        assert client.client.get.call_count == 2

    async def test_raises_when_status_not_success(self, client):
        client.client.get.return_value = _response({"status": "error", "data": []})

        with pytest.raises(ValueError, match="returned error"):
            await client.get_pool_chart(POOL_UUID)

    async def test_does_not_cache_a_failed_fetch(self, client):
        client.client.get.return_value = _response({"status": "error", "data": []})
        with pytest.raises(ValueError):
            await client.get_pool_chart(POOL_UUID)

        client.client.get.return_value = _response(SAMPLE_CHART)
        points = await client.get_pool_chart(POOL_UUID)

        assert len(points) == 2


class TestGetPoolMeta:
    async def test_returns_descriptor(self, client):
        client.client.get.return_value = _response(SAMPLE_META)

        meta = await client.get_pool_meta(POOL_UUID)

        assert meta["chain"] == "Base"
        assert meta["project"] == "aave-v3"
        assert meta["underlyingTokens"] == [USDC_BASE]

    async def test_returns_none_for_unknown_pool(self, client):
        client.client.get.return_value = _response({"data": []})

        assert await client.get_pool_meta("does-not-exist") is None
