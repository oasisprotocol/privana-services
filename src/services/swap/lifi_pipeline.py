"""The stages of a swap filled through LiFi. The user's signed input moves to
the LP on Sapphire (input), the LP withdraws it to its wallet on the input's
chain (withdraw), LiFi swaps or bridges it (execute), the output goes back
into the LP's Privana balance (deposit) and the LP pays the user (credit).
A swap that cannot go on while its input provably never left the LP's
balance or wallet is paid back (refund); anything else parks it for a person.

Each stage only submits to a batcher or lane and checks it on a later round."""

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any, Optional

import httpx
from eth_abi import decode
from hexbytes import HexBytes
from privana.client.errors import AccountingApiError, NetworkError
from web3 import Web3

from src.clients import evm_batcher, privana_lanes
from src.clients.accounting import get_accounting_client
from src.clients.base_evm import NATIVE_TOKEN, get_evm_client, is_native
from src.clients.evm_batcher import (
    FAILED,
    GAS_RETRY_SEC,
    MINED,
    NATIVE_TRANSFER_GAS_LIMIT,
    QUEUED,
    REVERTED,
    TxIntent,
    approve_intent,
    find_evm_tx,
    transfer_intent,
)
from src.clients.lifi import LIFI_DEFAULT_SLIPPAGE_BPS, get_lifi_client
from src.clients.privana import (
    DEPOSIT_CREDITED,
    check_deposit,
    deposit_address,
    get_swap_lp_privana_client,
)
from src.clients.sapphire import DEFAULT_GAS_LIMIT
from src.core.config import load_settings
from src.core.db import db_transaction, get_db
from src.core.fees import calculate_fee
from src.core.validation import sanitize_error, scrub_urls
from src.models.swap import SwapStatus, SwapStep, SwapVenue
from src.services.swap.stage import (
    ROUND_LIMIT,
    advance,
    claim,
    fail,
    finish,
    handle_each,
    input_transfer_call,
    note,
    owner,
    still_at,
    swaps_at,
    waited,
)

logger = logging.getLogger(__name__)

LIFI_STATUS_DONE = "DONE"
LIFI_STATUS_FAILED = "FAILED"
LIFI_STATUS_INVALID = "INVALID"
LIFI_SUBSTATUS_COMPLETED = "COMPLETED"

# What each of a swap's writes is for, in its owner.
INPUT = "input"
WITHDRAW = "withdraw"
APPROVE = "approve"
LIFI = "lifi"
DEPOSIT = "deposit"
# The input deposited back into the LP's Privana balance.
REFUND = "refund"
CREDIT = "credit"
REFUND_PAYOUT = "refund_payout"
# The step a swap waits at while its wallet tx of each kind is out.
_SENT_AT = {LIFI: SwapStep.EXECUTE_SENT, REFUND: SwapStep.REFUND_REDEPOSIT, DEPOSIT: SwapStep.DEPOSIT_SENT}

# Scheduled swaps taken per round: the Sapphire sender of their input txs,
# not this, sets the pace.
CLAIM_LIMIT = 5
# The withdraw lane sends one withdrawal at a time, so one withdrawal carries
# the inputs of every swap of a token waiting for one. One that may still
# land holds the lane: a withdrawal not signed within the hour is cancelled.
WITHDRAW_QUEUE_WAIT_SEC = 3600
# Funds already landed reach the LP's wallet in minutes: an input not there
# after 30 min is redeposited, and an output or refund not there is parked.
FUNDING_WAIT_SEC = 1800
# LiFi's bridges finish in minutes; a route not done in 72 min needs a person.
BRIDGE_WAIT_SEC = 4320
# Accounting credits a deposit once it is final on its chain and swept:
# Ethereum finality plus the sweep can take most of an hour.
CREDITING_WAIT_SEC = 3600
# LiFi quotes a second apart can differ slightly: slippage is narrowed again
# from each new quote, at most this many times.
NARROW_ATTEMPTS = 3

# Emitted by the LiFi diamond for a same-chain swap (lifinance/contracts ILiFi.sol).
SWAP_COMPLETED_TOPIC = Web3.keccak(
    text="LiFiGenericSwapCompleted(bytes32,string,string,address,address,address,uint256,uint256)"
)
# Its data: integrator, referrer, receiver, fromAssetId, toAssetId, fromAmount, toAmount.
SWAP_COMPLETED_DATA = ["string", "string", "address", "address", "address", "uint256", "uint256"]


