import asyncio
import logging
from typing import Optional

from src.core.config import load_settings
from src.services.earn.vault_service import VaultService, get_vault_service

logger = logging.getLogger(__name__)


class IdleDeployer:
    """Settles each pool with its strategy on a fixed interval.

    Deposits wait on the pool account and withdrawals are paid from it, so
    every round nets what the pool holds against its liquidity buffer and the
    withdrawals waiting on it, and moves the difference in one bridge. Seed
    principal, a deposit settled by recovery, and a reclaim a failed round
    left behind are swept up the same way.
    """

    def __init__(self, service: Optional[VaultService] = None) -> None:
        self._service = service
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def _get_service(self) -> VaultService:
        return self._service or get_vault_service()

    async def deploy_once(self) -> int:
        service = self._get_service()
        try:
            pools = await asyncio.to_thread(service.list_pools)
        except Exception:
            logger.exception("Idle deploy failed to list pools; skipping this round")
            return 0

        moved = 0
        for pool in pools:
            # A paused pool is usually paused because something is wrong with
            # it, which is not the moment to push more funds into its
            # strategy. Exits stay open, so its waiting withdrawals are still
            # reclaimed for.
            try:
                moved += await service.rebalance(pool["pool_id"], allow_deploy=bool(pool.get("active")))
            except Exception:
                # One pool's bridge being down says nothing about the others,
                # and the funds stay where they are until the next round.
                logger.exception("Rebalance failed pool=%s", pool["pool_id"])
        return moved

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("Idle deployer started (every %ds)", load_settings().earn_batch_interval_sec)

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
        logger.info("Idle deployer stopped")

    async def _run(self) -> None:
        # Every round is wrapped: an escaping exception would end the task for
        # the life of the process, and seed recorded afterwards would never be
        # deployed while the pool went on reporting it as backing.
        while self._running:
            try:
                await self.deploy_once()
            except Exception:
                logger.exception("Idle deploy round failed; retrying next interval")
            await asyncio.sleep(load_settings().earn_batch_interval_sec)


_deployer_instance: Optional[IdleDeployer] = None


def get_idle_deployer() -> IdleDeployer:
    global _deployer_instance
    if _deployer_instance is None:
        _deployer_instance = IdleDeployer()
    return _deployer_instance
