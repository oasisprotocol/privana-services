"""The pricing and output reading the LiFi stages keep from the old pipeline."""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from eth_abi import encode
from hexbytes import HexBytes

import src.services.swap.lifi_pipeline as lifi_pipeline
from src.core.fees import calculate_fee
from src.services.swap.lifi_pipeline import (
    BelowFloor,
    _delivered,
    _execution_quote,
    _holds,
    _slippage_bps_to_floor,
    _swap_output,
)
from tests.services.swap.world import BASE, TOKENS, USDC, WETH

FEE_BPS = 10
PRICE = 60_000
LP = "0x" + "4b" * 20


def _min_at(to_amount, slippage_bps):
    """LiFi's toAmountMin for `to_amount` at `slippage_bps`."""
    return to_amount * (10_000 - slippage_bps) // 10_000


def _floor(to_amount, slippage_bps):
    # Mirrors QuoteService: toAmountMin of the priced route, less the fee on gross.
    _, fee = calculate_fee(to_amount, FEE_BPS)
    return _min_at(to_amount, slippage_bps) - fee


class Lifi:
    """LiFi pricing the input at each of `to_amounts` in turn, its toAmountMin
    derived from the slippage asked for, as the live API does."""

    def __init__(self, *to_amounts):
        self.prices = iter(to_amounts)
        self.asked = []

    async def get_execution_quote(self, **kwargs):
        self.asked.append(kwargs["slippage_bps"])
        to_amount = next(self.prices)
        return {"transactionRequest": {}, "estimate": {
            "toAmount": str(to_amount), "toAmountMin": str(_min_at(to_amount, kwargs["slippage_bps"]))}}


@pytest.fixture
def pricing(settings, monkeypatch):
    def at(*to_amounts):
        lifi = Lifi(*to_amounts)
        monkeypatch.setattr(lifi_pipeline, "get_lifi_client", lambda: lifi)
        monkeypatch.setattr(lifi_pipeline, "load_settings", lambda: replace(settings, fee_bps=FEE_BPS))
        return lifi

    return at


async def _quote(slippage_bps, floor):
    quote = {"from_amount": "1000000", "to_amount_min": str(floor), "slippage_bps": slippage_bps}
    return await _execution_quote("s", quote, TOKENS[USDC], TOKENS[WETH], LP)


class TestSlippageFloor:
    """The floor stored on a quote and the execution check must use one slippage."""

    @pytest.mark.parametrize("slippage_bps", [50, 300, 1000])
    async def test_an_unchanged_price_clears_the_floor(self, pricing, slippage_bps):
        lifi = pricing(PRICE)

        await _quote(slippage_bps, _floor(PRICE, slippage_bps))

        assert lifi.asked == [slippage_bps]

    async def test_a_drop_beyond_the_slippage_is_below_the_floor(self, pricing):
        lifi = pricing(int(PRICE * 0.96))

        with pytest.raises(BelowFloor):
            await _quote(300, _floor(PRICE, 300))
        assert len(lifi.asked) == 1

    async def test_a_drop_within_the_slippage_narrows_it_to_keep_the_floor(self, pricing):
        dropped = int(PRICE * 0.99)
        lifi = pricing(dropped, dropped)
        floor = _floor(PRICE, 300)

        await _quote(300, floor)

        narrowed = lifi.asked[1]
        assert 0 < narrowed < 300
        # The minimum LiFi enforces on chain still credits the floor.
        assert calculate_fee(_min_at(dropped, narrowed), FEE_BPS)[0] >= floor

    async def test_a_further_drop_before_the_narrowed_quote_is_below_the_floor(self, pricing):
        pricing(int(PRICE * 0.99), int(PRICE * 0.95))

        with pytest.raises(BelowFloor):
            await _quote(300, _floor(PRICE, 300))

    async def test_a_quote_without_a_slippage_executes_at_lifis_default(self, pricing):
        # Such a floor was priced at LiFi's 0.5% default.
        lifi = pricing(PRICE)

        await _quote(None, _floor(PRICE, 50))

        assert lifi.asked == [50]


class TestSlippageBpsToFloor:
    @pytest.mark.parametrize("fee_bps", [0, 10, 150])
    @pytest.mark.parametrize("to_amount", [10**6, 58_141, 10**18 + 7])
    @pytest.mark.parametrize("shortfall_bps", [0, 10, 200])
    def test_its_minimum_credits_the_floor(self, fee_bps, to_amount, shortfall_bps):
        floor, _ = calculate_fee(_min_at(to_amount, shortfall_bps), fee_bps)
        slippage_bps = _slippage_bps_to_floor(to_amount, floor, fee_bps)
        assert slippage_bps is not None and slippage_bps >= 0
        credited, _ = calculate_fee(_min_at(to_amount, slippage_bps), fee_bps)
        assert credited >= floor

    def test_none_when_the_expected_output_misses_the_floor(self):
        assert _slippage_bps_to_floor(57_000, 58_140, 10) is None


