import sqlite3
import time
import uuid
from typing import Optional

from src.core.config import load_settings
from src.core.db import db_write, get_db
from src.core.eip712 import recover_transfer_signer
from src.core.validation import validate_signature
from src.models.swap import SwapRecord, SwapStatus

# A swap holds its input_nonce only while it could still consume it on the
# ledger. Once it settles - success or failure - the nonce is free again,
# either spent for good (COMPLETED, REFUNDED) or never touched (FAILED), so a
# later swap may legitimately reuse it (e.g. retrying against a fresh quote
# after a failure).
_ACTIVE_SWAP_STATUSES = (
    SwapStatus.SCHEDULED.value,
    SwapStatus.EXECUTING.value,
    SwapStatus.REFUNDING.value,
)


class SwapScheduler:
    """Validates swap requests and queues authorized swaps as swaps-table rows.

    Per-venue workers (internal/lifi) poll the swaps table and hand rows to
    their pipelines; this class only schedules.
    """

    def __init__(self) -> None:
        self.settings = load_settings()

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
        self._reject_if_nonce_active(user_address.lower(), input_nonce)

        swap_id = str(uuid.uuid4())
        now = int(time.time())
        try:
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
        except sqlite3.IntegrityError as exc:
            # idx_swaps_quote_id: a quote is consumed by exactly one swap ever.
            raise ValueError("A swap has already been scheduled for this quote") from exc
        return self._get_swap(swap_id)

    def _reject_if_nonce_active(self, user_address: str, input_nonce: int) -> None:
        """Refuse a second swap racing an unresolved one for the same input nonce.

        LifiSwap._submit_input treats a 409/422 on this nonce as proof that
        this exact swap's own transfer already landed (safe to resume after
        a crash). That only holds if no other swap could ever be signing
        against the same nonce at the same time - so this checks for one
        still in flight rather than forbidding reuse outright, which would
        also block a legitimate retry against a fresh quote after a failure.
        """
        conflict = get_db().execute(
            f"""SELECT id FROM swaps
                WHERE user_address = ? AND input_nonce = ?
                AND status IN ({",".join("?" * len(_ACTIVE_SWAP_STATUSES))})""",
            (user_address, input_nonce, *_ACTIVE_SWAP_STATUSES),
        ).fetchone()
        if conflict is not None:
            raise ValueError("input_nonce is already in use by another active swap")

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


_scheduler_instance: Optional[SwapScheduler] = None


def get_swap_scheduler() -> SwapScheduler:
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = SwapScheduler()
    return _scheduler_instance
