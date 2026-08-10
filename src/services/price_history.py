import asyncio
import json
import logging
import time
from typing import Optional

from src.clients.coingecko import (
    MAX_BACKFILL_DAYS,
    CoinGeckoClient,
    PricePoint,
    get_coingecko_client,
)
from src.core.config import load_settings
from src.core.db import db_write_many, get_db

logger = logging.getLogger(__name__)

DAY_SEC = 86400
SAMPLE_INTERVAL_SEC = DAY_SEC // 4  # ~4x/day
BACKFILL_DELAY_SEC = 5

def _sample_bucket(timestamp: int) -> int:
    """Snap a timestamp to the sampling grid.

    Bucketing to whole days would collapse every sample taken on the same day
    onto one primary key, and since rows are inserted with OR IGNORE only the
    first would survive — sampling 4x/day would silently store 1 point/day.
    DAY_SEC is an exact multiple of SAMPLE_INTERVAL_SEC, so CoinGecko's daily
    backfill points stay on their existing boundaries.
    """
    return timestamp - (timestamp % SAMPLE_INTERVAL_SEC)


def parse_coingecko_token_ids(raw_config: str) -> dict[str, str]:
    if not raw_config.strip():
        return {}
    try:
        parsed = json.loads(raw_config)
    except json.JSONDecodeError:
        logger.exception("COINGECKO_TOKEN_IDS contains invalid JSON; price history disabled")
        return {}
    if not isinstance(parsed, dict):
        logger.error("COINGECKO_TOKEN_IDS must be a JSON object; got %s", type(parsed).__name__)
        return {}
    return {str(k).lower(): str(v) for k, v in parsed.items()}


def configured_coin_ids() -> list[str]:
    mapping = parse_coingecko_token_ids(load_settings().coingecko_token_ids)
    return sorted(set(mapping.values()))


def store_points(coin_id: str, points: list[PricePoint]) -> int:
    rows = [
        (coin_id, _sample_bucket(p.timestamp), p.price_e8)
        for p in sorted(points, key=lambda p: p.timestamp)
    ]
    return db_write_many(
        get_db(),
        "INSERT OR IGNORE INTO token_price_history (coin_id, timestamp, price_e8) VALUES (?, ?, ?)",
        rows,
    )


def read_points(coin_id: str, days: Optional[int] = None) -> list[PricePoint]:
    sql = "SELECT timestamp, price_e8 FROM token_price_history WHERE coin_id = ?"
    params: list[object] = [coin_id]
    if days is not None:
        sql += " AND timestamp >= ?"
        params.append(int(time.time()) - days * 86400)
    sql += " ORDER BY timestamp ASC"

    rows = get_db().execute(sql, tuple(params)).fetchall()
    return [PricePoint(timestamp=row["timestamp"], price_e8=row["price_e8"]) for row in rows]


class PriceSampler:
    def __init__(self, client: Optional[CoinGeckoClient] = None) -> None:
        self._client = client
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def _get_client(self) -> CoinGeckoClient:
        return self._client or get_coingecko_client()

    async def backfill(self, days: int = MAX_BACKFILL_DAYS) -> int:
        stored = 0
        for index, coin_id in enumerate(configured_coin_ids()):
            if index:
                await asyncio.sleep(BACKFILL_DELAY_SEC)
            try:
                points = await self._get_client().get_price_history(coin_id, days)
            except Exception:
                # One unreachable coin must not abort the others.
                logger.exception("Price backfill failed coin=%s", coin_id)
                continue
            written = store_points(coin_id, points)
            stored += written
            logger.info(
                "Price backfill coin=%s fetched=%d stored=%d", coin_id, len(points), written
            )
        return stored

    async def sample_once(self) -> int:
        coin_ids = configured_coin_ids()
        if not coin_ids:
            return 0

        try:
            prices = await self._get_client().get_spot_prices(coin_ids)
        except Exception:
            logger.exception("Price sample failed; skipping this round")
            return 0

        now = int(time.time())
        stored = 0
        for coin_id, price_e8 in prices.items():
            stored += store_points(coin_id, [PricePoint(timestamp=now, price_e8=price_e8)])

        missing = set(coin_ids) - set(prices)
        if missing:
            logger.warning("CoinGecko returned no price for %s", ", ".join(sorted(missing)))
        return stored

    async def start(self) -> None:
        if self._task is not None:
            return
        if not configured_coin_ids():
            logger.info("COINGECKO_TOKEN_IDS is empty; price sampler not started")
            return

        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("Price sampler started (every %ds)", SAMPLE_INTERVAL_SEC)

    async def stop(self) -> None:
        self._running = False
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("Price sampler stopped")

    async def _run(self) -> None:
        # Every round is wrapped, not just the HTTP call inside it: an escaping
        # exception kills the task for the life of the process, and the service
        # would go on serving traffic while silently recording nothing.
        try:
            await self.backfill()
        except Exception:
            logger.exception("Price backfill failed; sampling anyway")

        while self._running:
            try:
                await self.sample_once()
            except Exception:
                logger.exception("Price sample round failed; retrying next interval")
            await asyncio.sleep(SAMPLE_INTERVAL_SEC)


_sampler_instance: Optional[PriceSampler] = None


def get_price_sampler() -> PriceSampler:
    global _sampler_instance
    if _sampler_instance is None:
        _sampler_instance = PriceSampler()
    return _sampler_instance
