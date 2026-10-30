"""The internal_swap stage. A swap the LP can fill from its own Privana
balance is one `SwapManager.swap`, the user's signed input and the LP's output
both or neither, queued on the LP's transfer lane."""

import asyncio
import logging
import time
from functools import cache

from web3.exceptions import ContractLogicError

from src.clients.accounting import get_accounting_client
from src.clients.base_evm import EvmClient
from src.clients.evm_batcher import find_evm_tx
from src.clients.lifi import LIFI_DEFAULT_SLIPPAGE_BPS
from src.clients.privana_lanes import (
    FAILED,
    FOREIGN_SPEND,
    LANDED,
    find_privana_tx,
    lane_evm_owner,
    lane_outflow,
    submit_privana,
    swap_intent,
)
from src.core.config import load_settings
from src.core.db import db_write, get_db
from src.core.fee_policy import resolve_internal_fee
from src.core.fees import calculate_fee
from src.core.validation import sanitize_error
from src.models.swap import SwapStatus, SwapStep, SwapVenue
from src.services.swap.lifi_pipeline import unqueued_lane_outflow
from src.services.swap.quote_service import get_quote_service
from src.services.swap.stage import (
    advance,
    claim,
    fail,
    finish,
    handle_each,
    input_transfer_call,
    owner,
    swaps_at,
    waited,
)

logger = logging.getLogger(__name__)

INTERNAL = "internal"
# The most swaps being priced at once: each one is a LiFi price call.
CLAIM_LIMIT = 10
# Each swap is re-priced when it runs, so a quote can be used long after it
# was made. A slow price holds up the round's other swaps.
REPRICE_TIMEOUT_SEC = 5
# A claimed swap that cannot be priced or simulated waits, holding its
# user's nonce, and fails once it has waited this long: ten minutes covers a
# LiFi or RPC blip without holding the user's other swaps for longer.
PRICING_GIVE_UP_SEC = 600


class PricingUnavailable(RuntimeError):
    """No current price for the swap; nothing about the swap itself is wrong."""


async def internal_swap_round() -> None:
    # Every sent swap, so each has its lane row before pricing counts the lane.
    await handle_each(swaps_at(SwapStep.INTERNAL_SWAP_SENT, limit=-1), _settle)
    pricing = swaps_at(SwapStep.INTERNAL_SWAP_PRICING, limit=CLAIM_LIMIT)
    claim(SwapVenue.INTERNAL, SwapStep.INTERNAL_SWAP_PRICING, CLAIM_LIMIT - len(pricing))
    await _price(swaps_at(SwapStep.INTERNAL_SWAP_PRICING, limit=CLAIM_LIMIT))


async def _settle(swap: dict) -> None:
    row = find_privana_tx(owner(swap["id"], INTERNAL))
    if row is None:
        submit_privana(load_settings().liquidity_provider_address, swap_intent(
            owner(swap["id"], INTERNAL), swap["user_address"], swap["from_token_id"], int(swap["from_amount"]),
            int(swap["input_nonce"]), swap["input_signature"], swap["to_token_id"], int(swap["to_amount_executed"]),
        ))
    elif row["status"] == LANDED:
        finish(swap, SwapStatus.COMPLETED, to_amount_actual=swap["to_amount_executed"],
               swap_tx_hash=find_evm_tx(lane_evm_owner(row))["tx_hash"])
    elif row["status"] == FAILED:
        # Both transfers or neither: a swap that did not land moved nothing.
        fail(swap, SwapStep.PARKED if row["error"] == FOREIGN_SPEND else SwapStep.NO_FUNDS_MOVED,
             f"swap failed: {row['error']}")