class BelowFloor(RuntimeError):
    """The price fell beyond the quote's slippage: the swap cannot pay its floor."""


async def input_round() -> None:
    if load_settings().lifi_execution_enabled:
        claim(SwapVenue.LIFI, SwapStep.INPUT_SENT, CLAIM_LIMIT)
    await handle_each(swaps_at(SwapStep.INPUT_SENT), _settle_input)


async def withdraw_round() -> None:
    _batch_withdrawals()
    await handle_each(swaps_at(SwapStep.WITHDRAW_SENT), _settle_withdraw)


def _batch_withdrawals() -> None:
    """Queue one withdrawal per token for every swap waiting without one. The
    withdrawal and its swaps are written in one transaction, with nothing
    awaited, so a swap is in exactly one withdrawal and its amount is fixed."""
    lp = load_settings().liquidity_provider_address
    db = get_db()
    with db_transaction(db):
        waiting = defaultdict(list)
        for swap in db.execute(
            "SELECT id, from_token_id, from_amount FROM swaps WHERE status = ? AND step = ? "
            "AND withdraw_batch_id IS NULL ORDER BY created_at",
            (SwapStatus.EXECUTING.value, SwapStep.WITHDRAW_SENT.value),
        ):
            waiting[swap["from_token_id"]].append(swap)
        for token_id, swaps in waiting.items():
            batch = owner(swaps[0]['id'], WITHDRAW)
            privana_lanes.submit_withdrawal(db, lp, batch, token_id, sum(int(s["from_amount"]) for s in swaps))
            db.executemany("UPDATE swaps SET withdraw_batch_id = ?, updated_at = ? WHERE id = ?",
                           [(batch, int(time.time()), s["id"]) for s in swaps])
    for token_id, swaps in waiting.items():
        logger.info("withdrawal %s queued for %d lifi swaps of %s", owner(swaps[0]["id"], WITHDRAW), len(swaps), token_id)


async def execute_round(chain_id: int) -> None:
    """The swaps whose input is on `chain_id`."""
    swaps = swaps_at(SwapStep.EXECUTE_FUNDING, SwapStep.EXECUTE_SENT, SwapStep.EXECUTE_BRIDGING, limit=-1)
    await handle_each(await _on_chain(swaps, "from_token_id", chain_id), _execute)


async def deposit_round(chain_id: int) -> None:
    """The swaps whose output is on `chain_id`."""
    swaps = swaps_at(SwapStep.DEPOSIT_SENT, SwapStep.DEPOSIT_CREDITING, limit=-1)
    await handle_each(await _on_chain(swaps, "to_token_id", chain_id), _deposit)


async def _on_chain(swaps: list[dict], token_field: str, chain_id: int) -> list[dict]:
    """The oldest ROUND_LIMIT of `swaps` on `chain_id`, filtered before the
    limit so swaps waiting on one chain never crowd out another's."""
    accounting = get_accounting_client()
    on_chain = []
    for swap in swaps:
        try:
            info = await accounting.get_token_info(swap[token_field])
        except Exception as exc:
            logger.warning("swap %s skipped this round, token lookup failed: %s", swap["id"], scrub_urls(repr(exc)))
            continue
        if info.chain_id == chain_id:
            on_chain.append(swap)
    return on_chain[:ROUND_LIMIT]


async def credit_round() -> None:
    await handle_each(swaps_at(SwapStep.CREDIT_SENT), _credit)


async def refund_round() -> None:
    await handle_each(
        swaps_at(SwapStep.REFUND_REDEPOSIT, SwapStep.REFUND_CREDITING, SwapStep.REFUND_PAYOUT), _refund)


