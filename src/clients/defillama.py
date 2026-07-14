import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFILLAMA_YIELDS_URL = "https://yields.llama.fi"

# DefiLlama's series is one point per day, so anything shorter than an hour just
# re-fetches the same numbers. Pool metadata (chain, project, underlying) is
# effectively immutable for a listed pool, so it can be held far longer.
CHART_TTL_SEC = 3600
META_TTL_SEC = 24 * 3600


@dataclass(frozen=True, slots=True)
class ChartPoint:
    """One point of a pool's APY chart, in the units the rest of the system uses.

    Kept instead of DefiLlama's raw row: of its eight fields we read two, and the
    cache holds the full series per pool for an hour, so the raw dicts cost ~15x
    what this does.
    """

    timestamp: int  # unix seconds
    apy_bps: int


def _parse_timestamp(value: object) -> Optional[int]:
    """DefiLlama stamps points as ISO-8601 ('2026-07-13T10:01:32.796Z'). Accept a
    raw epoch too, so a future source change doesn't silently drop every point."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


class DefiLlamaClient:
    """Read-only client for DefiLlama's yields API.

    Exists so the browser never talks to DefiLlama directly: APY history is
    fetched server-side, cached here, and served from our own API. That keeps a
    third party out of the user's request path and leaves the source swappable
    without a frontend change.
    """

    def __init__(self, base_url: str = DEFILLAMA_YIELDS_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=15.0, headers={"accept": "application/json"})
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

    async def get_pool_chart(self, pool_uuid: str) -> list[ChartPoint]:
        """Daily APY history for a DefiLlama pool, oldest first.

        DefiLlama reports apy as a percent float (3.14677 = 3.14677%); the rest of
        the system speaks integer bps, and the UI rounds to two decimals which is
        exactly 1 bps, so nothing is lost in the conversion. Points DefiLlama
        reports without a usable timestamp or apy are dropped.
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

        points = []
        for entry in body.get("data") or []:
            timestamp = _parse_timestamp(entry.get("timestamp"))
            apy = entry.get("apy")
            if timestamp is None or apy is None:
                continue
            points.append(ChartPoint(timestamp=timestamp, apy_bps=round(apy * 100)))

        points.sort(key=lambda p: p.timestamp)
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
