import asyncio
import logging
import time
import uuid
from typing import Optional

from src.core.config import load_settings
from src.core.db import db_write, get_db
from src.core.eip712 import recover_transfer_signer
from src.core.validation import validate_signature
from src.models.swap import SwapRecord, SwapStatus, SwapVenue
from src.services.swap.internal import get_internal_pipeline
from src.services.swap.lifi import get_lifi_pipeline

logger = logging.getLogger(__name__)

WORKER_POLL_INTERVAL_SEC = 5.0


class SwapExecutor:
    """Queues authorized swaps and hands them to their venue's pipeline."""

    def __init__(self) -> None:
        self.settings = load_settings()
        self._worker_task: Optional[asyncio.Task] = None

    async def schedule_swap(
        self,
        quote_id: str,
        input_nonce: int,
        input_signature: str,
    ) -> SwapRecord:
        quote = self._validate_quote(quote_id)
        validate_signature(input_signature, "input_signature")
        user_address = self._recover_signer(quote, input_nonce, input_signature)
        if quote["user_address"] != user_address.lower():
            raise ValueError("Quote was not created for this user")

        swap_id = str(uuid.uuid4())
        now = int(time.time())
        db_write(
            get_db(),
            """INSERT INTO swaps
               (id, quote_id, user_address, from_token_id, to_token_id,
                from_amount, to_amount_estimate, input_nonce, input_signature,
                status, venue, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                swap_id, quote["id"], user_address.lower(),
                quote["from_token_id"], quote["to_token_id"],
                quote["from_amount"], quote["to_amount_estimate"],
                input_nonce, input_signature,
                SwapStatus.SCHEDULED.value, quote["venue"], now, now,
            ),
        )
        return self._get_swap(swap_id)

    async def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._run_worker())

    async def stop(self) -> None:
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        self._worker_task = None

    async def _run_worker(self) -> None:
        while True:
            try:
                await self._process_pending_swaps()
            except Exception:
                logger.exception("Swap worker pass failed")
            await asyncio.sleep(WORKER_POLL_INTERVAL_SEC)

    async def _process_pending_swaps(self) -> None:
        """Execute queued swaps one at a time, oldest first.

        Internal rows still EXECUTING were interrupted by a restart and are
        simply retried: if the first attempt did land, the retry is rejected
        pre-flight on the spent transfer nonce. Li.Fi rows are only claimed
        while SCHEDULED — once executing, a background task owns them and
        recover_inflight_lifi_swaps settles the ones a restart orphaned.
        """
        rows = get_db().execute(
            """SELECT * FROM swaps
               WHERE (venue = ? AND status IN (?, ?))
                  OR (venue = ? AND status = ?)
               ORDER BY created_at, id""",
            (
                SwapVenue.INTERNAL.value,
                SwapStatus.SCHEDULED.value,
                SwapStatus.EXECUTING.value,
                SwapVenue.LIFI.value,
                SwapStatus.SCHEDULED.value,
            ),
        ).fetchall()
        for row in rows:
            swap = SwapRecord(**dict(row))
            if swap.venue == SwapVenue.LIFI.value:
                await get_lifi_pipeline().execute_swap(swap)
            else:
                await get_internal_pipeline().execute_swap(swap)

    def _validate_quote(self, quote_id: str) -> dict:
        db = get_db()
        row = db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        if row is None:
            raise ValueError("Quote not found")

        quote = dict(row)

        if int(time.time()) >= quote["expires_at"]:
            raise ValueError("Quote has expired")

        return quote

    def _recover_signer(self, quote: dict, input_nonce: int, input_signature: str) -> str:
        try:
            return recover_transfer_signer(
                chain_id=self.settings.accounting_chain_id,
                verifying_contract=self.settings.accounting_contract_address,
                to_address=quote["liquidity_provider"],
                token_id=quote["from_token_id"],
                amount=int(quote["from_amount"]),
                nonce=input_nonce,
                signature=input_signature,
            )
        except Exception as exc:
            raise ValueError("input_signature does not match the quoted transfer") from exc

    def _get_swap(self, swap_id: str) -> SwapRecord:
        db = get_db()
        row = db.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
        if row is None:
            raise ValueError(f"Swap {swap_id} not found")
        return SwapRecord(**dict(row))


_executor_instance: Optional[SwapExecutor] = None


def get_swap_executor() -> SwapExecutor:
    global _executor_instance
    if _executor_instance is None:
        _executor_instance = SwapExecutor()
    return _executor_instance
