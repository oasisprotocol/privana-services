from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from src.clients.coingecko import CoinGeckoClient, get_coingecko_client
from src.core.config import load_settings
from src.core.tokens import get_supported_token_ids
from src.services.price_history import parse_coingecko_token_ids

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SEC = 300


@dataclass(frozen=True)
class SpotPrice:
    token_id: str
    price_e8: int
    updated_at: int


def _coin_by_token() -> dict[str, str]:
    supported = set(get_supported_token_ids())
    mapping = parse_coingecko_token_ids(load_settings().coingecko_token_ids)
    return {token_id: coin_id for token_id, coin_id in mapping.items() if token_id in supported}


class SpotPriceCache:
    def __init__(
        self,
        client: Optional[CoinGeckoClient] = None,
        interval_sec: int = REFRESH_INTERVAL_SEC,
    ) -> None:
        self._client = client
        self._interval_sec = interval_sec
        self._prices: dict[str, SpotPrice] = {}
        self._attempted_at: Optional[float] = None
        self._lock = asyncio.Lock()

    async def current(self) -> list[SpotPrice]:
        if self._due() or self._lock.locked():
            async with self._lock:
                if self._due():
                    await self._refresh()
        return sorted(self._prices.values(), key=lambda price: price.token_id)

    def _due(self) -> bool:
        return self._attempted_at is None or time.time() - self._attempted_at >= self._interval_sec

    async def _refresh(self) -> None:
        self._attempted_at = time.time()
        coin_by_token = _coin_by_token()
        if not coin_by_token:
            return
        client = self._client or get_coingecko_client()
        try:
            by_coin = await client.get_spot_prices(sorted(set(coin_by_token.values())))
        except Exception:
            logger.warning("Spot price refresh failed; serving the last prices", exc_info=True)
            return
        now = int(time.time())
        for token_id, coin_id in coin_by_token.items():
            price_e8 = by_coin.get(coin_id)
            if price_e8 is not None:
                self._prices[token_id] = SpotPrice(token_id, price_e8, now)


_cache_instance: Optional[SpotPriceCache] = None


def get_spot_price_cache() -> SpotPriceCache:
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = SpotPriceCache()
    return _cache_instance
