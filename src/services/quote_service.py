import logging
import time
import uuid
from typing import Optional

from src.clients.accounting import get_accounting_client
from src.clients.lifi import get_lifi_client
from src.config import load_settings
from src.db import db_write, get_db
from src.fees import calculate_fee
from src.models.api import QuoteResponse
from src.validation import validate_address, validate_amount, validate_token_id

logger = logging.getLogger(__name__)


CLEANUP_INTERVAL = 60


class QuoteService:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.accounting = get_accounting_client()
        self.lifi = get_lifi_client()
        self._last_cleanup = 0

    async def get_quote(
        self,
        from_token_id: str,
        to_token_id: str,
        from_amount: str,
        user_address: str,
        slippage: float = 0.03,
    ) -> QuoteResponse:
        self.cleanup_expired_quotes()
        validate_token_id(from_token_id, "from_token_id")
        validate_token_id(to_token_id, "to_token_id")
        validate_amount(from_amount, "from_amount")
        validate_address(user_address, "user_address")

        existing = await self._find_existing_quote(user_address, from_token_id, to_token_id, from_amount)
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

        lifi_response = await self.lifi.get_routes(
            from_chain_id=from_chain_id,
            to_chain_id=to_chain_id,
            from_token_address=from_on_chain,
            to_token_address=to_on_chain,
            from_amount=from_amount,
        )

        routes = lifi_response.get("routes", [])
        if not routes:
            raise ValueError("No routes available for this swap")
        best_route = routes[0]
        to_amount_str = best_route.get("toAmount", "0")
        to_amount_min_str = best_route.get("toAmountMin", to_amount_str)
        route_tool = None
        steps = best_route.get("steps", [])
        if steps:
            route_tool = steps[0].get("tool")

        fee_bps = self.settings.fee_bps
        to_amount_after_fee, fee_amount = calculate_fee(int(to_amount_str), fee_bps)
        to_amount_min = int(to_amount_min_str) - fee_amount

        transfer_nonce = await self.accounting.get_transfer_nonce(user_address)
        liquidity_provider = self.settings.liquidity_provider_address

        quote_id = str(uuid.uuid4())
        now = int(time.time())
        expires_at = now + self.settings.quote_ttl

        db = get_db()
        db_write(
            db,
            """INSERT INTO quotes
               (id, user_address, from_token_id, to_token_id, from_chain_id, to_chain_id,
                from_amount, to_amount_gross, to_amount_estimate, to_amount_min,
                route_tool, liquidity_provider, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                quote_id, user_address.lower(), from_token_id.lower(), to_token_id.lower(),
                from_chain_id, to_chain_id,
                from_amount, to_amount_str, str(to_amount_after_fee), str(max(to_amount_min, 0)),
                route_tool, liquidity_provider, expires_at, now,
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
            tool_used=route_tool,
            liquidity_provider=liquidity_provider,
            transfer_nonce=transfer_nonce,
            expires_at=expires_at,
        )

    async def _find_existing_quote(
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
        fee_bps = self.settings.fee_bps
        _, fee_amount = calculate_fee(int(quote["to_amount_gross"]), fee_bps)
        transfer_nonce = await self.accounting.get_transfer_nonce(user_address)

        return QuoteResponse(
            quote_id=quote["id"],
            from_token_id=quote["from_token_id"],
            to_token_id=quote["to_token_id"],
            from_chain_id=quote["from_chain_id"],
            to_chain_id=quote["to_chain_id"],
            from_amount=quote["from_amount"],
            to_amount_gross=quote["to_amount_gross"],
            to_amount_estimate=quote["to_amount_estimate"],
            to_amount_min=quote["to_amount_min"],
            fee_bps=fee_bps,
            fee_amount=str(fee_amount),
            tool_used=quote["route_tool"],
            liquidity_provider=quote["liquidity_provider"],
            transfer_nonce=transfer_nonce,
            expires_at=quote["expires_at"],
        )

    def cleanup_expired_quotes(self) -> int:
        now = int(time.time())
        if now - self._last_cleanup < CLEANUP_INTERVAL:
            return 0
        db = get_db()
        cursor = db_write(db, "DELETE FROM quotes WHERE expires_at < ?", (now,))
        self._last_cleanup = now
        deleted = cursor.rowcount
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} expired quotes")
        return deleted

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
