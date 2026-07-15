from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.clients.coingecko import CoinGeckoClient, to_price_e8

SAMPLE_SPOT = {"usd-coin": {"usd": 0.999736}, "ethereum": {"usd": 1873.58}}

SAMPLE_CHART = {
    "prices": [
        [1781481600000, 1724.6319835654128],
        [1781568000000, 1750.0],
    ]
}


def _response(payload: dict) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


@pytest.fixture
def client():
    c = CoinGeckoClient()
    c.client = AsyncMock(spec=httpx.AsyncClient)
    return c


class TestToPriceE8:
    def test_scales_by_1e8(self):
        assert to_price_e8(1873.58) == 187358000000

    def test_keeps_stablecoin_precision(self):
        # 0.999736 * 1e8 is 99973599.99999999 as a float; a naive int() would
        # record a depegged stablecoin one unit low, every single sample.
        assert to_price_e8(0.999736) == 99973600


class TestGetSpotPrices:
    async def test_returns_scaled_prices(self, client):
        client.client.get.return_value = _response(SAMPLE_SPOT)

        assert await client.get_spot_prices(["usd-coin", "ethereum"]) == {
            "usd-coin": 99973600,
            "ethereum": 187358000000,
        }

    async def test_omits_coins_without_a_price(self, client):
        # A coin CoinGecko doesn't know must be absent, not recorded as zero.
        client.client.get.return_value = _response({"usd-coin": {"usd": 1.0}, "nonsense": {}})

        assert await client.get_spot_prices(["usd-coin", "nonsense"]) == {"usd-coin": 100000000}

    async def test_no_request_without_coins(self, client):
        assert await client.get_spot_prices([]) == {}
        client.client.get.assert_not_called()


class TestGetPriceHistory:
    async def test_converts_millis_to_seconds_oldest_first(self, client):
        client.client.get.return_value = _response(SAMPLE_CHART)

        points = await client.get_price_history("ethereum", days=30)

        assert [(p.timestamp, p.price_e8) for p in points] == [
            (1781481600, 172463198357),
            (1781568000, 175000000000),
        ]

    async def test_clamps_days_to_the_public_api_limit(self, client):
        # Asking beyond a year makes the public API reject the whole request
        # (error 10012) rather than return the year it does have.
        client.client.get.return_value = _response(SAMPLE_CHART)

        await client.get_price_history("ethereum", days=1000)

        assert client.client.get.call_args.kwargs["params"]["days"] == 365

    async def test_skips_malformed_points(self, client):
        client.client.get.return_value = _response(
            {"prices": [[1781481600000, 1724.0], [1781568000000, None], ["nope"]]}
        )

        points = await client.get_price_history("ethereum", days=30)

        assert [p.price_e8 for p in points] == [172400000000]
