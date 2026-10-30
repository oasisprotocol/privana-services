"""LiFi swaps through the LP's real senders and lanes on the fake chains."""

import asyncio
import threading
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from privana.client.errors import AccountingApiError, NetworkError
from web3 import Web3
from web3.exceptions import Web3RPCError

import src.clients.evm_batcher as evm_batcher
import src.clients.privana_lanes as privana_lanes
import src.services.swap.lifi_pipeline as lifi_pipeline
import src.services.swap.stage as stage
from src.core.db import db_write
from tests.clients.fake_chain import SAPPHIRE_CHAIN_ID, decode, nonce_of
from tests.services.swap.world import (
    AMOUNT,
    BASE,
    DIAMOND,
    ETH_WETH,
    ETHEREUM,
    TOKENS,
    USDC,
    USERS,
    WETH,
    World,
)

USER = USERS[0]
# The swap's 60_000 output, less the 10 bps fee.
NET = 59_940
ONE_OF_EACH = ["input", "approve", "lifi", "deposit"]


@pytest.fixture
def world(settings, test_db, monkeypatch):
    return World(settings, test_db, monkeypatch)


def lp(world):
    return world.lp_address.lower()


async def test_a_swap_moves_each_leg_once_and_credits_the_user_net_of_the_fee(world):
    swap_id = await world.schedule("lifi")

    swap = await world.until(swap_id, "completed")

    assert swap["to_amount_actual"] == str(NET)
    assert world.evm_owners(swap_id) == ONE_OF_EACH
    assert len(world.api.requests) == 1
    assert world.ledger.landed == [(USER.address.lower(), lp(world), 0), (lp(world), USER.address.lower(), 0)]
    assert world.paid(USER) == [(WETH, NET)]
    assert not any(world.wallets[BASE].balances.values())


async def test_a_chain_whose_rpc_hangs_holds_only_its_own_swaps(world):
    on_base = await world.schedule("lifi")
    on_ethereum = await world.schedule("lifi", user=USERS[1], from_token=ETH_WETH, to_token=USDC)
    for _ in range(10):
        await world.stage_rounds("input", "withdraw")
        for sender in world.senders.values():
            sender.run_round()
        await world.withdraw_lane.run_round()
    assert {world.swap(s)["step"] for s in (on_base, on_ethereum)} == {"execute:funding"}
    released = threading.Event()
    world.senders[ETHEREUM].evm.balance_of = lambda *_: released.wait(10)

    try:
        await asyncio.wait_for(lifi_pipeline.execute_round(BASE), 5)
    finally:
        released.set()

    assert world.swap(on_base)["step"] == "execute:sent"
    assert world.swap(on_ethereum)["step"] == "execute:funding"


async def test_an_output_waiting_for_its_deposit_never_funds_another_swap(world):
    world.lifi.to_amount = AMOUNT
    first = await world.schedule("lifi")

    async def run(*stages):
        await world.stage_rounds(*stages)
        for sender in world.senders.values():
            sender.run_round()
        await world.withdraw_lane.run_round()

    for _ in range(10):
        await run("input", "withdraw", "execute")
    world.api.relay_paused = True
    second = await world.schedule("lifi", user=USERS[1], from_token=WETH, to_token=USDC)
    for _ in range(10):
        await run("input", "withdraw", "execute")
    # The first swap's WETH is in the wallet for its deposit; the second's
    # withdrawal landed but its WETH has not arrived.
    assert (world.swap(first)["step"], world.swap(second)["step"]) == ("deposit:sent", "execute:funding")
    assert "lifi" not in world.evm_owners(second)

    world.wallets[BASE].balances[TOKENS[WETH].token_address.lower()] += AMOUNT
    await world.until(first, "completed")
    assert (await world.until(second, "completed"))["to_amount_actual"]


async def test_an_output_another_swap_spent_while_bridging_is_deposited_once_the_wallet_refills(world):
    world.lifi.to_amount = AMOUNT
    world.lifi.status = {"status": "PENDING"}
    bridged = await world.schedule("lifi", user=USERS[1], from_token=ETH_WETH, to_token=USDC)
    await world.until(bridged, "execute:bridging")
    world.api.relay_paused = True
    # Its withdrawal landed but its USDC has not arrived: the bridged USDC,
    # already in the wallet, funds it.
    spender = await world.schedule("lifi")
    await world.until(spender, "deposit:sent")
    world.lifi.status = None

    await world.run(5)

    assert (world.swap(bridged)["status"], world.swap(bridged)["step"]) == ("executing", "deposit:sent")
    world.wallets[BASE].balances[TOKENS[USDC].token_address.lower()] += AMOUNT
    await world.until(bridged, "completed")
    await world.until(spender, "completed")


