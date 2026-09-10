import asyncio
import logging
from typing import Awaitable, Callable, Optional

from src.core.db import get_db
from src.models.swap import SwapRecord

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SEC = 5.0


class SwapWorker:
    """Polls the swaps table for one venue and hands each queued row to its pipeline."""

    def __init__(
        self,
        name: str,
        claim_sql: str,
        claim_params: tuple,
        execute: Optional[Callable[[SwapRecord], Awaitable[None]]] = None,
        execute_batch: Optional[Callable[[list[SwapRecord]], Awaitable[None]]] = None,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
    ) -> None:
        self._name = name
        self._claim_sql = claim_sql
        self._claim_params = claim_params
        self._execute = execute
        self._execute_batch = execute_batch
        self._poll_interval_sec = poll_interval_sec
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_worker())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run_worker(self) -> None:
        while True:
            try:
                await self._process()
            except Exception:
                logger.exception("%s swap worker pass failed", self._name)
            await asyncio.sleep(self._poll_interval_sec)

    async def _process(self) -> None:
        rows = get_db().execute(self._claim_sql, self._claim_params).fetchall()
        swaps = [SwapRecord(**dict(row)) for row in rows]

        if self._execute_batch is not None:
            try:
                await self._execute_batch(swaps)
            except Exception:
                logger.exception("%s swap worker batch failed", self._name)
            return

        for swap in swaps:
            try:
                await self._execute(swap)
            except Exception:
                logger.exception("%s swap %s execution failed", self._name, swap.id)