async def _settle_input(swap: dict) -> None:
    tx = find_evm_tx(owner(swap["id"], INPUT))
    if tx is None:
        # The user's signed transfer, sent by the LP: its receipt says whether it moved.
        settings = load_settings()
        call = input_transfer_call(swap)
        evm_batcher.submit(settings.accounting_chain_id, settings.liquidity_provider_address, TxIntent(
            owner=owner(swap["id"], INPUT), to=call["to"], gas=DEFAULT_GAS_LIMIT, data=call["data"]))
        return
    if tx["status"] == MINED:
        advance(swap, SwapStep.WITHDRAW_SENT)
        return
    if tx["status"] == QUEUED or _retry_for_gas(swap, tx):
        return
    outcome = sanitize_error(tx["error"] or f"reverted as {tx['tx_hash']}")
    if not _unsent_or_reverted(tx):
        fail(swap, SwapStep.PARKED, f"input tx failed after a signed attempt: {outcome}")
        return
    # Our tx moved nothing, and a transfer signed at the input's nonce runs
    # only once the user's nonce reaches it. Past it, anyone may have sent
    # the user's signed transfer: the nonce alone never confirms the input.
    nonce = await get_accounting_client().get_transfer_nonce(swap["user_address"])
    if nonce <= int(swap["input_nonce"]):
        fail(swap, SwapStep.NO_FUNDS_MOVED, f"input transfer {outcome}")
    else:
        fail(swap, SwapStep.PARKED, f"input transfer {outcome}, but the user's nonce is past it at {nonce}")


async def _settle_withdraw(swap: dict) -> None:
    if swap["withdraw_batch_id"] is None:
        # Queued in a withdrawal at the start of the next round.
        return
    row = privana_lanes.find_privana_tx(swap["withdraw_batch_id"])
    if row["status"] == privana_lanes.LANDED:
        advance(swap, SwapStep.EXECUTE_FUNDING)
    elif row["status"] == privana_lanes.FAILED:
        # Refused with the nonce not past it: the input is still in the LP's
        # Privana balance.
        _start_refund(swap, f"withdrawal failed: {row['error']}", SwapStep.REFUND_PAYOUT)
    elif row["error"] == privana_lanes.WITHDRAW_GIVEN_UP:
        # It may still land, so the swap can neither go on nor be refunded.
        fail(swap, SwapStep.PARKED, f"withdrawal {row['id']} not landed after {row['attempts']} sends")
    elif (row["status"] == privana_lanes.QUEUED and waited(swap, WITHDRAW_QUEUE_WAIT_SEC)
          and privana_lanes.cancel_queued(swap["withdraw_batch_id"], "cancelled: not sent in time")):
        # Never signed, so it can never land: the input is still in the LP's
        # Privana balance. The other swaps in it see it FAILED next round.
        _start_refund(swap, f"withdrawal not sent after {WITHDRAW_QUEUE_WAIT_SEC} s", SwapStep.REFUND_PAYOUT)


async def _execute(swap: dict) -> None:
    if swap["step"] == SwapStep.EXECUTE_FUNDING.value:
        await _send_lifi_tx(swap)
    elif swap["step"] == SwapStep.EXECUTE_SENT.value:
        await _settle_lifi_tx(swap)
    else:
        await _settle_bridge(swap)


async def _send_lifi_tx(swap: dict) -> None:
    """Queue the swap once the wallet holds its input, and gas for a native
    input. Sent earlier, it reverts. The wallet is shared, so its balance
    does not prove this swap's withdrawal arrived."""
    if find_evm_tx(owner(swap["id"], LIFI)) is not None:
        # Queued before a restart could record it.
        advance(swap, SwapStep.EXECUTE_SENT)
        return
    if waited(swap, FUNDING_WAIT_SEC):
        _start_refund(swap, f"input not funded after {FUNDING_WAIT_SEC} s", SwapStep.REFUND_REDEPOSIT)
        return
    # Cleanup keeps the quote of a swap in flight.
    quote = dict(get_db().execute("SELECT * FROM quotes WHERE id = ?", (swap["quote_id"],)).fetchone())
    from_info, to_info = await _token_infos(swap)
    evm = get_evm_client(from_info.chain_id)
    amount, token = int(swap["from_amount"]), from_info.token_address
    if not await _holds(evm, token, swap["from_token_id"], amount):
        return
    try:
        exec_quote = await _execution_quote(swap["id"], quote, from_info, to_info, evm.address)
    except BelowFloor as exc:
        _start_refund(swap, str(exc), SwapStep.REFUND_REDEPOSIT)
        return
    try:
        lifi_tx = _swap_intent(owner(swap["id"], LIFI), from_info.chain_id, exec_quote["transactionRequest"],
                               is_native(token))
    except ValueError as exc:
        _start_refund(swap, str(exc), SwapStep.REFUND_REDEPOSIT)
        return
    # Nothing awaits between this check's read of the queued spends and the
    # submit, so another swap's spend is either counted or not queued yet.
    if not await _holds(evm, token, swap["from_token_id"], amount, lifi_tx.gas) or not still_at(swap):
        return
    # The approval and the swap are queued together, so no other swap's
    # approval for the same spender lands between them.
    intents = [lifi_tx] if is_native(token) else [
        approve_intent(owner(swap["id"], APPROVE), token, exec_quote["estimate"]["approvalAddress"], amount),
        lifi_tx,
    ]
    evm_batcher.submit(from_info.chain_id, load_settings().liquidity_provider_address, *intents)
    advance(swap, SwapStep.EXECUTE_SENT)