async def test_an_input_about_to_be_withdrawn_never_pays_an_internal_swap(world):
    lifi = await world.schedule("lifi", user=USERS[1], from_token=WETH, to_token=USDC)
    for _ in range(10):
        await world.stage_rounds("input")
        world.senders[SAPPHIRE_CHAIN_ID].run_round()
    # The input is the LP's only WETH, and its withdrawal is not queued yet.
    assert world.swap(lifi)["step"] == "withdraw:sent" and "withdraw" not in world.lane_rows(lifi)
    internal = await world.schedule("internal")

    await world.stage_rounds("internal_swap")

    assert world.swap(internal)["error"].startswith("Insufficient liquidity")
    assert (await world.until(lifi, "completed"))["to_amount_actual"]


def _crash_once(monkeypatch, module, name, kind, after):
    """`module.name` dies once for the swap's `kind` of write, as a restart
    would cut it: before it submitted, or right after."""
    real = getattr(module, name)
    crashed = []

    def submit(*args):
        owners = [arg.owner if hasattr(arg, "owner") else arg for arg in args]
        hit = not crashed and any(isinstance(o, str) and o.endswith(f":{kind}") for o in owners)
        if hit and not after:
            crashed.append(kind)
            raise RuntimeError("process killed")
        result = real(*args)
        if hit:
            crashed.append(kind)
            raise RuntimeError("process killed")
        return result

    monkeypatch.setattr(module, name, submit)
    return crashed


@pytest.mark.parametrize("after", [False, True], ids=["before submit", "after submit"])
@pytest.mark.parametrize("module,name,kind", [
    (evm_batcher, "submit", "input"),
    (privana_lanes, "submit_withdrawal", "withdraw"),
    (evm_batcher, "submit", "lifi"),
    (evm_batcher, "submit", "deposit"),
    (privana_lanes, "submit_privana", "credit"),
])
async def test_a_restart_cutting_any_stage_short_sends_nothing_twice(world, monkeypatch, module, name, kind, after):
    crashed = _crash_once(monkeypatch, module, name, kind, after)
    swap_id = await world.schedule("lifi")

    for _ in range(30):
        if world.swap(swap_id)["status"] == "completed":
            break
        await world.run()
        if crashed:
            world.restart()

    assert crashed == [kind]
    assert world.swap(swap_id)["status"] == "completed"
    assert world.evm_owners(swap_id) == ONE_OF_EACH
    assert len(world.api.requests) == 1
    assert world.paid(USER) == [(WETH, NET)]


async def test_restarting_between_every_round_completes_with_one_of_each(world):
    swap_id = await world.schedule("lifi")

    for _ in range(30):
        if world.swap(swap_id)["status"] == "completed":
            break
        world.restart()
        await world.run()

    assert world.swap(swap_id)["status"] == "completed"
    assert world.evm_owners(swap_id) == ONE_OF_EACH
    assert world.paid(USER) == [(WETH, NET)]


async def test_an_input_whose_send_reply_was_lost_is_settled_by_its_receipt(world):
    # The node took the user's transfer, but its reply never came back.
    sapphire = world.sapphire

    def reply_lost(raw):
        sapphire.on_send = None
        sapphire.sent.append(raw)
        sapphire.pool[nonce_of(raw)] = raw
        raise ConnectionError("read timed out")

    sapphire.on_send = reply_lost
    swap_id = await world.schedule("lifi")

    swap = await world.until(swap_id, "completed")

    assert swap["to_amount_actual"] == str(NET)
    (row,) = [r for r in world.db.execute("SELECT * FROM evm_transactions WHERE owner LIKE '%:input'")]
    assert len(row["attempts"].split()) == 1
    assert world.ledger.landed[0] == (USER.address.lower(), lp(world), 0)


async def test_an_input_that_reverted_with_the_users_nonce_unspent_moved_no_funds(world):
    world.ledger.reverting_when_mined.add(lp(world))
    swap_id = await world.schedule("lifi")

    swap = await world.until(swap_id, "failed")

    assert swap["step"] == "no_funds_moved"
    assert world.evm_owners(swap_id) == ["input"]
    assert world.api.requests == []
    assert world.ledger.landed == []


