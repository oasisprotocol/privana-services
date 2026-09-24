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
    EARN_STATUS_COMPLETED,
    EARN_STATUS_EXECUTING,
    EARN_STATUS_FAILED,
    EARN_STATUS_PENDING,
    EARN_STATUS_SCHEDULED,
    EARN_STATUS_UNDEPLOYED,
    get_vault_service,
)
from src.services.user_queue import users_with_inflight_work

logger = logging.getLogger(__name__)


def _public_error(exc: Exception) -> str:
    """What a caller is allowed to see.

    sanitize_error only scrubs hosts out of revert messages; a provider or
    connection error carries its endpoint verbatim, and this string is served
    from /v1/operations/unsettled. Anything that is not the chain's own answer
    gets a fixed message instead.
    """
    if isinstance(exc, ValueError):
        return sanitize_error(str(exc))
    text = str(exc)
    if "revert" in text.lower():
        return sanitize_error(text)
    return "Operation could not be completed; please retry"

POLL_INTERVAL = 1.0
REPAIR_INTERVAL_SEC = 60.0


class EarnWorker:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info("Earn worker started (every %.0fs)", POLL_INTERVAL)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        logger.info("Earn worker stopped")

    async def _recover(self) -> bool:
        """Reconcile rows a restart left mid-flight, the way the internal swap
        pipeline does it.

        Three outcomes, and which one applies turns on how far the row got:
        a recorded hash can be settled from its receipt; a row that reached
        the contract call without recording one has an unknown outcome and
        must never be replayed, because its accounting transfer may already
        be on chain; anything before that never left, so it is safe to queue
        again. Returns whether anything is still in flight, so a recovery
        pass never runs alongside a fresh claim.
        """
        rows = get_db().execute(
            "SELECT * FROM earn_transactions WHERE status IN (?, ?)",
            (EARN_STATUS_EXECUTING, EARN_STATUS_PENDING),
        ).fetchall()
        if not rows:
            return False
        service = get_vault_service()
        for raw in rows:
            row = dict(raw)
            if row["tx_hash"]:
                await self._settle(service, row)
            elif row["status"] == EARN_STATUS_PENDING:
                logger.warning(
                    "Earn %s %s submission outcome unknown; manual recovery required",
                    row["operation"], row["id"],
                )
                service._update_transaction(
                    row["id"], status=EARN_STATUS_FAILED,
                    error="Submission outcome unknown; manual recovery required",
                )
            else:
                logger.warning(
                    "Earn %s %s never reached the contract; returning it to the queue",
                    row["operation"], row["id"],
                )
                service._update_transaction(row["id"], status=EARN_STATUS_SCHEDULED)
        return True

    @staticmethod
    async def _settle(service, row: dict) -> None:
        try:
            receipt = await asyncio.to_thread(service.sapphire.wait_for_receipt, row["tx_hash"])
        except Exception as exc:
            # A timeout is not a revert. Keep the hash and reconcile it on the
            # next pass; never rebroadcast an uncertain transaction.
            logger.warning("Earn %s receipt still unknown: %s", row["id"], exc)
            service._update_transaction(row["id"], error=sanitize_error(str(exc)))
            return
        if receipt["status"] == 1:
            logger.info("Earn %s %s recovered as landed", row["operation"], row["id"])
            pool_id = bytes.fromhex(row["pool_id"].removeprefix("0x"))
            await service._record_share_delta(
                row["id"], pool_id, receipt["blockNumber"], row["amount"],
            )
            # Never routed, so the idle deployer completes a deposit.
            status = (
                EARN_STATUS_UNDEPLOYED if row["operation"] == EARN_OP_DEPOSIT
                else EARN_STATUS_COMPLETED
            )
            service._update_transaction(row["id"], status=status, error=None)
        else:
            service._update_transaction(
                row["id"], status=EARN_STATUS_FAILED,
                error=f"Transaction reverted: {row['tx_hash']}",
            )

    @staticmethod
    def _claim(limit: int) -> list[dict]:
        rows = get_db().execute(
            "SELECT * FROM earn_transactions WHERE status = ? "
            "ORDER BY created_at, rowid LIMIT ?",
            (EARN_STATUS_SCHEDULED, limit),
        ).fetchall()
        claimed: list[dict] = []
        # Anything this user already has off the queue, in either pipeline, may
        # have spent the nonce this row was signed against.
        seen: set[str] = users_with_inflight_work()
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
                seen.add(row["user_address"].lower())
        return claimed

    async def run_once(self) -> None:
        # Reconcile before claiming: no new work is submitted while anything is
        # still in flight, which is the rule the internal swap pipeline keeps.
        if await self._recover():
            return
        # One row per pass. Claiming a batch would mark rows executing that this
        # pass never reaches, and a crash then reports them failed without
        # having attempted them.
        for row in self._claim(1):
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
                # deposit/withdraw settle their own row once they have an
                # on-chain outcome, so only claim the ones that never got that
                # far. Writing unconditionally would report a deposit that
                # succeeded as failed because some read after it threw.
                logger.exception("Earn %s %s failed", row["operation"], row["id"])
                self._fail_if_unsettled(row["id"], _public_error(exc))

    @staticmethod
    def _fail_if_unsettled(tx_id: str, error: str) -> None:
        changed = db_write(
            get_db(),
            "UPDATE earn_transactions SET status = ?, error = ?, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (EARN_STATUS_FAILED, error, int(time.time()), tx_id, EARN_STATUS_EXECUTING),
        ).rowcount
        if not changed:
            logger.info(
                "Earn %s already settled before the error was recorded; left as is", tx_id,
            )

    async def _loop(self) -> None:
        next_repair = 0.0
        while not self._stop.is_set():
            # Between operations, never alongside one.
            if time.monotonic() >= next_repair:
                try:
                    await get_vault_service().repair_share_ledger()
                except Exception:
                    logger.exception("Earn share ledger repair failed")
                next_repair = time.monotonic() + REPAIR_INTERVAL_SEC
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
