import asyncio
import logging
from typing import Optional

from src.services.earn.vault_service import VaultService, get_vault_service

logger = logging.getLogger(__name__)

DEPLOY_INTERVAL_SEC = 300


class IdleDeployer:
    """Puts idle pool funds to work.

    Seed principal is paid into a pool's account from outside and recorded
    separately, so nothing about that flow deploys it; it would sit earning
    nothing, which is the opposite of the point. The same applies to a
    deposit whose routing failed and to a reclaim a reverted withdrawal left
    behind. This sweeps all of it into the pool's strategy.
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

        deployed = 0
        for pool in pools:
            # A paused pool is usually paused because something is wrong with
            # it, which is not the moment to push more funds into its
            # strategy. Exits stay open either way.
            if not pool.get("active"):
                continue
            try:
                deployed += await service.deploy_idle(pool["pool_id"])
            except Exception:
                # One pool's bridge being down says nothing about the others,
                # and the funds stay idle until the next round either way.
                logger.exception("Idle deploy failed pool=%s", pool["pool_id"])
        return deployed

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("Idle deployer started (every %ds)", DEPLOY_INTERVAL_SEC)

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
            await asyncio.sleep(DEPLOY_INTERVAL_SEC)


_deployer_instance: Optional[IdleDeployer] = None


def get_idle_deployer() -> IdleDeployer:
    global _deployer_instance
    if _deployer_instance is None:
        _deployer_instance = IdleDeployer()
    return _deployer_instance