async def test_an_input_that_reverted_once_the_users_nonce_moved_parks(world):
    # Anyone can send the user's signed transfer: the nonce alone proves nothing.
    world.ledger.reverting_when_mined.add(lp(world))
    swap_id = await world.schedule("lifi")
    await world.run()
    world.ledger.transfer_nonces[USER.address.lower()] = 1

    swap = await world.until(swap_id, "failed")

    assert swap["step"] == "parked"
    assert world.api.requests == []


async def test_an_input_signed_past_the_users_nonce_moved_no_funds(world):
    # A transfer runs only at its signer's current nonce: one not reached yet never ran.
    swap_id = await world.schedule("lifi", nonce=1)

    swap = await world.until(swap_id, "failed")

    assert swap["step"] == "no_funds_moved"
    assert world.ledger.landed == []


async def test_a_withdrawal_that_lands_while_its_reply_is_lost_moves_on_without_a_second(world):
    world.api.replies = [(True, NetworkError("Request timeout after 30s"))]
    swap_id = await world.schedule("lifi")

    await world.until(swap_id, "completed")

    assert len(world.api.requests) == 1
    assert world.lane_rows(swap_id)["withdraw"]["status"] == "landed"
    assert world.paid(USER) == [(WETH, NET)]


async def test_a_withdrawal_the_lane_stops_resending_parks_its_swap(world):
    world.api.replies = [(False, NetworkError("Request timeout after 30s"))] * (privana_lanes.WITHDRAW_MAX_SENDS + 5)
    swap_id = await world.schedule("lifi")
    await world.until(swap_id, "withdraw:sent")
    for _ in range(privana_lanes.WITHDRAW_MAX_SENDS + 2):
        db_write(world.db, "UPDATE privana_transactions SET updated_at = updated_at - ? WHERE kind = 'withdraw'",
                 (privana_lanes.WITHDRAW_RESEND_SEC,))
        await world.run()

    swap = world.swap(swap_id)
    assert (swap["status"], swap["step"]) == ("failed", "parked")
    assert len(world.api.requests) == privana_lanes.WITHDRAW_MAX_SENDS
    assert world.paid(USER) == []


async def test_a_refused_withdrawal_pays_the_input_back_from_the_lps_balance(world):
    world.api.replies = [(False, AccountingApiError("API request failed: 400 Bad Request", 400, "no balance"))]
    swap_id = await world.schedule("lifi")
    await world.until(swap_id, "withdraw:sent")
    assert len(world.api.requests) == 1
    db_write(world.db, "UPDATE privana_transactions SET updated_at = updated_at - ? WHERE kind = 'withdraw'",
             (privana_lanes.WITHDRAW_RESEND_SEC,))

    swap = await world.until(swap_id, "refunded")

    assert len(world.api.requests) == 1
    assert world.evm_owners(swap_id) == ["input"]
    assert world.paid(USER) == [(USDC, AMOUNT)]
    assert swap["error"].startswith("withdrawal failed")


async def test_a_reverted_lifi_tx_deposits_the_input_back_and_refunds_it_once(world):
    world.lifi.reverts = True
    swap_id = await world.schedule("lifi")

    await world.until(swap_id, "refunded")
    await world.run(3)

    assert world.evm_owners(swap_id) == ["input", "approve", "lifi", "refund"]
    assert world.paid(USER) == [(USDC, AMOUNT)]
    assert not any(world.wallets[BASE].balances.values())


async def test_a_lifi_tx_the_node_refuses_outright_refunds_the_input(world):
    # Refused before any node took it, so the input never left the wallet.
    def refuse_the_swap(raw):
        if Web3.to_checksum_address(decode(raw)["to"]) == DIAMOND:
            raise Web3RPCError("insufficient funds for gas * price + value")

    world.chains[BASE].on_send = refuse_the_swap
    swap_id = await world.schedule("lifi")

    await world.until(swap_id, "refunded")

    assert world.evm_owners(swap_id) == ["input", "approve", "lifi", "refund"]
    assert world.paid(USER) == [(USDC, AMOUNT)]


async def test_a_price_below_the_floor_refunds_before_anything_is_sent(world):
    world.lifi.to_amount = 50_000
    swap_id = await world.schedule("lifi", floor=55_000)

    swap = await world.until(swap_id, "refunded")

    assert "below floor" in swap["error"]
    assert world.evm_owners(swap_id) == ["input", "refund"]
    assert world.paid(USER) == [(USDC, AMOUNT)]