async def _settle_lifi_tx(swap: dict) -> None:
    tx = find_evm_tx(owner(swap["id"], LIFI))
    if tx["status"] == QUEUED:
        return
    # An approval refused for gas fails the swap tx after it unsent too.
    if tx["status"] != MINED and _retry_for_gas(swap, find_evm_tx(owner(swap["id"], APPROVE)), tx):
        return
    if tx["status"] != MINED:
        outcome = sanitize_error(tx["error"] or f"reverted as {tx['tx_hash']}")
        if _unsent_or_reverted(tx):
            _start_refund(swap, f"lifi tx {outcome}", SwapStep.REFUND_REDEPOSIT)
        else:
            fail(swap, SwapStep.PARKED, f"lifi tx failed after a signed attempt: {outcome}")
        return
    from_info, to_info = await _token_infos(swap)
    if from_info.chain_id != to_info.chain_id:
        advance(swap, SwapStep.EXECUTE_BRIDGING, lifi_tx_hash=tx["tx_hash"])
        return
    # The swap's reported output, not the wallet balance, which other swaps and gas move.
    try:
        received = _swap_output(tx["to_address"], json.loads(tx["logs"]), load_settings().liquidity_provider_address,
                                _lifi_token(to_info.token_address))
    except RuntimeError as exc:
        fail(swap, SwapStep.PARKED, str(exc))
        return
    advance(swap, SwapStep.DEPOSIT_SENT, lifi_tx_hash=tx["tx_hash"], to_amount_received=str(received))


async def _settle_bridge(swap: dict) -> None:
    """One LiFi status call per round: credits what LiFi delivered to the LP."""
    tx_hash = swap["lifi_tx_hash"]
    if waited(swap, BRIDGE_WAIT_SEC):
        fail(swap, SwapStep.PARKED, f"lifi route of {tx_hash} not done after {BRIDGE_WAIT_SEC} s")
        return
    from_info, to_info = await _token_infos(swap)
    try:
        status = await get_lifi_client().get_status(tx_hash, from_info.chain_id, to_info.chain_id)
    except httpx.HTTPStatusError as exc:
        # LiFi answers 404 until it indexes the tx.
        if exc.response.status_code != 404:
            raise
        status = {}
    try:
        received = _delivered(status, to_info, load_settings().liquidity_provider_address)
    except RuntimeError as exc:
        fail(swap, SwapStep.PARKED, f"lifi route of {tx_hash}: {exc}")
        return
    if received is not None:
        advance(swap, SwapStep.DEPOSIT_SENT, to_amount_received=str(received))


async def _deposit(swap: dict) -> None:
    amount = int(swap["to_amount_received"])
    if swap["step"] == SwapStep.DEPOSIT_CREDITING.value:
        await _check_credited(swap, DEPOSIT, amount, SwapStep.CREDIT_SENT)
        return
    tx = find_evm_tx(owner(swap["id"], DEPOSIT))
    if tx is None:
        info = await get_accounting_client().get_token_info(swap["to_token_id"])
        to = await deposit_address(await get_swap_lp_privana_client())
        # Another swap may have spent this output while it waited for its own
        # withdrawal, which refills the wallet once it arrives. This output is
        # among the queued spends, so the wallet must cover them all.
        if await _holds(get_evm_client(info.chain_id), info.token_address, swap["to_token_id"], 0,
                        NATIVE_TRANSFER_GAS_LIMIT):
            if still_at(swap):
                _submit_deposit(swap, DEPOSIT, info, amount, to)
        elif waited(swap, FUNDING_WAIT_SEC):
            fail(swap, SwapStep.PARKED, f"output not in the LP's wallet to deposit after {FUNDING_WAIT_SEC} s")
        return
    _settle_deposit(swap, tx, SwapStep.DEPOSIT_CREDITING)


