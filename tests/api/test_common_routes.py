from unittest.mock import AsyncMock, MagicMock, patch

from src.services.spot_prices import SpotPrice

TOKEN_ID = "0x" + "aa" * 32


class TestPricesRoute:
    async def test_returns_cached_prices_as_usd_strings(self, api_client):
        cache = MagicMock()
        cache.current = AsyncMock(return_value=[SpotPrice(TOKEN_ID, 99_990_111, 1_790_670_410)])
        with patch("src.services.spot_prices.get_spot_price_cache", return_value=cache):
            r = await api_client.get("/v1/prices")

        assert r.status_code == 200
        assert r.json() == {
            "prices": [{"token_id": TOKEN_ID, "usd": "0.99990111", "updated_at": 1_790_670_410}]
        }

    async def test_returns_an_empty_list_before_any_price_is_known(self, api_client):
        cache = MagicMock()
        cache.current = AsyncMock(return_value=[])
        with patch("src.services.spot_prices.get_spot_price_cache", return_value=cache):
            r = await api_client.get("/v1/prices")

        assert r.status_code == 200
        assert r.json() == {"prices": []}