async def test_a_bridge_credits_what_lifi_delivered_on_the_output_chain(world):
    swap_id = await world.schedule("lifi", to_token=ETH_WETH)

    swap = await world.until(swap_id, "completed")

    deposit = world.db.execute("SELECT * FROM evm_transactions WHERE owner LIKE '%:deposit'").fetchone()
    assert deposit["chain_id"] == ETHEREUM
    assert world.paid(USER) == [(ETH_WETH, NET)]
    assert swap["lifi_tx_hash"] is not None
    assert world.lifi.status_calls == 1


@pytest.mark.parametrize("status", [
    {"status": "FAILED"},
    {"status": "DONE", "substatus": "REFUNDED"},
    {"status": "DONE", "substatus": "PARTIAL"},
], ids=["failed", "refunded", "partial"])
async def test_a_bridge_that_did_not_deliver_parks_without_refunding(world, status):
    world.lifi.status = status
    swap_id = await world.schedule("lifi", to_token=ETH_WETH)

    swap = await world.until(swap_id, "failed")
    await world.run(3)

    assert swap["step"] == "parked"
    assert world.evm_owners(swap_id) == ["input", "approve", "lifi"]
    assert world.paid(USER) == []


async def test_an_input_that_never_reaches_the_wallet_is_never_paid_back(world):
    # Withdrawn from the LP's Privana balance: only a redeposit could refund it.
    world.api.relay_paused = True
    swap_id = await world.schedule("lifi")
    await world.until(swap_id, "execute:funding")
    await world.run(3)
    assert world.lifi.quotes == {}

    world.age(swap_id, lifi_pipeline.FUNDING_WAIT_SEC)
    await world.run(3)
    assert world.swap(swap_id)["step"] == "refund:redeposit"
    world.age(swap_id, lifi_pipeline.FUNDING_WAIT_SEC)
    swap = await world.until(swap_id, "failed")

    assert swap["step"] == "parked"
    assert world.evm_owners(swap_id) == ["input"]
    assert world.paid(USER) == []


async def test_an_input_another_swaps_queued_tx_will_spend_is_not_counted_as_funding(world):
    # The first swap's input is in the wallet, held by its queued swap tx.
    no_base = {SAPPHIRE_CHAIN_ID, ETHEREUM}
    first = await world.schedule("lifi", user=USERS[0])
    await world.until(first, "execute:sent", senders=no_base)
    world.api.relay_paused = True
    second = await world.schedule("lifi", user=USERS[1])
    await world.until(second, "execute:funding", senders=no_base)

    await world.run(3, senders=no_base)

    assert world.swap(second)["step"] == "execute:funding"
    assert world.evm_owners(second) == ["input"]


async def test_a_credit_unconfirmed_for_a_while_is_never_signed_again(world):
    # The old path re-signed a payout whose confirmation timed out.
    swap_id = await world.schedule("lifi")
    await world.until(swap_id, "deposit:crediting")
    world.sapphire.auto_mine = False
    await world.run(5)
    assert world.lane_rows(swap_id)["credit"]["status"] == "sent"
    world.ledger.set_lp_balance(WETH, 10**6)
    internal = await world.schedule("internal", user=USERS[1])
    await world.run(3)
    assert world.swap(swap_id)["status"] == "executing"
    assert world.lane_rows(swap_id)["credit"]["attempts"] == 1

    world.sapphire.auto_mine = True
    await world.until(swap_id, "completed")
    await world.until(internal, "completed")

    assert [(to, nonce) for signer, to, nonce in world.ledger.landed if signer == lp(world)] == [
        (USER.address.lower(), 0), (USERS[1].address.lower(), 1)]
    assert world.paid(USER) == [(WETH, NET)]


@pytest.mark.parametrize("step,deadline,setup", [
    ("input:sent", 7200, "sapphire stopped"),
    ("withdraw:sent", 7200, "api unanswered"),
    ("execute:sent", 7200, "base stopped"),
    ("deposit:sent", 7200, "deposit address unavailable"),
    ("deposit:crediting", lifi_pipeline.CREDITING_WAIT_SEC, "never credited"),
    ("credit:sent", 7200, "sapphire stopped at credit"),
])
async def test_only_external_waits_park_at_their_deadline(world, step, deadline, setup):
    senders = None
    if setup == "sapphire stopped":
        senders = {BASE, ETHEREUM}
    elif setup == "base stopped":
        senders = {SAPPHIRE_CHAIN_ID, ETHEREUM}
    elif setup == "api unanswered":
        world.api.replies = [(False, NetworkError("Request timeout after 30s"))] * 50
    elif setup == "never credited":
        world.api.credits = False
    elif setup == "deposit address unavailable":
        world.api.get_deposit_address = AsyncMock(side_effect=NetworkError("Request timeout after 30s"))
    swap_id = await world.schedule("lifi")
    await world.until(swap_id, step, senders=senders)
    if setup == "sapphire stopped at credit":
        senders = {BASE, ETHEREUM}
    await world.run(2, senders=senders)
    world.age(swap_id, deadline - 60)
    await world.run(2, senders=senders)
    assert world.swap(swap_id)["step"] == step

    world.age(swap_id, 60)
    await world.run(senders=senders)

    swap = world.swap(swap_id)
    if step == "deposit:crediting":
        assert (swap["status"], swap["step"]) == ("failed", "parked")
        assert swap["error"].endswith("manual recovery required")
    else:
        assert (swap["status"], swap["step"]) == ("executing", step)
    assert world.paid(USER) == []


