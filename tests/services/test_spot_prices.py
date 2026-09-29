import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.services.spot_prices as spot_prices
from src.services.spot_prices import SpotPrice, SpotPriceCache

USDC = "0x" + "aa" * 32
ETH = "0x" + "bb" * 32
T0 = 1_800_000_000


@pytest.fixture
def clock(monkeypatch):
    now = [float(T0)]
    monkeypatch.setattr(spot_prices.time, "time", lambda: now[0])
    return now


@pytest.fixture(autouse=True)
def mapping(monkeypatch):
    monkeypatch.setattr(spot_prices, "_coin_by_token", lambda: {USDC: "usd-coin", ETH: "ethereum"})


def _client(prices):
    client = MagicMock()
    client.get_spot_prices = AsyncMock(return_value=prices)
    return client


class TestSpotPriceCache:
    async def test_fetches_once_per_interval(self, clock):
        client = _client({"usd-coin": 99_990_000, "ethereum": 272_494_000_000})
        cache = SpotPriceCache(client=client, interval_sec=300)

        first = await cache.current()
        await cache.current()
        assert client.get_spot_prices.await_count == 1
        assert first == [SpotPrice(USDC, 99_990_000, T0), SpotPrice(ETH, 272_494_000_000, T0)]

        clock[0] += 300
        await cache.current()
        assert client.get_spot_prices.await_count == 2

    async def test_leaves_out_tokens_the_provider_did_not_price(self, clock):
        cache = SpotPriceCache(client=_client({"usd-coin": 100_000_000}))
        assert [p.token_id for p in await cache.current()] == [USDC]

    async def test_a_failed_refresh_keeps_the_last_prices_and_waits_out_the_interval(self, clock):
        client = _client({"usd-coin": 100_000_000, "ethereum": 250_000_000_000})
        cache = SpotPriceCache(client=client, interval_sec=300)
        await cache.current()

        clock[0] += 300
        client.get_spot_prices.side_effect = RuntimeError("429 Too Many Requests")
        stale = await cache.current()
        assert [p.updated_at for p in stale] == [T0, T0]

        clock[0] += 10
        await cache.current()
        assert client.get_spot_prices.await_count == 2

    async def test_concurrent_requests_share_one_refresh(self, clock):
        release = asyncio.Event()

        async def slow_prices(_coin_ids):
            await release.wait()
            return {"usd-coin": 100_000_000}

        client = MagicMock()
        client.get_spot_prices = AsyncMock(side_effect=slow_prices)
        cache = SpotPriceCache(client=client)

        requests = [asyncio.create_task(cache.current()) for _ in range(5)]
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*requests)

        assert client.get_spot_prices.await_count == 1
        assert all(r == [SpotPrice(USDC, 100_000_000, T0)] for r in results)