async def _credit(swap: dict) -> None:
    row = privana_lanes.find_privana_tx(owner(swap["id"], CREDIT))
    if row is None:
        credited, _ = calculate_fee(int(swap["to_amount_received"]), load_settings().fee_bps)
        _pay(swap, CREDIT, swap["to_token_id"], credited)
    elif row["status"] == privana_lanes.LANDED:
        finish(swap, SwapStatus.COMPLETED, to_amount_actual=str(json.loads(row["payload"])["amount"]))
    elif row["status"] == privana_lanes.FAILED:
        fail(swap, SwapStep.PARKED, f"credit failed: {row['error']}")


def _start_refund(swap: dict, reason: str, step: SwapStep) -> None:
    """Only where the input provably never left: a withdrawal that failed
    (REFUND_PAYOUT), or a LiFi tx never queued or settled unspent
    (REFUND_REDEPOSIT, the input back from the wallet first)."""
    if advance(swap, step, SwapStatus.REFUNDING, error=reason):
        logger.info("lifi swap %s refunding: %s", swap["id"], reason)


async def _refund(swap: dict) -> None:
    amount = int(swap["from_amount"])
    if swap["step"] == SwapStep.REFUND_CREDITING.value:
        await _check_credited(swap, REFUND, amount, SwapStep.REFUND_PAYOUT)
        return
    if swap["step"] == SwapStep.REFUND_PAYOUT.value:
        _settle_refund_payout(swap, amount)
        return
    tx = find_evm_tx(owner(swap["id"], REFUND))
    if tx is not None:
        _settle_deposit(swap, tx, SwapStep.REFUND_CREDITING)
        return
    if waited(swap, FUNDING_WAIT_SEC):
        fail(swap, SwapStep.PARKED, f"input not in the LP's wallet to return after {FUNDING_WAIT_SEC} s")
        return
    info = await get_accounting_client().get_token_info(swap["from_token_id"])
    to = await deposit_address(await get_swap_lp_privana_client())
    # As at execute, nothing awaits between the queued spends read and the submit.
    if await _holds(get_evm_client(info.chain_id), info.token_address, swap["from_token_id"], amount) \
            and still_at(swap):
        _submit_deposit(swap, REFUND, info, amount, to)


def _settle_refund_payout(swap: dict, amount: int) -> None:
    row = privana_lanes.find_privana_tx(owner(swap["id"], REFUND_PAYOUT))
    if row is None:
        _pay(swap, REFUND_PAYOUT, swap["from_token_id"], amount)
    elif row["status"] == privana_lanes.LANDED:
        finish(swap, SwapStatus.REFUNDED)
    elif row["status"] == privana_lanes.FAILED:
        fail(swap, SwapStep.PARKED, f"refund failed: {row['error']}")


def _pay(swap: dict, kind: str, token_id: str, amount: int) -> None:
    """The LP's transfer of `amount` to the user, on its lane."""
    lp = load_settings().liquidity_provider_address
    privana_lanes.submit_privana(lp, privana_lanes.transfer_balance_intent(
        owner(swap["id"], kind), swap["user_address"], token_id, amount))


def _submit_deposit(swap: dict, kind: str, info: Any, amount: int, deposit_address: str) -> None:
    evm_batcher.submit(info.chain_id, load_settings().liquidity_provider_address,
                       transfer_intent(owner(swap["id"], kind), info.token_address, deposit_address, amount))


