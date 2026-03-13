import json
import logging
import time
import uuid
from typing import Optional

from src.clients.accounting import get_accounting_client
from src.clients.lifi import get_lifi_client
from src.config import load_settings
from src.db import db_write, get_db
from src.models.swap import SUBMISSION_ACCEPTED, VALID_TRANSITIONS, SwapRecord, SwapStatus

logger = logging.getLogger(__name__)


class SwapExecutor:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.accounting = get_accounting_client()
        self.lifi = get_lifi_client()

    async def initiate_swap(
        self,
        quote_id: str,
        user_address: str,
        lock_signature: str,
        lock_expiry: int,
    ) -> SwapRecord:
        db = get_db()
        quote_row = db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        if quote_row is None:
            raise ValueError("Quote not found")

        quote = dict(quote_row)

        if int(time.time()) > quote["expires_at"]:
            raise ValueError("Quote has expired")

        if quote["user_address"] != user_address.lower():
            raise ValueError("Quote was not created for this user")

        lock_result = await self.accounting.lock_funds(
            user_address=user_address,
            service_address=self.settings.service_address,
            token_id=quote["from_token_id"],
            amount=int(quote["from_amount"]),
            expiry=lock_expiry,
            signature=lock_signature,
        )

        if lock_result.status not in SUBMISSION_ACCEPTED:
            raise ValueError(f"Lock submission failed: {lock_result.detail or lock_result.status}")

        submission_id = lock_result.submission_id

        swap_id = str(uuid.uuid4())
        now = int(time.time())
        db_write(
            db,
            """INSERT INTO swaps
               (id, quote_id, user_address, from_token_id, to_token_id,
                from_chain_id, to_chain_id, from_amount, to_amount_estimate,
                to_amount_min, status, lock_submission_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                swap_id, quote_id, user_address.lower(),
                quote["from_token_id"], quote["to_token_id"],
                quote["from_chain_id"], quote["to_chain_id"],
                quote["from_amount"], quote["to_amount_estimate"],
                quote["to_amount_min"], SwapStatus.PENDING_LOCK.value,
                submission_id, now, now,
            ),
        )

        return self._get_swap(swap_id)

    async def advance_swap(self, swap_id: str) -> SwapRecord:
        swap = self._get_swap(swap_id)
        status = SwapStatus(swap.status)

        if status.is_terminal:
            return swap

        try:
            if status == SwapStatus.PENDING_LOCK:
                return await self._step_confirm_lock(swap)
            elif status == SwapStatus.LOCKED:
                return await self._step_execute(swap)
            elif status in (SwapStatus.EXECUTING, SwapStatus.MONITORING):
                return await self._step_monitor(swap)
            elif status == SwapStatus.SETTLING:
                return await self._step_settle(swap)
            elif status.is_failure:
                return await self._step_refund(swap)
            elif status == SwapStatus.REFUNDING:
                return await self._step_refund(swap)
        except Exception as exc:
            logger.exception(f"Swap {swap_id} failed at step {status.value}")
            self._update_swap(swap_id, error=str(exc))

        return self._get_swap(swap_id)

    async def _step_confirm_lock(self, swap: SwapRecord) -> SwapRecord:
        swap = self._get_swap(swap.id)
        if SwapStatus(swap.status) != SwapStatus.PENDING_LOCK:
            return swap

        now = int(time.time())
        if now - swap.created_at > 120:
            self._update_swap(
                swap.id, status=SwapStatus.SWAP_FAILED,
                error="Lock confirmation timed out"
            )
            return self._get_swap(swap.id)

        try:
            locked_resp = await self.accounting.get_locked_funds(
                user_address=swap.user_address,
                service_address=self.settings.service_address,
            )
        except Exception as exc:
            logger.warning(f"Failed to poll locked funds for swap {swap.id}: {exc}")
            return self._get_swap(swap.id)

        matched_lock_id = None
        for lock in locked_resp.locks:
            if (
                lock.token_id.lower() == swap.from_token_id.lower()
                and str(lock.amount) == str(swap.from_amount)
            ):
                matched_lock_id = lock.lock_id
                break

        if matched_lock_id is not None:
            self._update_swap(
                swap.id,
                status=SwapStatus.LOCKED,
                lock_id=matched_lock_id,
            )
            return self._get_swap(swap.id)

        return self._get_swap(swap.id)

    async def _step_execute(self, swap: SwapRecord) -> SwapRecord:
        swap = self._get_swap(swap.id)
        if SwapStatus(swap.status) != SwapStatus.LOCKED:
            return swap

        db = get_db()
        quote_row = db.execute("SELECT * FROM quotes WHERE id = ?", (swap.quote_id,)).fetchone()
        if quote_row is None:
            self._update_swap(swap.id, status=SwapStatus.SWAP_FAILED, error="Quote not found")
            return self._get_swap(swap.id)

        quote = dict(quote_row)
        lifi_response = json.loads(quote["lifi_response"])
        tx_request = lifi_response.get("transactionRequest", {})

        self._update_swap(swap.id, status=SwapStatus.EXECUTING)

        try:
            to_addr = tx_request.get("to", "")
            calldata = tx_request.get("data", "0x")
            value = int(tx_request.get("value", "0"), 0) if tx_request.get("value") else 0
            gas_limit = int(tx_request.get("gasLimit", "500000"), 0) if tx_request.get("gasLimit") else 500_000

            result = await self.accounting.relay_execute(
                chain_id=swap.from_chain_id,
                to=to_addr,
                data=calldata,
                value=value,
                gas_limit=gas_limit,
            )
            swap_tx_hash = result.detail or result.submission_id
            tool_used = lifi_response.get("tool", "")
            self._update_swap(
                swap.id,
                status=SwapStatus.MONITORING,
                swap_tx_hash=swap_tx_hash,
                lifi_tool_used=tool_used,
            )
        except Exception as exc:
            logger.error(f"Swap execution failed for {swap.id}: {exc}")
            self._update_swap(
                swap.id, status=SwapStatus.SWAP_FAILED,
                error=f"Swap relay failed: {exc}"
            )

        return self._get_swap(swap.id)

    async def _step_monitor(self, swap: SwapRecord) -> SwapRecord:
        swap = self._get_swap(swap.id)
        if SwapStatus(swap.status) not in (SwapStatus.EXECUTING, SwapStatus.MONITORING):
            return swap

        if not swap.swap_tx_hash:
            self._update_swap(
                swap.id, status=SwapStatus.SWAP_FAILED,
                error="No swap tx hash to monitor"
            )
            return self._get_swap(swap.id)

        now = int(time.time())
        is_cross_chain = swap.from_chain_id != swap.to_chain_id
        timeout = self.settings.cross_chain_timeout if is_cross_chain else self.settings.same_chain_timeout

        if now - swap.created_at > timeout:
            self._update_swap(
                swap.id, status=SwapStatus.SWAP_FAILED,
                error="Swap monitoring timed out"
            )
            return self._get_swap(swap.id)

        try:
            status_resp = await self.lifi.get_status(
                tx_hash=swap.swap_tx_hash,
                from_chain=swap.from_chain_id,
                to_chain=swap.to_chain_id,
            )
        except Exception as exc:
            logger.warning(f"Li.Fi status check failed for swap {swap.id}: {exc}")
            return self._get_swap(swap.id)

        lifi_status = status_resp.get("status", "").upper()

        if lifi_status == "DONE":
            receiving = status_resp.get("receiving", {})
            actual_amount = receiving.get("amount", swap.to_amount_estimate)
            self._update_swap(
                swap.id,
                status=SwapStatus.SETTLING,
                to_amount_actual=str(actual_amount),
            )
            return await self._step_settle(self._get_swap(swap.id))
        elif lifi_status == "FAILED":
            self._update_swap(
                swap.id, status=SwapStatus.SWAP_FAILED,
                error="Li.Fi reported swap as failed"
            )

        return self._get_swap(swap.id)

    async def _step_settle(self, swap: SwapRecord) -> SwapRecord:
        swap = self._get_swap(swap.id)
        if SwapStatus(swap.status) != SwapStatus.SETTLING:
            return swap

        if swap.lock_id is None:
            self._update_swap(
                swap.id, status=SwapStatus.SETTLE_FAILED,
                error="No lock ID for settlement"
            )
            return self._get_swap(swap.id)

        output_amount = int(swap.to_amount_actual or swap.to_amount_estimate)

        try:
            await self.accounting.relay_settle_swap(
                user_address=swap.user_address,
                lock_id=swap.lock_id,
                output_token_id=swap.to_token_id,
                output_amount=output_amount,
                swap_tx_hash=swap.swap_tx_hash,
            )
            self._update_swap(swap.id, status=SwapStatus.COMPLETED)
        except Exception as exc:
            logger.error(f"Settlement failed for swap {swap.id}: {exc}")
            self._update_swap(
                swap.id, status=SwapStatus.SETTLE_FAILED,
                error=f"Settlement failed: {exc}"
            )

        return self._get_swap(swap.id)

    async def _step_refund(self, swap: SwapRecord) -> SwapRecord:
        swap = self._get_swap(swap.id)
        status = SwapStatus(swap.status)

        if status == SwapStatus.REFUNDED:
            return swap

        if swap.lock_id is None:
            self._update_swap(swap.id, status=SwapStatus.REFUNDED)
            return self._get_swap(swap.id)

        if status != SwapStatus.REFUNDING:
            self._update_swap(swap.id, status=SwapStatus.REFUNDING)

        try:
            await self.accounting.unlock_funds(
                user_address=swap.user_address,
                lock_id=swap.lock_id,
            )
            self._update_swap(swap.id, status=SwapStatus.REFUNDED)
        except Exception as exc:
            logger.error(f"Refund failed for swap {swap.id}: {exc}")
            self._update_swap(swap.id, error=f"Refund failed: {exc}")

        return self._get_swap(swap.id)

    def _get_swap(self, swap_id: str) -> SwapRecord:
        db = get_db()
        row = db.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
        if row is None:
            raise ValueError(f"Swap {swap_id} not found")
        return SwapRecord(**dict(row))

    def _update_swap(self, swap_id: str, **fields) -> None:
        db = get_db()
        if "status" in fields and isinstance(fields["status"], SwapStatus):
            new_status = fields["status"]
            row = db.execute("SELECT status FROM swaps WHERE id = ?", (swap_id,)).fetchone()
            if row is not None:
                current = SwapStatus(row["status"])
                allowed = VALID_TRANSITIONS.get(current, set())
                if new_status not in allowed:
                    raise ValueError(
                        f"Invalid transition: {current.value} → {new_status.value}"
                    )
        fields["updated_at"] = int(time.time())
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [swap_id]
        db_write(db, f"UPDATE swaps SET {set_clause} WHERE id = ?", tuple(values))

    def get_active_swaps(self) -> list[SwapRecord]:
        db = get_db()
        active_statuses = [s.value for s in SwapStatus if s.is_active]
        placeholders = ",".join("?" for _ in active_statuses)
        rows = db.execute(
            f"SELECT * FROM swaps WHERE status IN ({placeholders})",
            active_statuses,
        ).fetchall()
        return [SwapRecord(**dict(row)) for row in rows]


_executor_instance: Optional[SwapExecutor] = None


def get_swap_executor() -> SwapExecutor:
    global _executor_instance
    if _executor_instance is None:
        _executor_instance = SwapExecutor()
    return _executor_instance
