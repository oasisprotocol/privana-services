import asyncio
import logging
from typing import Optional

from src.config import load_settings
from src.services.swap_executor import get_swap_executor

logger = logging.getLogger(__name__)


class StatusMonitor:
    def __init__(self) -> None:
        self.settings = load_settings()
        self._is_running = False
        self._task: Optional[asyncio.Task] = None

    async def _run(self) -> None:
        poll_interval = self.settings.swap_poll_interval
        logger.info(f"Status monitor started with {poll_interval}s poll interval")

        while self._is_running:
            try:
                executor = get_swap_executor()
                active_swaps = executor.get_active_swaps()

                if active_swaps:
                    logger.info(f"Processing {len(active_swaps)} active swaps")

                for swap in active_swaps:
                    if not self._is_running:
                        break
                    try:
                        await executor.advance_swap(swap.id)
                    except Exception:
                        logger.exception(f"Error advancing swap {swap.id}")

            except Exception:
                logger.exception("Error during status monitor poll")

            await asyncio.sleep(poll_interval)

        logger.info("Status monitor stopped")

    async def start(self) -> None:
        if self._is_running:
            logger.warning("Status monitor is already running")
            return

        self._is_running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if not self._is_running:
            return

        logger.info("Stopping status monitor...")
        self._is_running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info("Status monitor stopped")


_monitor_instance: Optional[StatusMonitor] = None


def get_status_monitor() -> StatusMonitor:
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = StatusMonitor()
    return _monitor_instance
