"""Persistent swap queue. One owner per database, with independent venue loops."""
import asyncio
import logging
from typing import Optional

from src.core.config import load_settings
from src.core.db import get_db

logger = logging.getLogger(__name__)
POLL_INTERVAL = 1.0
lp_transfer_lock = asyncio.Lock()


class SwapWorker:
    def __init__(self) -> None:
        self.settings = load_settings()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        from src.services.swap.internal_pipeline import get_internal_pipeline

        self._internal_pipeline = get_internal_pipeline()
        self._pipeline = None

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        self._tasks = [asyncio.create_task(self._internal_loop())]
        if self.settings.lifi_execution_enabled:
            self._tasks.append(asyncio.create_task(self._lifi_loop()))

    async def stop(self) -> None:
        self._stop.set()
        # Do not cancel a to_thread broadcast: it could still be sending while
        # the DB closes. Finish the current bounded internal batch first.
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._pipeline is not None:
            tasks = list(self._pipeline._tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


    async def run_internal_once(self) -> None:
        """Compatibility delegate; execution lives in InternalSwapPipeline."""
        await self._internal_pipeline.run_internal_once()

    async def _internal_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._internal_pipeline.run_internal_once()
            except Exception:
                logger.exception("Internal swap worker iteration failed")
            await self._pause()

    async def _lifi_loop(self) -> None:
        recovered = False
        while not self._stop.is_set():
            try:
                if not recovered:
                    from src.services.swap.lifi_pipeline import (
                        get_lifi_pipeline,
                        recover_inflight_lifi_swaps,
                    )

                    self._pipeline = await asyncio.to_thread(get_lifi_pipeline)
                    await recover_inflight_lifi_swaps(self._pipeline)
                    recovered = True
                await self.run_lifi_once()
            except Exception:
                logger.exception("LiFi swap dispatcher iteration failed")
            await self._pause()

    async def run_lifi_once(self) -> None:
        if not self.settings.lifi_execution_enabled:
            return
        if self._pipeline is None:
            from src.services.swap.lifi_pipeline import get_lifi_pipeline

            self._pipeline = await asyncio.to_thread(get_lifi_pipeline)
        for swap in self._internal_pipeline._claim("lifi", 5):
            try:
                row = get_db().execute("SELECT * FROM quotes WHERE id = ?", (swap["quote_id"],)).fetchone()
                if row is None:
                    raise ValueError("Scheduled swap quote not found")
                quote = dict(row)
                await self._pipeline.launch(
                    quote, swap["user_address"], int(swap["input_nonce"]),
                    swap["input_signature"], swap_id=swap["id"],
                )
            except Exception as exc:
                self._internal_pipeline._update(swap["id"], status="failed", error=str(exc))

    async def _pause(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL)
        except TimeoutError:
            pass



_worker_instance: Optional[SwapWorker] = None


def get_swap_worker() -> SwapWorker:
    global _worker_instance
    if _worker_instance is None:
        _worker_instance = SwapWorker()
    return _worker_instance