async def test_a_native_input_also_needs_the_gas_its_tx_pays(monkeypatch):
    monkeypatch.setattr(lifi_pipeline, "_queued_spend", lambda token_id: 0)
    evm = MagicMock(address=LP, max_gas_cost=MagicMock(return_value=5_000))
    evm.balance_of = MagicMock(return_value=1_004_999)

    assert not await _holds(evm, None, USDC, 1_000_000, gas=300_000)
    assert await _holds(evm, TOKENS[USDC].token_address, USDC, 1_000_000, gas=300_000)
    evm.max_gas_cost.assert_called_once_with(300_000)


def test_reads_the_output_of_a_real_swap():
    # Base, 10-02: USDC to LINK for the swap pool, tx 0x0183af8e...
    log = {
        "address": "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE",
        "topics": [
            HexBytes("0x38eee76fd911eabac79da7af16053e809be0e12c8637f156e77e1af309b99537"),
            HexBytes("0x243feac82354d758b2de7100b47352c4f9d0a755ee41530284fad6f7d0e9f047"),
        ],
        "data": HexBytes(
            "0x00000000000000000000000000000000000000000000000000000000000000e0"
            "0000000000000000000000000000000000000000000000000000000000000120"
            "000000000000000000000000d1cbc91ab86c43f975a18126cc6ad77cf1f22b8a"
            "000000000000000000000000833589fcd6edb6e08f4c7c32d4f71b54bda02913"
            "00000000000000000000000088fb150bdc53a65fe94dea0c9ba0a6daf8c6e196"
            "00000000000000000000000000000000000000000000000000000000004c4b40"
            "00000000000000000000000000000000000000000000000004d2920370b6e8b4"
            "0000000000000000000000000000000000000000000000000000000000000010"
            "70726976616e612d736572766963657300000000000000000000000000000000"
            "000000000000000000000000000000000000000000000000000000000000002a"
            "3078303030303030303030303030303030303030303030303030303030303030"
            "3030303030303030303000000000000000000000000000000000000000000000"
        ),
    }

    received = _swap_output(
        "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE", [log],
        "0xD1cbC91Ab86C43f975A18126cc6AD77cf1F22b8a", "0x88Fb150BDc53A65fe94Dea0c9BA0a6dAf8C6e196",
    )

    assert received == 347500664734542004


@pytest.mark.parametrize("emitter,receiver,token", [
    ("0x" + "66" * 20, None, None),
    (None, "0x" + "77" * 20, None),
    (None, None, "0x" + "99" * 20),
], ids=["another emitter", "another receiver", "another token"])
def test_an_output_the_logs_do_not_show_paying_the_lp_is_refused(emitter, receiver, token):
    diamond = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"
    to_token = TOKENS[WETH].token_address
    log = {
        "address": emitter or diamond,
        "topics": [lifi_pipeline.SWAP_COMPLETED_TOPIC, HexBytes("01" * 32)],
        "data": HexBytes(encode(lifi_pipeline.SWAP_COMPLETED_DATA, [
            "privana-services", "", receiver or LP, TOKENS[USDC].token_address, token or to_token, 1, 60_000])),
    }

    with pytest.raises(RuntimeError, match="no LiFi swap paying"):
        _swap_output(diamond, [log], LP, to_token)


def _done(chain_id=BASE, to=LP, token=None, substatus="COMPLETED"):
    return {"status": "DONE", "substatus": substatus, "toAddress": to,
            "receiving": {"chainId": chain_id, "amount": "60000",
                          "token": {"address": token or TOKENS[WETH].token_address}}}


def test_a_bridge_delivers_what_lifi_reports_once_it_is_done():
    assert _delivered({"status": "PENDING", "substatus": "WAIT_DESTINATION_TRANSACTION"}, TOKENS[WETH], LP) is None
    assert _delivered({}, TOKENS[WETH], LP) is None
    assert _delivered(_done(), TOKENS[WETH], LP) == 60_000


@pytest.mark.parametrize("status", [
    {"status": "FAILED"},
    {"status": "INVALID"},
    _done(substatus="PARTIAL"),
    _done(substatus="REFUNDED"),
    _done(to="0x" + "77" * 20),
    _done(chain_id=10),
    _done(token="0x" + "99" * 20),
], ids=["failed", "invalid", "partial", "refunded", "another recipient", "another chain", "another token"])
def test_anything_but_a_full_delivery_to_the_lp_is_refused(status):
    with pytest.raises(RuntimeError):
        _delivered(status, TOKENS[WETH], LP)
