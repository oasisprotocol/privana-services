import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

COINGECKO_API_URL = "https://api.coingecko.com/api/v3"

# Prices are stored as integers scaled by 1e8, never as floats: the same column
# holds USDC (~0.999736) and ETH (~1873.58), and this database has no precedent
# for a fractional column. 8 decimals keeps a stablecoin's depeg visible while
# leaving room for six-figure assets inside a signed 64-bit int.
PRICE_SCALE = 10**8

# The public API refuses ranges beyond a year
MAX_BACKFILL_DAYS = 365


@dataclass(frozen=True, slots=True)
class PricePoint:
    timestamp: int
    price_e8: int


def to_price_e8(price: float) -> int:
    return int((Decimal(str(price)) * PRICE_SCALE).to_integral_value())


class CoinGeckoClient:
    def __init__(self, base_url: str = COINGECKO_API_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=15.0, headers={"accept": "application/json"})

    async def get_spot_prices(self, coin_ids: list[str]) -> dict[str, int]:
        if not coin_ids:
            return {}

        response = await self.client.get(
            f"{self.base_url}/simple/price",
            params={"ids": ",".join(sorted(set(coin_ids))), "vs_currencies": "usd"},
        )
        response.raise_for_status()

        prices: dict[str, int] = {}
        for coin_id, quote in (response.json() or {}).items():
            usd = (quote or {}).get("usd")
            if usd is None:
                continue
            prices[str(coin_id)] = to_price_e8(usd)
        return prices

    async def get_price_history(self, coin_id: str, days: int) -> list[PricePoint]:
        # The interval is fixed rather than a parameter: we keep one row per coin
        # per day, so anything finer would be fetched only to be dropped when the
        # points collapse onto their day. (At days=365 the API picks daily on its
        # own; asking makes the coupling explicit.)
        response = await self.client.get(
            f"{self.base_url}/coins/{coin_id}/market_chart",
            params={
                "vs_currency": "usd",
                "days": min(days, MAX_BACKFILL_DAYS),
                "interval": "daily",
            },
        )
        response.raise_for_status()

        points = []
        for entry in response.json().get("prices") or []:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            millis, usd = entry[0], entry[1]
            if millis is None or usd is None:
                continue
            points.append(PricePoint(timestamp=int(millis) // 1000, price_e8=to_price_e8(usd)))

        points.sort(key=lambda p: p.timestamp)
        return points


_client_instance: Optional[CoinGeckoClient] = None


def get_coingecko_client() -> CoinGeckoClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = CoinGeckoClient()
    return _client_instance