async def _price(swaps: list[dict]) -> None:
    """Price each claimed swap now and check the LP can pay it. Its lane row
    is submitted at the next round, before that round counts the lane's
    outflows."""
    candidates = []
    for swap in swaps:
        # Simulated before it is priced: only a swap whose input would move
        # is worth an external price lookup.
        call = {"from": load_settings().liquidity_provider_address, **input_transfer_call(swap)}
        try:
            await asyncio.to_thread(_sapphire_lp().simulate, call)
        except ContractLogicError as exc:
            fail(swap, SwapStep.NO_FUNDS_MOVED, f"input transfer would revert: {sanitize_error(str(exc))}", exc)
            continue
        except Exception as exc:
            _defer(swap, f"input simulation unavailable: {sanitize_error(str(exc))}")
            continue
        candidates.append(swap)
    prices = await asyncio.gather(*(_fresh_payout(swap) for swap in candidates), return_exceptions=True)
    available: dict[str, int] = {}
    for swap, price in zip(candidates, prices):
        if isinstance(price, PricingUnavailable):
            _defer(swap, f"no price: {price}")
            continue
        if isinstance(price, BaseException):
            fail(swap, SwapStep.NO_FUNDS_MOVED, sanitize_error(str(price)), price)
            continue
        token = swap["to_token_id"]
        if token not in available:
            try:
                balance = int((await get_accounting_client().get_lp_balance(token)).balance)
            except Exception as exc:
                _defer(swap, f"LP balance unavailable: {sanitize_error(str(exc))}")
                continue
            available[token] = (balance - lane_outflow(load_settings().liquidity_provider_address, token)
                                - unqueued_lane_outflow(token))
        if available[token] < price:
            fail(swap, SwapStep.NO_FUNDS_MOVED, f"Insufficient liquidity for this swap: {available[token]} < {price}")
            continue
        available[token] -= price
        advance(swap, SwapStep.INTERNAL_SWAP_SENT, to_amount_executed=str(price))


def _defer(swap: dict, reason: str) -> None:
    """Price it at a later round, or fail it once it has waited too long."""
    if waited(swap, PRICING_GIVE_UP_SEC):
        fail(swap, SwapStep.NO_FUNDS_MOVED, f"{reason} for {PRICING_GIVE_UP_SEC} s")
        return
    if swap["error"] != reason:
        db_write(get_db(), "UPDATE swaps SET error = ? WHERE id = ?", (reason, swap["id"]))
        logger.info("internal swap %s waits: %s", swap["id"], reason)


@cache
def _sapphire_lp() -> EvmClient:
    """The LP's Sapphire account, for simulations only: its sender sends."""
    settings = load_settings()
    return EvmClient(settings.sapphire_rpc_url, settings.liquidity_provider_secret_key,
                     settings.sapphire_rpc_headers)


async def _fresh_payout(swap: dict) -> int:
    """The output at the current price, net of the fee due now. Below the
    quote's floor the price moved beyond its slippage and the swap fails."""
    quote = get_db().execute(
        "SELECT to_amount_min, slippage_bps FROM quotes WHERE id = ?", (swap["quote_id"],)
    ).fetchone()
    if quote is None:
        raise ValueError("Scheduled swap quote not found")
    quotes = get_quote_service()
    try:
        from_info = await quotes.accounting.get_token_info(swap["from_token_id"])
        to_info = await quotes.accounting.get_token_info(swap["to_token_id"])
        routes = await asyncio.wait_for(
            quotes._price_route(
                from_info, to_info, swap["from_amount"],
                quote["slippage_bps"] or LIFI_DEFAULT_SLIPPAGE_BPS,
            ),
            REPRICE_TIMEOUT_SEC,
        )
    except Exception as exc:
        raise PricingUnavailable(type(exc).__name__) from exc
    if not routes.get("routes"):
        raise PricingUnavailable("no route")
    route = routes["routes"][0]
    # Checked again at the current price: a quote can be used long after it was made.
    quotes._enforce_max_swap_size(route)
    # Fees are those due now, so an exemption that has ended no longer applies.
    decision = resolve_internal_fee(swap["user_address"], int(time.time()))
    net, _ = calculate_fee(int(route["toAmount"]), decision.fee_bps)
    floor = int(quote["to_amount_min"])
    if net < floor:
        raise ValueError(f"execution quote below floor: net={net} floor={floor}")
    return net
