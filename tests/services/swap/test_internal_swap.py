"""Internal swaps through the LP's real Sapphire sender and transfer lane."""

import pytest

import src.services.swap.internal_pipeline as internal_pipeline
from tests.clients.fake_chain import nonce_of
from tests.services.swap.world import BASE, ETHEREUM, USERS, WETH, World

USER = USERS[0]
# The internal price of 60_000, less the 10 bps fee.
NET = 59_940


@pytest.fixture
def world(settings, test_db, monkeypatch):
    world = World(settings, test_db, monkeypatch)
    world.ledger.set_lp_balance(WETH, 10**6)
    return world


def lp(world):
    return world.lp_address.lower()


def lose_the_first_reply(world):
    """The node takes the first tx sent, but its reply never comes back."""
    sapphire = world.sapphire

    def reply_lost(raw):
        sapphire.on_send = None
        sapphire.sent.append(raw)
        sapphire.pool[nonce_of(raw)] = raw
        raise ConnectionError("read timed out")

    sapphire.on_send = reply_lost


async def test_a_swap_moves_both_legs_once_at_the_current_price(world):
    world.internal_gross = 61_000
    swap_id = await world.schedule("internal")

    swap = await world.until(swap_id, "completed")

    assert swap["to_amount_actual"] == swap["to_amount_executed"] == str(61_000 - 61)
    assert swap["swap_tx_hash"] is not None
    assert world.ledger.landed == [(USER.address.lower(), lp(world), 0), (lp(world), USER.address.lower(), 0)]
    assert world.paid(USER) == [(WETH, 61_000 - 61)]


async def test_a_lost_send_reply_does_not_hold_up_the_swaps_behind_it(world):
    # The old pipeline left a swap signed without a hash executing, and ran no
    # other internal swap while it was.
    lose_the_first_reply(world)
    await world.schedule("internal", user=USERS[0])
    second = await world.schedule("internal", user=USERS[1])

    await world.until(second, "completed")

    assert world.paid(USERS[1]) == [(WETH, NET)]


async def test_a_swap_that_landed_after_a_lost_send_reply_completes(world):
    lose_the_first_reply(world)
    swap_id = await world.schedule("internal")

    swap = await world.until(swap_id, "completed")

    assert swap["error"] is None
    assert world.paid(USER) == [(WETH, NET)]


async def test_a_swap_mined_late_completes(world):
    # The old pipeline failed a swap whose receipt took over a minute.
    world.sapphire.auto_mine = False
    swap_id = await world.schedule("internal")
    await world.run(5)
    world.age(swap_id, 600)
    await world.run(3)
    assert world.swap(swap_id)["status"] == "executing"

    world.sapphire.auto_mine = True
    swap = await world.until(swap_id, "completed")

    assert world.paid(USER) == [(WETH, NET)]
    assert swap["to_amount_actual"] == str(NET)


async def test_an_input_that_would_revert_fails_without_moving_funds(world):
    swap_id = await world.schedule("internal")
    world.ledger.transfer_nonces[USER.address.lower()] = 1

    swap = await world.until(swap_id, "failed")

    assert swap["step"] == "no_funds_moved"
    assert world.lane_rows(swap_id) == {}
    assert world.ledger.landed == []


async def test_a_swap_its_lane_could_not_land_moved_no_funds(world):
    swap_id = await world.schedule("internal")
    await world.stage_rounds("internal_swap")
    await world.stage_rounds("internal_swap")
    # The user spends the nonce before the lane sends the swap.
    world.ledger.transfer_nonces[USER.address.lower()] = 1

    swap = await world.until(swap_id, "failed")

    assert swap["step"] == "no_funds_moved"
    assert world.lane_rows(swap_id)["internal"]["status"] == "failed"
    assert world.ledger.landed == []


async def test_the_lps_balance_counts_payouts_still_on_its_lane(world):
    world.ledger.set_lp_balance(WETH, 100_000)
    first = await world.schedule("internal", user=USERS[0])
    await world.stage_rounds("internal_swap")
    await world.stage_rounds("internal_swap")
    assert world.lane_rows(first)["internal"]["status"] == "queued"
    second = await world.schedule("internal", user=USERS[1])

    await world.stage_rounds("internal_swap")

    swap = world.swap(second)
    assert (swap["status"], swap["step"]) == ("failed", "no_funds_moved")
    assert "Insufficient liquidity" in swap["error"]
    assert (await world.until(first, "completed"))["to_amount_actual"] == str(NET)


