"""Persistent earn queue. Mirrors the swap worker: the request records a row
and returns, this loop executes it."""
import asyncio
import logging
import time
from typing import Optional

from src.core.db import db_write, get_db
from src.core.validation import sanitize_error
from src.services.earn.vault_service import (
    EARN_OP_DEPOSIT,
    EARN_STATUS_EXECUTING,
    EARN_STATUS_FAILED,
    EARN_STATUS_SCHEDULED,
    get_vault_service,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL = 1.0
BATCH_SIZE = 5


class EarnWorker:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        # A row left executing by a restart is not safe to replay: its
        # accounting transfer may already be on chain. Surface it as failed so
        # the operator sees it rather than paying twice.
        self._fail_orphans()
        self._task = asyncio.create_task(self._loop())
        logger.info("Earn worker started (every %.0fs)", POLL_INTERVAL)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        logger.info("Earn worker stopped")

    @staticmethod
    def _fail_orphans() -> None:
        changed = db_write(
            get_db(),
            "UPDATE earn_transactions SET status = ?, error = ?, updated_at = ? "
            "WHERE status = ?",
            (
                EARN_STATUS_FAILED,
                "Interrupted while executing; needs manual reconciliation",
                int(time.time()),
                EARN_STATUS_EXECUTING,
            ),
        ).rowcount
        if changed:
            logger.warning(
                "Earn worker: %d operation(s) were executing at shutdown and "
                "need manual reconciliation", changed,
            )

    @staticmethod
    def _claim(limit: int) -> list[dict]:
        rows = get_db().execute(
            "SELECT * FROM earn_transactions WHERE status = ? "
            "ORDER BY created_at, rowid LIMIT ?",
            (EARN_STATUS_SCHEDULED, limit),
        ).fetchall()
        claimed: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            # One in flight per user: both legs consume that user's accounting
            # transfer nonce, so a second request has to wait for the first to
            # spend it or it signs against a nonce that is already gone.
            if row["user_address"] in seen:
                continue
            now = int(time.time())
            changed = db_write(
                get_db(),
                "UPDATE earn_transactions SET status = ?, claimed_at = ?, "
                "updated_at = ? WHERE id = ? AND status = ?",
                (EARN_STATUS_EXECUTING, now, now, row["id"], EARN_STATUS_SCHEDULED),
            ).rowcount
            if changed:
                claimed.append(dict(row))
                seen.add(row["user_address"])
        return claimed

    async def run_once(self) -> None:
        for row in self._claim(BATCH_SIZE):
            service = get_vault_service()
            call = service.deposit if row["operation"] == EARN_OP_DEPOSIT else service.withdraw
            try:
                await call(
                    pool_id_hex=row["pool_id"],
                    user_address=row["user_address"],
                    amount=row["amount"],
                    nonce=int(row["nonce"]),
                    signature=row["signature"],
                    scheduled_id=row["id"],
                )
            except Exception as exc:
                # deposit/withdraw already mark their own row on an on-chain
                # outcome. Reaching here means it never got that far, so the
                # row is still executing and this is the only thing that will
                # settle it.
                logger.exception("Earn %s %s failed", row["operation"], row["id"])
                service._update_transaction(
                    row["id"], status=EARN_STATUS_FAILED, error=sanitize_error(str(exc)),
                )

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("Earn worker iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL)
            except TimeoutError:
                pass


_worker: Optional[EarnWorker] = None


def get_earn_worker() -> EarnWorker:
    global _worker
    if _worker is None:
        _worker = EarnWorker()
    return _worker