def _settle_deposit(swap: dict, tx: dict, crediting: SwapStep) -> None:
    if tx["status"] == MINED:
        advance(swap, crediting, deposit_tx_hash=tx["tx_hash"])
    elif tx["status"] != QUEUED and not _retry_for_gas(swap, tx):
        # The funds are in the LP's wallet, or may be.
        fail(swap, SwapStep.PARKED, f"deposit tx {tx['status']}: {sanitize_error(tx['error'] or tx['tx_hash'])}")


async def _check_credited(swap: dict, kind: str, amount: int, then: SwapStep) -> None:
    """One deposit check per round."""
    tx = find_evm_tx(owner(swap["id"], kind))
    try:
        status = (await check_deposit(await get_swap_lp_privana_client(), tx["chain_id"], tx["tx_hash"], amount)).status
    except (AccountingApiError, NetworkError) as exc:
        # Refused until the deposit is final on its chain.
        status = f"not accepted: {scrub_urls(str(exc))}"
        logger.warning("lifi swap %s deposit %s %s", swap["id"], tx["tx_hash"], status)
    if status == DEPOSIT_CREDITED:
        advance(swap, then)
    elif waited(swap, CREDITING_WAIT_SEC):
        fail(swap, SwapStep.PARKED, f"deposit {tx['tx_hash']} not credited after {CREDITING_WAIT_SEC} s: {status}")


def _retry_for_gas(swap: dict, *txs: Optional[dict]) -> bool:
    """Whether one of the swap's `txs` was refused because the LP could not
    pay its gas. The swap then stays where it is, and the txs that failed
    unsent are queued again once the LP has had a while to be topped up."""
    unsent = [tx for tx in txs if tx is not None and tx["status"] == FAILED and not tx["attempts"]]
    refused = [tx for tx in unsent if evm_batcher.refused_for_gas(tx)]
    if not refused:
        return False
    if int(time.time()) - max(tx["updated_at"] for tx in refused) < GAS_RETRY_SEC:
        if note(swap, f"refused for gas on chain {refused[0]['chain_id']}; retrying"):
            logger.error("lifi swap %s: %s refused for gas on chain %s; retrying in %d s", swap["id"],
                         refused[0]["owner"], refused[0]["chain_id"], GAS_RETRY_SEC)
        return True
    evm_batcher.requeue(*(tx["owner"] for tx in unsent))
    return True


def _unsent_or_reverted(tx: dict) -> bool:
    """Whether a settled tx provably spent nothing: it reverted, or failed
    before any attempt was signed. A signed attempt may have reached a node."""
    return tx["status"] == REVERTED or (tx["status"] == FAILED and not tx["attempts"])


async def _token_infos(swap: dict) -> tuple[Any, Any]:
    accounting = get_accounting_client()
    return (await accounting.get_token_info(swap["from_token_id"]),
            await accounting.get_token_info(swap["to_token_id"]))


async def _execution_quote(
    swap_id: str, quote: dict, from_info: Any, to_info: Any, from_address: str
) -> dict:
    lifi = get_lifi_client()
    request = dict(
        from_chain_id=from_info.chain_id,
        to_chain_id=to_info.chain_id,
        from_token_address=_lifi_token(from_info.token_address),
        to_token_address=_lifi_token(to_info.token_address),
        from_amount=quote["from_amount"],
        from_address=from_address,
    )
    fee_bps = load_settings().fee_bps
    floor = int(quote["to_amount_min"])
    # Quotes stored before the slippage_bps column existed were priced at LiFi's default.
    slippage_bps = quote.get("slippage_bps")
    if slippage_bps is None:
        slippage_bps = LIFI_DEFAULT_SLIPPAGE_BPS
    # The floor came from a route priced at this slippage. A different one
    # moves toAmountMin, not the price.
    exec_quote = await lifi.get_execution_quote(**request, slippage_bps=slippage_bps)
    for _ in range(NARROW_ATTEMPTS):
        if _net_min(exec_quote, fee_bps) >= floor:
            break
        # The price fell since the quote. Spend less of the tolerance so
        # the minimum LiFi enforces on chain still pays out the floor.
        narrowed_bps = _slippage_bps_to_floor(int(exec_quote["estimate"]["toAmount"]), floor, fee_bps)
        if narrowed_bps is None:
            break
        logger.info("lifi swap %s narrowing slippage to %s bps", swap_id, narrowed_bps)
        exec_quote = await lifi.get_execution_quote(**request, slippage_bps=narrowed_bps)
    net_min = _net_min(exec_quote, fee_bps)
    if net_min < floor:
        raise BelowFloor(f"execution quote below floor: net_min={net_min} floor={floor}")
    return exec_quote


