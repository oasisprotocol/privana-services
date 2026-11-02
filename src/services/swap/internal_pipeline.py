"""The internal_swap stage. A swap the LP can fill from its own Privana
balance is one `SwapManager.swap`, the user's signed input and the LP's output
both or neither, queued on the LP's transfer lane."""

import asyncio
import logging
import time
from functools import cache
from typing import Optional

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
from src.core.db import get_db
from src.core.fee_policy import resolve_internal_fee
from src.core.fees import calculate_fee
from src.core.validation import sanitize_error, scrub_urls
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
    note,
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
        if row["error"] == FOREIGN_SPEND:
            fail(swap, SwapStep.PARKED, f"swap failed: {row['error']}")
        else:
            # Both transfers or neither: the swap itself moved nothing.
            await _fail_unless_input_spent(swap, f"swap failed: {row['error']}")


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
            await _fail_unless_input_spent(swap, f"input transfer would revert: {sanitize_error(str(exc))}", exc)
            continue
        except Exception as exc:
            await _defer(swap, f"input simulation unavailable: {sanitize_error(str(exc))}")
            continue
        candidates.append(swap)
    prices = await asyncio.gather(*(_fresh_payout(swap) for swap in candidates), return_exceptions=True)
    priced = []
    for swap, price in zip(candidates, prices):
        if isinstance(price, PricingUnavailable):
            await _defer(swap, f"no price: {price}")
        elif isinstance(price, BaseException):
            await _fail_unless_input_spent(swap, sanitize_error(str(price)), price)
        else:
            priced.append((swap, price))
    available, unavailable = await _lp_available([swap["to_token_id"] for swap, _ in priced])
    # Nothing awaits from the reads in _lp_available to the last advance, so
    # an outflow queued meanwhile is always counted.
    short, deferred = [], []
    for swap, price in priced:
        token = swap["to_token_id"]
        if token not in available:
            deferred.append((swap, f"LP balance unavailable: {unavailable[token]}"))
        elif available[token] < price:
            short.append((swap, f"Insufficient liquidity for this swap: {available[token]} < {price}"))
        else:
            available[token] -= price
            advance(swap, SwapStep.INTERNAL_SWAP_SENT, to_amount_executed=str(price))
    for swap, reason in deferred:
        await _defer(swap, reason)
    for swap, error in short:
        await _fail_unless_input_spent(swap, error)


async def _lp_available(tokens: list[str]) -> tuple[dict[str, int], dict[str, str]]:
    """What the LP can still pay in each of `tokens`: its balance less what its
    lanes will take out, and why a balance could not be read."""
    lp = load_settings().liquidity_provider_address
    tokens = list(dict.fromkeys(tokens))

    def outflow(token: str) -> int:
        return lane_outflow(lp, token) + unqueued_lane_outflow(token)

    before = {token: outflow(token) for token in tokens}
    balances = await asyncio.gather(*(get_accounting_client().get_lp_balance(token) for token in tokens),
                                    return_exceptions=True)
    available, unavailable = {}, {}
    for token, balance in zip(tokens, balances):
        if isinstance(balance, BaseException):
            unavailable[token] = sanitize_error(str(balance))
        else:
            # An outflow landing during the read has left the lane but maybe
            # not the balance read: the larger of the two counts it.
            available[token] = int(balance.balance) - max(before[token], outflow(token))
    return available, unavailable


async def _defer(swap: dict, reason: str) -> None:
    """Price it at a later round, or fail it once it has waited too long."""
    if waited(swap, PRICING_GIVE_UP_SEC):
        await _fail_unless_input_spent(swap, f"{reason} for {PRICING_GIVE_UP_SEC} s")
        return
    if note(swap, reason):
        logger.info("internal swap %s waits: %s", swap["id"], reason)


async def _fail_unless_input_spent(swap: dict, error: str, exc: Optional[BaseException] = None) -> None:
    """Fail a claimed swap that did not go through. Its user's swaps wait for
    it, so their transfer nonce moves only by a transfer signed at it: then
    the input may have been sent to the LP without the swap, and a person has
    to settle it. While the nonce is unread, it is tried again next round."""
    try:
        nonce = await get_accounting_client().get_transfer_nonce(swap["user_address"])
    except Exception as read_exc:
        logger.warning("internal swap %s not failed this round, transfer nonce unavailable: %s",
                       swap["id"], scrub_urls(repr(read_exc)))
        return
    if nonce <= int(swap["input_nonce"]):
        fail(swap, SwapStep.NO_FUNDS_MOVED, error, exc)
    else:
        fail(swap, SwapStep.PARKED, f"{error}, but the user's nonce is past it at {nonce}", exc)


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
