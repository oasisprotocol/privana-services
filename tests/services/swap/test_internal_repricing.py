"""Internal swaps are priced when they run, not when they were quoted."""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.core.fee_policy as fee_policy_module
from src.core.fee_policy import parse_fee_policies
from src.services.swap.internal_pipeline import InternalSwapPipeline, PricingUnavailable

USER = "0x" + "d8" * 20


def _swap(**overrides):
    swap = {
        "id": "s1", "quote_id": "q1", "user_address": USER,
        "from_token_id": "0x" + "aa" * 32, "to_token_id": "0x" + "bb" * 32,
        "from_amount": "1000000", "to_amount_estimate": "990000",
        "to_amount_executed": None, "created_at": int(time.time()),
    }
    swap.update(overrides)
    return swap


@pytest.fixture
def quotes(monkeypatch, insert_quote, settings):
    """The quote service as execution uses it: LiFi returns `to_amount` gross."""
    import src.services.swap.internal_pipeline as module

    insert_quote("q1", user_address=USER, to_amount_min="980000", slippage_bps=50)
    service = MagicMock()
    service.accounting.get_token_info = AsyncMock(return_value=MagicMock())
    service.route = {"toAmount": "1000000", "fromAmountUSD": "1"}
    service._price_route = AsyncMock(side_effect=lambda *a: {"routes": [service.route]})
    service._enforce_max_swap_size = MagicMock()
    monkeypatch.setattr(module, "get_quote_service", lambda: service)
    monkeypatch.setattr(fee_policy_module, "_policies", [])
    monkeypatch.setattr(module, "resolve_internal_fee", fee_policy_module.resolve_internal_fee)
    return service


def _net(gross, settings):
    return gross - gross * settings.fee_bps // 10_000


async def test_pays_the_current_price_when_it_beat_the_quote(quotes, settings, test_db):
    quotes.route["toAmount"] = "1010000"

    payout = await InternalSwapPipeline()._fresh_payout(_swap())

    # The user gets the better price, never more than the market pays.
    assert payout == _net(1_010_000, settings)


async def test_a_price_move_inside_the_slippage_still_executes(quotes, settings, test_db):
    quotes.route["toAmount"] = "985000"

    assert await InternalSwapPipeline()._fresh_payout(_swap()) == _net(985_000, settings)


async def test_a_price_move_beyond_the_slippage_fails_the_swap(quotes, test_db):
    quotes.route["toAmount"] = "900000"

    with pytest.raises(ValueError, match="execution quote below floor"):
        await InternalSwapPipeline()._fresh_payout(_swap())


@pytest.mark.parametrize("failure", ["error", "timeout", "no route"])
async def test_no_price_is_not_a_price_move(quotes, monkeypatch, test_db, failure):
    import src.services.swap.internal_pipeline as module

    if failure == "error":
        quotes._price_route = AsyncMock(side_effect=OSError("lifi down"))
    elif failure == "timeout":
        monkeypatch.setattr(module, "REPRICE_TIMEOUT_SEC", 0.01)

        async def slow(*_):
            await asyncio.sleep(1)

        quotes._price_route = slow
    else:
        quotes._price_route = AsyncMock(return_value={"routes": []})

    with pytest.raises(PricingUnavailable):
        await InternalSwapPipeline()._fresh_payout(_swap())


async def test_an_exemption_that_ended_is_not_applied_at_execution(quotes, monkeypatch, settings, test_db):
    now = int(time.time())
    monkeypatch.setattr(fee_policy_module, "_policies", parse_fee_policies(json.dumps([{
        "id": "ended", "fee_bps": 0, "valid_from": now - 7200, "valid_until": now - 3600,
        "wallets": [USER],
    }])))

    assert await InternalSwapPipeline()._fresh_payout(_swap()) == _net(1_000_000, settings)


async def test_an_exemption_still_running_is_applied(quotes, monkeypatch, test_db):
    now = int(time.time())
    monkeypatch.setattr(fee_policy_module, "_policies", parse_fee_policies(json.dumps([{
        "id": "running", "fee_bps": 0, "valid_from": now - 60, "valid_until": now + 3600,
        "wallets": [USER],
    }])))

    assert await InternalSwapPipeline()._fresh_payout(_swap()) == 1_000_000


async def test_the_size_cap_is_checked_at_the_current_price(quotes, test_db):
    quotes._enforce_max_swap_size = MagicMock(side_effect=ValueError("Swap size exceeds the maximum"))

    with pytest.raises(ValueError, match="maximum"):
        await InternalSwapPipeline()._fresh_payout(_swap())
