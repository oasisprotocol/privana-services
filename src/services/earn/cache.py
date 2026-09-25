"""Periodically refreshed snapshot for the public pool listing."""

import asyncio
import logging
from typing import Optional

from src.models.earn import PoolListResponse, PoolResponse
from src.services.earn.registry import get_strategy_registry
from src.services.earn.vault_service import get_vault_service

logger = logging.getLogger(__name__)
REFRESH_INTERVAL_SEC = 60


class PoolListCache:
    def __init__(self) -> None:
        self._snapshot: Optional[PoolListResponse] = None
        self._task: Optional[asyncio.Task] = None

    def get(self) -> Optional[PoolListResponse]:
        return self._snapshot

    async def refresh_once(self) -> None:
        service = get_vault_service()
        pools = await asyncio.to_thread(service.list_pools)
        results = await asyncio.gather(*[
            asyncio.gather(
                service.effective_total_assets(p["pool_id"], p["total_assets"]),
                service.strategy_apy_bps_safe(p["pool_id"]),
            )
            for p in pools
        ])
        snapshot = PoolListResponse(pools=[
            PoolResponse(
                pool_id=p["pool_id"],
                token_id=p["token_id"],
                strategy=get_strategy_registry().get(p["pool_id"]).name,
                total_assets=str(effective),
                apy_bps=apy_bps,
                status="active" if p["active"] else "paused",
                pool_address=p["pool_address"],
            )
            for p, (effective, apy_bps) in zip(pools, results)
        ])
        self._snapshot = snapshot

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh_once()
            except Exception:
                logger.exception("Earn pool refresh failed; keeping previous snapshot")
            await asyncio.sleep(REFRESH_INTERVAL_SEC)


_cache_instance: Optional[PoolListCache] = None


def get_pool_list_cache() -> PoolListCache:
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = PoolListCache()
    return _cache_instance
