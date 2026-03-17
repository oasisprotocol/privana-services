import json
import logging
import time
import uuid
from typing import Optional

from src.clients.accounting import get_accounting_client
from src.clients.lifi import get_lifi_client
from src.config import load_settings
from src.db import db_write, get_db
from src.models.api import QuoteResponse
from src.validation import validate_address, validate_amount, validate_token_id

logger = logging.getLogger(__name__)


class QuoteService:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.accounting = get_accounting_client()
        self.lifi = get_lifi_client()

    async def get_quote(
        self,
        from_token_id: str,
        to_token_id: str,
        from_amount: str,
        user_address: str,
        slippage: float = 0.03,
    ) -> QuoteResponse:
        validate_token_id(from_token_id, "from_token_id")
        validate_token_id(to_token_id, "to_token_id")
        validate_amount(from_amount, "from_amount")
        validate_address(user_address, "user_address")

        existing = self._find_existing_quote(user_address, from_token_id, to_token_id, from_amount)
        if existing is not None:
            return existing

        from_info = await self.accounting.get_token_info(from_token_id)
        to_info = await self.accounting.get_token_info(to_token_id)

        if not from_info.chain_id or not to_info.chain_id:
            raise ValueError("Token chain info not available")

        from_chain_id = from_info.chain_id
        to_chain_id = to_info.chain_id
        from_on_chain = from_info.token_address or "0x0000000000000000000000000000000000000000"
        to_on_chain = to_info.token_address or "0x0000000000000000000000000000000000000000"

        lifi_response = await self.lifi.get_quote(
            from_chain=from_chain_id,
            to_chain=to_chain_id,
            from_token=from_on_chain,
            to_token=to_on_chain,
            from_amount=from_amount,
            from_address=self.settings.vault_evm_address,
            slippage=slippage,
        )

        estimate = lifi_response.get("estimate", {})
        to_amount_str = estimate.get("toAmount", "0")
        to_amount_min_str = estimate.get("toAmountMin", to_amount_str)

        fee_bps = self.settings.fee_bps
        to_amount = int(to_amount_str)
        fee_amount = (to_amount * fee_bps) // 10_000
        to_amount_after_fee = to_amount - fee_amount
        to_amount_min = int(to_amount_min_str) - fee_amount

        tool_used = lifi_response.get("tool")

        tx_request = lifi_response.get("transactionRequest", {})
        approval_address = tx_request.get("to")

        quote_id = str(uuid.uuid4())
        now = int(time.time())
        expires_at = now + self.settings.quote_ttl

        db = get_db()
        db_write(
            db,
            """INSERT INTO quotes
               (id, user_address, from_token_id, to_token_id, from_chain_id, to_chain_id,
                from_amount, to_amount_estimate, to_amount_min, lifi_response,
                approval_address, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                quote_id, user_address.lower(), from_token_id.lower(), to_token_id.lower(),
                from_chain_id, to_chain_id,
                from_amount, str(to_amount_after_fee), str(max(to_amount_min, 0)),
                json.dumps(lifi_response),
                approval_address, expires_at, now,
            ),
        )

        return QuoteResponse(
            quote_id=quote_id,
            from_token_id=from_token_id.lower(),
            to_token_id=to_token_id.lower(),
            from_chain_id=from_chain_id,
            to_chain_id=to_chain_id,
            from_amount=from_amount,
            to_amount_gross=to_amount_str,
            to_amount_estimate=str(to_amount_after_fee),
            to_amount_min=str(max(to_amount_min, 0)),
            fee_bps=fee_bps,
            fee_amount=str(fee_amount),
            tool_used=tool_used,
            approval_address=approval_address,
            expires_at=expires_at,
        )

    def _find_existing_quote(
        self,
        user_address: str,
        from_token_id: str,
        to_token_id: str,
        from_amount: str,
    ) -> Optional[QuoteResponse]:
        db = get_db()
        now = int(time.time())
        row = db.execute(
            """SELECT * FROM quotes
               WHERE user_address = ? AND from_token_id = ? AND to_token_id = ?
               AND from_amount = ? AND expires_at > ?
               ORDER BY created_at DESC LIMIT 1""",
            (user_address.lower(), from_token_id.lower(), to_token_id.lower(), from_amount, now),
        ).fetchone()

        if row is None:
            return None

        quote = dict(row)
        lifi_response = json.loads(quote["lifi_response"])
        estimate = lifi_response.get("estimate", {})
        to_amount_gross = estimate.get("toAmount", "0")
        fee_bps = self.settings.fee_bps
        fee_amount = (int(to_amount_gross) * fee_bps) // 10_000

        return QuoteResponse(
            quote_id=quote["id"],
            from_token_id=quote["from_token_id"],
            to_token_id=quote["to_token_id"],
            from_chain_id=quote["from_chain_id"],
            to_chain_id=quote["to_chain_id"],
            from_amount=quote["from_amount"],
            to_amount_gross=to_amount_gross,
            to_amount_estimate=quote["to_amount_estimate"],
            to_amount_min=quote["to_amount_min"],
            fee_bps=fee_bps,
            fee_amount=str(fee_amount),
            tool_used=lifi_response.get("tool"),
            approval_address=quote["approval_address"],
            expires_at=quote["expires_at"],
        )

    def get_stored_quote(self, quote_id: str) -> Optional[dict]:
        db = get_db()
        row = db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        if row is None:
            return None
        return dict(row)

_service_instance: Optional[QuoteService] = None


def get_quote_service() -> QuoteService:
    global _service_instance
    if _service_instance is None:
        _service_instance = QuoteService()
    return _service_instance
