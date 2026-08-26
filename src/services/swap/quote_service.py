import json
import logging
import time
import uuid
from typing import Optional

from src.clients.accounting import get_accounting_client
from src.clients.lifi import get_lifi_client
from src.core.config import load_settings
from src.core.db import db_write, get_db
from src.core.fee_policy import FeeDecision, resolve_internal_fee
from src.core.fees import calculate_fee
from src.core.validation import validate_address, validate_amount, validate_token_id
from src.models.api import QuoteResponse
from src.models.swap import SwapVenue

logger = logging.getLogger(__name__)


CLEANUP_INTERVAL = 60


def _parse_token_map(raw: str) -> dict:
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse LIFI_TOKEN_MAP, ignoring")
        return {}


class QuoteService:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.accounting = get_accounting_client()
        self.lifi = get_lifi_client()
        self._last_cleanup = 0
        self._token_map = _parse_token_map(self.settings.lifi_token_map)

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

        lifi_from_chain = from_chain_id
        lifi_to_chain = to_chain_id
        lifi_from_token = from_on_chain
        lifi_to_token = to_on_chain

        chain_key = str(from_chain_id)
        if chain_key in self._token_map:
            mapping = self._token_map[chain_key]
            lifi_from_chain = mapping.get("chain_id", from_chain_id)
            lifi_from_token = mapping.get("tokens", {}).get(from_on_chain, from_on_chain)
        chain_key = str(to_chain_id)
        if chain_key in self._token_map:
            mapping = self._token_map[chain_key]
            lifi_to_chain = mapping.get("chain_id", to_chain_id)
            lifi_to_token = mapping.get("tokens", {}).get(to_on_chain, to_on_chain)

        lifi_response = await self.lifi.get_routes(
            from_chain_id=lifi_from_chain,
            to_chain_id=lifi_to_chain,
            from_token_address=lifi_from_token,
            to_token_address=lifi_to_token,
            from_amount=from_amount,
        )

        routes = lifi_response.get("routes", [])
        if not routes:
            raise ValueError("No routes available for this swap")
        best_route = routes[0]
        self._enforce_max_swap_size(best_route)
        to_amount_str = best_route.get("toAmount", "0")
        to_amount_min_str = best_route.get("toAmountMin", to_amount_str)
        route_tool = None
        steps = best_route.get("steps", [])
        if steps:
            route_tool = steps[0].get("tool")

        now = int(time.time())
        # The internal-candidate fee must be resolved before the venue check:
        # the LP has to cover the user's payout, and a fee exemption raises
        # that payout to the full gross amount.
        decision = resolve_internal_fee(user_address, now)
        to_amount_after_fee, fee_amount = calculate_fee(int(to_amount_str), decision.fee_bps)

        liquidity_provider = self.settings.liquidity_provider_address
        lp_balance = await self.accounting.get_lp_balance(to_token_id)
        venue = SwapVenue.INTERNAL.value
        if int(lp_balance.balance) < to_amount_after_fee:
            venue = await self._select_lifi_venue_or_raise(
                from_chain_id, to_chain_id, from_on_chain, to_on_chain, from_amount
            )
            # Exemptions never apply to LiFi routed swaps.
            decision = FeeDecision(fee_bps=self.settings.fee_bps)
            to_amount_after_fee, fee_amount = calculate_fee(
                int(to_amount_str), decision.fee_bps
            )
        to_amount_min = int(to_amount_min_str) - fee_amount

        transfer_nonce = await self.accounting.get_transfer_nonce(user_address)

        quote_id = str(uuid.uuid4())
        expires_at = now + self.settings.quote_ttl
        if decision.valid_until is not None:
            expires_at = min(expires_at, decision.valid_until)

        db = get_db()
        db_write(
            db,
            """INSERT INTO quotes
               (id, user_address, from_token_id, to_token_id, from_chain_id, to_chain_id,
                from_amount, to_amount_gross, to_amount_estimate, to_amount_min,
                route_tool, liquidity_provider, expires_at, created_at, venue)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                quote_id, user_address.lower(), from_token_id.lower(), to_token_id.lower(),
                from_chain_id, to_chain_id,
                from_amount, to_amount_str, str(to_amount_after_fee), str(max(to_amount_min, 0)),
                route_tool, liquidity_provider, expires_at, now, venue,
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
            fee_bps=decision.fee_bps,
            fee_amount=str(fee_amount),
            fee_policy_id=decision.policy_id,
            tool_used=route_tool,
            liquidity_provider=liquidity_provider,
            transfer_nonce=transfer_nonce,
            expires_at=expires_at,
            venue=venue,
        )

    def _enforce_max_swap_size(self, route: dict) -> None:
        """Apply the ``MAX_SWAP_AMOUNT_USD`` per-swap cap to every venue.

        A cap of 0 disables the check. When Li.Fi does not price the route we
        let it through rather than rejecting on missing data, and say so in
        the log so a silently unenforced cap is visible.
        """
        cap = self.settings.max_swap_amount_usd
        if cap <= 0:
            return

        from_amount_usd = route.get("fromAmountUSD")
        if from_amount_usd is None:
            logger.warning(
                "Route has no fromAmountUSD; MAX_SWAP_AMOUNT_USD=%d not enforced for this quote",
                cap,
            )
            return

        if float(from_amount_usd) > cap:
            raise ValueError(f"Swap size exceeds the maximum of {cap} USD")

    async def _select_lifi_venue_or_raise(
        self,
        from_chain_id: int,
        to_chain_id: int,
        from_token_address: str,
        to_token_address: str,
        from_amount: str,
    ) -> str:
        if not self.settings.lifi_execution_enabled:
            raise ValueError("Insufficient liquidity for this swap")
        real_routes = await self.lifi.get_routes(
            from_chain_id=from_chain_id,
            to_chain_id=to_chain_id,
            from_token_address=from_token_address,
            to_token_address=to_token_address,
            from_amount=from_amount,
        )
        if not real_routes.get("routes"):
            raise ValueError("Insufficient liquidity for this swap")
        cap = self.settings.lifi_max_swap_amount_usd
        from_amount_usd = real_routes["routes"][0].get("fromAmountUSD")
        if cap > 0 and from_amount_usd is not None and float(from_amount_usd) > cap:
            raise ValueError("Swap size exceeds LiFi routing limit")
        return SwapVenue.LIFI.value

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
        # Re-resolve the fee rather than storing it: the policy config is the
        # source of truth and cannot have changed under a quote that is still
        # valid, since a quote's expiry is clamped to its policy window at
        # issuance. An exemption only ever applies to an internal fill; a LiFi
        # quote always carries the global fee.
        if quote["venue"] == SwapVenue.INTERNAL.value:
            decision = resolve_internal_fee(user_address, now)
        else:
            decision = FeeDecision(fee_bps=self.settings.fee_bps)
        fee_bps = decision.fee_bps
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
            fee_policy_id=decision.policy_id,
            tool_used=quote["route_tool"],
            liquidity_provider=quote["liquidity_provider"],
            transfer_nonce=transfer_nonce,
            expires_at=quote["expires_at"],
            venue=quote["venue"],
        )

    def cleanup_expired_quotes(self) -> int:
        now = int(time.time())
        if now - self._last_cleanup < CLEANUP_INTERVAL:
            return 0
        db = get_db()
        cursor = db_write(db, "DELETE FROM quotes WHERE expires_at <= ?", (now,))
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
