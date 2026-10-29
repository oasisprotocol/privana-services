"""One asyncio worker per swap stage, and per chain for the stages that ask
a chain about each swap, so a chain whose RPC hangs holds only its own swaps."""

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from src.clients.base_evm import RPC_SETTINGS, rpc_chain_id
from src.core.config import load_settings
from src.services.swap import internal_pipeline, lifi_pipeline

logger = logging.getLogger(__name__)

# Between rounds of a stage that reads only the database and its own lanes.
ROUND_SEC = 1.0
# Between rounds of a stage that asks a chain or an API about each swap, so
# a hundred swaps waiting there make a hundred calls per recheck, not per second.
RECHECK_SEC = 6.0

STAGES: tuple[tuple[str, Callable[[], Awaitable[None]], float], ...] = (
    ("internal_swap", internal_pipeline.internal_swap_round, ROUND_SEC),
    ("input", lifi_pipeline.input_round, ROUND_SEC),
    ("withdraw", lifi_pipeline.withdraw_round, ROUND_SEC),
    ("credit", lifi_pipeline.credit_round, ROUND_SEC),
    ("refund", lifi_pipeline.refund_round, RECHECK_SEC),
)
# Run once per configured chain RPC, each with the chain it serves.
CHAIN_STAGES: tuple[tuple[str, Callable[[int], Awaitable[None]], float], ...] = (
    ("execute", lifi_pipeline.execute_round, RECHECK_SEC),
    ("deposit", lifi_pipeline.deposit_round, RECHECK_SEC),
)


class SwapWorker:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        self._tasks = [asyncio.create_task(self._run(name, run_round, interval), name=f"swap-{name}")
                       for name, run_round, interval in STAGES]
        settings = load_settings()
        self._tasks += [asyncio.create_task(self._run_on_chain(name, setting, run_round, interval),
                                            name=f"swap-{name}-{setting}")
                        for name, run_round, interval in CHAIN_STAGES
                        for setting in RPC_SETTINGS if getattr(settings, setting)]

    async def stop(self) -> None:
        """Each stage ends its round first: the database closes after this."""
        self._stop.set()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run(self, name: str, run_round: Callable[[], Awaitable[None]], interval: float) -> None:
        while not self._stop.is_set():
            try:
                await run_round()
            except Exception:
                logger.exception("swap stage %s round failed", name)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _run_on_chain(self, name: str, setting: str, run_round: Callable[[int], Awaitable[None]],
                            interval: float) -> None:
        chain_id: Optional[int] = None

        async def chain_round() -> None:
            nonlocal chain_id
            if chain_id is None:
                chain_id = await asyncio.to_thread(rpc_chain_id, setting)
            if chain_id is not None:
                await run_round(chain_id)

        await self._run(f"{name} ({setting})", chain_round, interval)


_worker_instance: Optional[SwapWorker] = None


def get_swap_worker() -> SwapWorker:
    global _worker_instance
    if _worker_instance is None:
        _worker_instance = SwapWorker()
    return _worker_instance