async def test_a_users_next_swap_waits_for_the_one_before_to_spend_its_nonce(world):
    first = await world.schedule("internal", nonce=0)
    second = await world.schedule("internal", nonce=1)

    await world.stage_rounds("internal_swap")
    assert world.swap(second)["status"] == "scheduled"
    await world.until(second, "completed")

    assert world.swap(first)["status"] == "completed"
    assert world.paid(USER) == [(WETH, NET), (WETH, NET)]


async def test_a_users_swaps_are_claimed_in_order_across_venues(world):
    # Both spend the user's transfer nonce: the later one waits, unclaimed.
    internal = await world.schedule("internal", nonce=0)
    lifi = await world.schedule("lifi", nonce=1)

    await world.stage_rounds("input")
    assert world.swap(lifi)["status"] == "scheduled"
    await world.until(lifi, "completed")

    assert world.swap(internal)["status"] == "completed"
    assert world.paid(USER) == [(WETH, NET), (WETH, NET)]


async def test_a_swap_waits_for_its_lane_even_after_an_hour(world):
    swap_id = await world.schedule("internal")
    stopped = {BASE, ETHEREUM}
    await world.run(3, senders=stopped)
    world.age(swap_id, 7200)
    await world.run(senders=stopped)
    assert world.swap(swap_id)["status"] == "executing"

    await world.until(swap_id, "completed")
    assert world.paid(USER) == [(WETH, NET)]


async def test_a_foreign_lp_nonce_parks_its_swap_but_the_next_swap_can_finish(world):
    world.sapphire.auto_mine = False
    first = await world.schedule("internal")
    await world.run(2)
    world.ledger.transfer_nonces[lp(world)] = 1
    world.sapphire.mine()
    world.sapphire.auto_mine = True
    failed = await world.until(first, "failed")
    assert failed["step"] == "parked"

    second = await world.schedule("internal", user=USERS[1])
    await world.until(second, "completed")
    assert world.paid(USER) == []
    assert world.paid(USERS[1]) == [(WETH, NET)]


async def test_a_swap_without_a_price_waits_then_fails_without_moving_funds(world, monkeypatch):
    async def down(*_):
        raise TimeoutError

    monkeypatch.setattr(world.quotes, "_price_route", down)
    swap_id = await world.schedule("internal")
    await world.run(2)
    assert world.swap(swap_id)["step"] == "internal_swap:pricing"
    world.age(swap_id, internal_pipeline.PRICING_GIVE_UP_SEC)

    await world.run()

    swap = world.swap(swap_id)
    assert (swap["status"], swap["step"]) == ("failed", "no_funds_moved")
    assert world.ledger.landed == []


async def test_swaps_are_not_claimed_faster_than_they_are_priced(world, monkeypatch):
    async def down(*_):
        raise TimeoutError

    monkeypatch.setattr(world.quotes, "_price_route", down)
    monkeypatch.setattr(internal_pipeline, "CLAIM_LIMIT", 2)
    swap_ids = [await world.schedule("internal", user=user) for user in USERS[:3]]

    await world.run(3)

    # The third waits unclaimed, so its give-up clock has not started.
    assert [world.swap(swap_id)["status"] for swap_id in swap_ids] == ["executing", "executing", "scheduled"]


async def test_a_price_below_the_quotes_floor_fails_without_moving_funds(world):
    world.internal_gross = 50_000
    swap_id = await world.schedule("internal", floor=55_000)

    swap = await world.until(swap_id, "failed")

    assert swap["step"] == "no_funds_moved"
    assert "below floor" in swap["error"]
    assert world.ledger.landed == []


async def test_swaps_priced_in_one_round_share_the_lps_balance(world):
    # Room for one payout of 59_940, not two.
    world.ledger.set_lp_balance(WETH, 100_000)
    first = await world.schedule("internal", user=USERS[0])
    second = await world.schedule("internal", user=USERS[1])

    await world.stage_rounds("internal_swap")

    assert [world.swap(i)["step"] for i in (first, second)] == ["internal_swap:sent", "no_funds_moved"]