def _taken_after_an_attempt(world, owner):
    """The sender gave up on a signed attempt: it may still have been mined."""
    db_write(world.db, "UPDATE evm_transactions SET status = 'failed', attempts = '0:0xab', "
                       "error = 'nonce taken by another tx' WHERE owner = ?", (owner,))


async def test_a_lifi_tx_failed_after_a_signed_attempt_parks_without_refunding(world):
    no_base = {SAPPHIRE_CHAIN_ID, ETHEREUM}
    swap_id = await world.schedule("lifi")
    await world.until(swap_id, "execute:sent", senders=no_base)
    _taken_after_an_attempt(world, f"swap:{swap_id}:lifi")

    swap = await world.until(swap_id, "failed", senders=no_base)

    assert swap["step"] == "parked"
    assert world.evm_owners(swap_id) == ["input", "approve", "lifi"]
    assert world.paid(USER) == []


async def test_an_input_failed_after_a_signed_attempt_parks(world):
    stopped = {BASE, ETHEREUM}
    swap_id = await world.schedule("lifi")
    await world.run(senders=stopped)
    _taken_after_an_attempt(world, f"swap:{swap_id}:input")

    swap = await world.until(swap_id, "failed", senders=stopped)

    assert swap["step"] == "parked"


async def test_a_bridge_lifi_never_reports_done_parks_at_its_deadline(world):
    world.lifi.status = {"status": "PENDING"}
    swap_id = await world.schedule("lifi", to_token=ETH_WETH)
    await world.until(swap_id, "execute:bridging")
    world.age(swap_id, lifi_pipeline.BRIDGE_WAIT_SEC - 60)
    await world.run(2)
    assert world.swap(swap_id)["step"] == "execute:bridging"

    world.age(swap_id, 60)
    await world.run()

    swap = world.swap(swap_id)
    assert (swap["status"], swap["step"]) == ("failed", "parked")
    assert world.paid(USER) == []


async def test_a_lifi_tx_built_for_another_chain_is_never_queued(world):
    real = world.lifi.get_execution_quote

    async def for_ethereum(**kwargs):
        quote = await real(**kwargs)
        quote["transactionRequest"]["chainId"] = ETHEREUM
        return quote

    world.lifi.get_execution_quote = for_ethereum
    swap_id = await world.schedule("lifi")

    await world.until(swap_id, "refunded")

    assert world.evm_owners(swap_id) == ["input", "refund"]


async def test_a_credit_that_failed_parks_and_never_refunds_the_input(world):
    world.ledger.reverting_recipients.add(USER.address.lower())
    swap_id = await world.schedule("lifi")

    swap = await world.until(swap_id, "failed")
    await world.run(3)

    assert (swap["step"], world.lane_rows(swap_id)["credit"]["status"]) == ("parked", "failed")
    assert "refund_payout" not in world.lane_rows(swap_id)


async def test_turning_lifi_off_leaves_queued_lifi_swaps_queued(world):
    swap_id = await world.schedule("lifi")
    world.config = replace(world.config, lifi_execution_enabled=False)

    await world.run(rounds=3)

    assert world.swap(swap_id)["status"] == "scheduled"
    assert world.ledger.landed == []


async def test_blocked_users_queued_swaps_never_hide_another_users_swap(world, monkeypatch):
    monkeypatch.setattr(stage, "ROUND_LIMIT", 2)
    await world.schedule("lifi", nonce=0)
    await world.schedule("lifi", nonce=1)
    world.config = replace(world.config, lifi_execution_enabled=False)
    other = await world.schedule("internal", user=USERS[1])

    await world.run()

    assert world.swap(other)["status"] != "scheduled"
