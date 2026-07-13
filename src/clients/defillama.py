import logging
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFILLAMA_YIELDS_URL = "https://yields.llama.fi"

# DefiLlama's series is one point per day, so anything shorter than an hour just
# re-fetches the same numbers. Pool metadata (chain, project, underlying) is
# effectively immutable for a listed pool, so it can be held far longer.
CHART_TTL_SEC = 3600
META_TTL_SEC = 24 * 3600


class DefiLlamaClient:
    """Read-only client for DefiLlama's yields API.

    Exists so the browser never talks to DefiLlama directly: APY history is
    fetched server-side, cached here, and served from our own API. That keeps a
    third party out of the user's request path and leaves the source swappable
    without a frontend change.
    """

    def __init__(self, base_url: str = DEFILLAMA_YIELDS_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            timeout=15.0, headers={"accept": "application/json"}
        )
        self._cache: dict[str, tuple[float, Any]] = {}

    def _cached(self, key: str, ttl: int) -> Optional[Any]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at > ttl:
            del self._cache[key]
            return None
        return value

    def _store(self, key: str, value: Any) -> None:
        self._cache[key] = (time.monotonic(), value)

    async def get_pool_chart(self, pool_uuid: str) -> list[dict]:
        """Daily APY history for a DefiLlama pool, oldest first.

        Each point is ``{timestamp, apy, apyBase, apyReward, tvlUsd}`` with apy
        as a percent float (3.14677 = 3.14677%).
        """
        cache_key = f"chart:{pool_uuid}"
        cached = self._cached(cache_key, CHART_TTL_SEC)
        if cached is not None:
            return cached

        response = await self.client.get(f"{self.base_url}/chart/{pool_uuid}")
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "success":
            raise ValueError(f"DefiLlama chart for {pool_uuid} returned {body.get('status')}")

        points = body.get("data") or []
        self._store(cache_key, points)
        return points

    async def get_pool_meta(self, pool_uuid: str) -> Optional[dict]:
        """Descriptor for a single pool: chain, project, underlyingTokens.

        Used to verify a configured pool UUID actually refers to the asset we
        think it does. Returns None when DefiLlama doesn't know the pool.
        """
        cache_key = f"meta:{pool_uuid}"
        cached = self._cached(cache_key, META_TTL_SEC)
        if cached is not None:
            return cached

        response = await self.client.get(
            f"{self.base_url}/poolsEnriched", params={"pool": pool_uuid}
        )
        response.raise_for_status()
        rows = response.json().get("data") or []
        if not rows:
            return None

        self._store(cache_key, rows[0])
        return rows[0]


_client_instance: Optional[DefiLlamaClient] = None


def get_defillama_client() -> DefiLlamaClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = DefiLlamaClient()
    return _client_instance