async def _holds(evm: Any, token: Optional[str], token_id: str, amount: int, gas: int = 0) -> bool:
    """Whether the wallet holds `amount`, plus `gas` at the fee cap when
    `token` is the native coin that pays for it, beyond what txs already
    queued will take out of it."""
    # Read before the balance too: a tx mined during the balance read has left
    # both the balance and the queue by the second read.
    spend = _queued_spend(token_id)
    try:
        if gas and is_native(token):
            amount += await asyncio.to_thread(evm.max_gas_cost, gas)
        balance = await asyncio.to_thread(evm.balance_of, token, evm.address)
    except Exception as exc:
        logger.warning("balance or fees for %s unavailable: %s", token, scrub_urls(str(exc)))
        return False
    return balance >= amount + max(spend, _queued_spend(token_id))


def _queued_spend(token_id: str) -> int:
    """What the swaps will take out of the wallet in `token_id`: the inputs of
    queued swap txs and redeposits, and the outputs to deposit, queued or not
    yet, since they are in the wallet from the moment the swap delivers them."""
    rows = get_db().execute(
        "SELECT e.owner, e.status, e.attempts, s.step, s.from_token_id, s.to_token_id, s.from_amount, "
        "s.to_amount_received FROM evm_transactions e "
        "JOIN swaps s ON e.owner IN ('swap:' || s.id || ':' || ?, 'swap:' || s.id || ':' || ?, "
        "'swap:' || s.id || ':' || ?) WHERE e.status IN (?, ?) AND (s.from_token_id = ? OR s.to_token_id = ?)",
        (LIFI, REFUND, DEPOSIT, QUEUED, FAILED, token_id, token_id),
    ).fetchall()
    spend = 0
    for row in rows:
        # A tx that failed unsent still spends while its swap waits at the
        # step that sends it: refused for gas, it is queued again.
        if row["status"] == FAILED and (
            row["attempts"] or row["step"] != _SENT_AT[row["owner"].rsplit(":", 1)[1]].value
        ):
            continue
        if row["owner"].endswith(f":{DEPOSIT}"):
            if row["to_token_id"] == token_id:
                spend += int(row["to_amount_received"])
        elif row["from_token_id"] == token_id:
            spend += int(row["from_amount"])
    awaiting_deposit = get_db().execute(
        "SELECT s.to_amount_received FROM swaps s WHERE s.step = ? AND s.to_token_id = ? AND NOT EXISTS "
        "(SELECT 1 FROM evm_transactions e WHERE e.owner = 'swap:' || s.id || ':' || ?)",
        (SwapStep.DEPOSIT_SENT.value, token_id, DEPOSIT),
    ).fetchall()
    return spend + sum(int(row["to_amount_received"]) for row in awaiting_deposit)


def unqueued_lane_outflow(token_id: str) -> int:
    """What the swaps here have moved to a step that writes on the LP's
    lanes, the round before they queue it: a withdrawal or refund of the
    input, or a credit of the output (counted before its fee)."""
    rows = get_db().execute(
        "SELECT * FROM swaps WHERE venue = ? AND status IN (?, ?) AND step IN (?, ?, ?) "
        "AND (from_token_id = ? OR to_token_id = ?)",
        (SwapVenue.LIFI.value, SwapStatus.EXECUTING.value, SwapStatus.REFUNDING.value, SwapStep.WITHDRAW_SENT.value,
         SwapStep.REFUND_PAYOUT.value, SwapStep.CREDIT_SENT.value, token_id, token_id),
    ).fetchall()
    outflow = 0
    for swap in rows:
        if swap["step"] == SwapStep.CREDIT_SENT.value:
            kind, token, amount = CREDIT, swap["to_token_id"], swap["to_amount_received"]
        else:
            kind = WITHDRAW if swap["step"] == SwapStep.WITHDRAW_SENT.value else REFUND_PAYOUT
            token, amount = swap["from_token_id"], swap["from_amount"]
        if token == token_id and _not_on_lane(swap, kind):
            outflow += int(amount)
    return outflow


