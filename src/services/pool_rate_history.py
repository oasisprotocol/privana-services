import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from src.core.db import db_write_many, get_db
from src.services.earn.vault_service import VaultService, get_vault_service

logger = logging.getLogger(__name__)

DAY_SEC = 86400
# Share the price sampler's grid so a portfolio valuation can join rate and
# price rows on the same timestamps.
SAMPLE_INTERVAL_SEC = DAY_SEC // 4  # ~4x/day


def _sample_bucket(timestamp: int) -> int:
    """Snap a timestamp to the sampling grid.

    Bucketing to whole days would collapse every sample taken on the same day
    onto one primary key, and since rows are inserted with OR IGNORE only the
    first would survive. DAY_SEC is an exact multiple of SAMPLE_INTERVAL_SEC.
    """
    return timestamp - (timestamp % SAMPLE_INTERVAL_SEC)


@dataclass(frozen=True)
class PoolRatePoint:
    timestamp: int
    total_assets: str
    total_shares: str


def store_point(pool_id: str, point: PoolRatePoint) -> int:
    return db_write_many(
        get_db(),
        "INSERT OR IGNORE INTO pool_rate_history "
        "(pool_id, timestamp, total_assets, total_shares) VALUES (?, ?, ?, ?)",
        [(pool_id, _sample_bucket(point.timestamp), point.total_assets, point.total_shares)],
    )


def read_points(pool_id: str, days: Optional[int] = None) -> list[PoolRatePoint]:
    sql = "SELECT timestamp, total_assets, total_shares FROM pool_rate_history WHERE pool_id = ?"
    params: list[object] = [pool_id]
    if days is not None:
        sql += " AND timestamp >= ?"
        params.append(int(time.time()) - days * DAY_SEC)
    sql += " ORDER BY timestamp ASC"

    rows = get_db().execute(sql, tuple(params)).fetchall()
    return [
        PoolRatePoint(
            timestamp=row["timestamp"],
            total_assets=row["total_assets"],
            total_shares=row["total_shares"],
        )
        for row in rows
    ]


class PoolRateSampler:
    def __init__(self, service: Optional[VaultService] = None) -> None:
        self._service = service
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def _get_service(self) -> VaultService:
        return self._service or get_vault_service()

    async def sample_once(self) -> int:
        service = self._get_service()
        try:
            pools = await asyncio.to_thread(service.list_pools)
        except Exception:
            logger.exception("Pool rate sample failed to list pools; skipping this round")
            return 0

        now = int(time.time())
        stored = 0
        for pool in pools:
            if not pool.get("active"):
                continue
            pool_id = pool["pool_id"]
            try:
                # Sample the strategy's live AUM, not the on-chain total_assets:
                # for Aave-style pools that figure only moves on sync and would
                # record a staircase instead of a yield curve.
                assets = await service.effective_total_assets(pool_id, pool["total_assets"])
                stored += store_point(
                    pool_id,
                    PoolRatePoint(
                        timestamp=now,
                        total_assets=str(assets),
                        total_shares=str(pool["total_shares"]),
                    ),
                )
            except Exception:
                # One unreadable pool must not abort the others.
                logger.exception("Pool rate sample failed pool=%s", pool_id)
                continue
        return stored

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("Pool rate sampler started (every %ds)", SAMPLE_INTERVAL_SEC)

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
        logger.info("Pool rate sampler stopped")

    async def _run(self) -> None:
        # No backfill: past total_shares is unrecoverable (Sapphire non-archive,
        # no events), so we can only record forward. Every round is wrapped — an
        # escaping exception would kill the task for the life of the process and
        # the service would go on serving traffic while silently recording nothing.
        while self._running:
            try:
                await self.sample_once()
            except Exception:
                logger.exception("Pool rate sample round failed; retrying next interval")
            await asyncio.sleep(SAMPLE_INTERVAL_SEC)


_sampler_instance: Optional[PoolRateSampler] = None


def get_pool_rate_sampler() -> PoolRateSampler:
    global _sampler_instance
    if _sampler_instance is None:
        _sampler_instance = PoolRateSampler()
    return _sampler_instance
