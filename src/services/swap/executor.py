import time
import uuid
from typing import Optional

from src.core.config import load_settings
from src.core.db import db_write, get_db
from src.core.eip712 import recover_transfer_signer
from src.core.validation import validate_signature
from src.models.swap import SwapRecord, SwapStatus, SwapVenue
from src.services.user_queue import assert_nonce_free


class SwapExecutor:
    """Validate and durably enqueue requests; never contact a chain in the request path."""

    def __init__(self) -> None:
        self.settings = load_settings()

    async def schedule_swap(
        self, quote_id: str, input_nonce: int, input_signature: str
    ) -> SwapRecord:
        if not 0 <= input_nonce < 2**256:
            raise ValueError("Invalid input nonce")
        validate_signature(input_signature, "input_signature")
        # An HTTP retry returns the original operation, including after quote expiry.
        existing = get_db().execute(
            "SELECT * FROM swaps WHERE quote_id = ? AND input_nonce = ? "
            "AND input_signature = ?",
            (quote_id, str(input_nonce), input_signature.lower()),
        ).fetchone()
        if existing is not None:
            return SwapRecord(**dict(existing))
        quote = self._validate_quote(quote_id)
        user_address = self._recover_signer(quote, input_nonce, input_signature).lower()
        if quote["user_address"] != user_address:
            raise ValueError("Quote was not created for this user")
        assert_nonce_free(user_address, input_nonce)
        venue = quote.get("venue")
        if venue not in {v.value for v in SwapVenue}:
            raise ValueError("Unsupported swap venue")
        if venue == SwapVenue.LIFI.value and not self.settings.lifi_execution_enabled:
            raise ValueError("LiFi execution is disabled")
        swap_id = str(uuid.uuid4())
        now = int(time.time())
        db_write(
            get_db(),
            """INSERT INTO swaps
               (id, quote_id, user_address, from_token_id, to_token_id,
                from_amount, to_amount_estimate, status, venue, created_at, updated_at,
                input_nonce, input_signature)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (swap_id, quote_id, user_address, quote["from_token_id"],
             quote["to_token_id"], quote["from_amount"], quote["to_amount_estimate"],
             SwapStatus.SCHEDULED.value, venue, now, now, str(input_nonce),
             input_signature.lower()),
        )
        return self._get_swap(swap_id)

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

    def _update_swap(self, swap_id: str, **fields) -> None:
        db = get_db()
        fields["updated_at"] = int(time.time())
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [swap_id]
        db_write(db, f"UPDATE swaps SET {set_clause} WHERE id = ?", tuple(values))


_executor_instance: Optional[SwapExecutor] = None


def get_swap_executor() -> SwapExecutor:
    global _executor_instance
    if _executor_instance is None:
        _executor_instance = SwapExecutor()
    return _executor_instance