def _not_on_lane(swap: dict, kind: str) -> bool:
    """Whether the lane rows leave out the swap's `kind` of write. A failed
    withdrawal leaves its swaps' inputs in the LP's balance until each swap
    queues its refund payout."""
    if kind != WITHDRAW:
        return privana_lanes.find_privana_tx(owner(swap["id"], kind)) is None
    if swap["withdraw_batch_id"] is None:
        return True
    return privana_lanes.find_privana_tx(swap["withdraw_batch_id"])["status"] == privana_lanes.FAILED


def _swap_intent(owner: str, chain_id: int, request: dict, simulate: bool) -> TxIntent:
    # LiFi builds the calldata for one chain. Signed for another, it would go to
    # the same address there with the same value attached.
    requested = request.get("chainId")
    if requested is not None and int(requested) != chain_id:
        raise ValueError(f"transaction built for chain {requested} cannot be sent on chain {chain_id}")
    return TxIntent(
        owner=owner,
        to=request["to"],
        gas=int(request["gasLimit"], 16),
        data=request["data"],
        value=int(request.get("value", "0x0"), 16),
        simulate=simulate,
    )


def _lifi_token(token_address: Optional[str]) -> str:
    return NATIVE_TOKEN if is_native(token_address) else token_address


def _swap_output(diamond: str, logs: list, recipient: str, token: str) -> int:
    """What the swap sent to `diamond` paid `recipient` in `token`, from its
    logs. Only the called contract's log counts: anything the swap touches can
    emit the topic."""
    diamond = diamond.lower()
    for log in logs:
        topics = log["topics"]
        if (
            log["address"].lower() != diamond
            or not topics
            or HexBytes(topics[0]) != SWAP_COMPLETED_TOPIC
        ):
            continue
        _, _, receiver, _, to_asset, _, to_amount = decode(
            SWAP_COMPLETED_DATA, HexBytes(log["data"])
        )
        if receiver.lower() == recipient.lower() and to_asset.lower() == token.lower():
            return to_amount
    raise RuntimeError(f"swap logs show no LiFi swap paying {recipient} in {token}")


def _delivered(status: dict, to_info: Any, recipient: str) -> Optional[int]:
    """What LiFi's `status` of a bridge says it delivered to `recipient`, or
    None while it is not done. Anything but a full delivery raises."""
    state, substatus = status.get("status"), status.get("substatus")
    # Only DONE with COMPLETED is a full delivery. DONE with PARTIAL or
    # REFUNDED, FAILED and INVALID are final. Anything else is pending.
    if state == LIFI_STATUS_DONE and substatus == LIFI_SUBSTATUS_COMPLETED:
        receiving = status["receiving"]
        if (
            int(receiving["chainId"]) != to_info.chain_id
            or receiving["token"]["address"].lower() != _lifi_token(to_info.token_address).lower()
            or status["toAddress"].lower() != recipient.lower()
        ):
            raise RuntimeError(f"delivered elsewhere: {receiving}")
        return int(receiving["amount"])
    if state in (LIFI_STATUS_DONE, LIFI_STATUS_FAILED, LIFI_STATUS_INVALID):
        raise RuntimeError(f"ended {state} {substatus}")
    return None


def _net_min(exec_quote: dict, fee_bps: int) -> int:
    """The least the user is credited if LiFi pays out its toAmountMin."""
    net, _ = calculate_fee(int(exec_quote["estimate"]["toAmountMin"]), fee_bps)
    return net


def _slippage_bps_to_floor(to_amount: int, floor: int, fee_bps: int) -> Optional[int]:
    """The largest slippage whose toAmountMin still credits `floor` after the
    fee, or None when the expected output itself falls short of it."""
    # Smallest gross output whose net is at least the floor. The fee rounds
    # down, so the estimate can sit a unit or two above it.
    gross_min = -(-floor * 10_000 // (10_000 - fee_bps))
    while gross_min > 0 and calculate_fee(gross_min - 1, fee_bps)[0] >= floor:
        gross_min -= 1
    if to_amount < gross_min:
        return None
    # Rounded down: a smaller slippage only raises the minimum.
    return (to_amount - gross_min) * 10_000 // to_amount
