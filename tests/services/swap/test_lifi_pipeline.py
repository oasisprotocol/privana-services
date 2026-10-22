import asyncio
import sqlite3
import time
import uuid
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from eth_abi import encode
from hexbytes import HexBytes
from web3 import Web3

from src.clients import evm_batcher
from src.clients.evm_batcher import TxIntent, transfer_intent
from src.core.db import db_write, get_db
from src.core.fees import calculate_fee
from src.models.common import TokenInfo

USER = "0x" + "a" * 40
POOL = "0x152E6a7125665764a4F1F1df80E8f5D49Bf0239c"
DIAMOND = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"
LIFI_TX = "0x" + "cd" * 32
DEPOSIT_TX = "0x" + "ef" * 32
APPROVE_TX = "0x" + "ab" * 32
DEPOSIT_ADDRESS = "0x" + "dd" * 20
FROM_TOKEN = "0x" + "aa" * 32
TO_TOKEN = "0x" + "bb" * 32
FROM_INFO = TokenInfo(
    token_id=FROM_TOKEN, token_type=1, token_type_name="ERC20", data="0x00",
    chain_id=84532, chain_name="Base Sepolia",
    token_address="0x8eEDCff0b07609Cfb5e2775dFf21EDbACc30D0df",
)
TO_INFO = TokenInfo(
    token_id=TO_TOKEN, token_type=1, token_type_name="ERC20", data="0x00",
    chain_id=84532, chain_name="Base Sepolia",
    token_address="0xA9B8D8039cb3FF9d9Fff6decD18EA7bb792e51D3",
)
EXEC_QUOTE = {
    "tool": "fly",
    "transactionRequest": {
        "to": DIAMOND,
        "data": "0xdead", "value": "0x0",
        "gasLimit": "0x15fcbf", "gasPrice": "0x3b9aca00",
    },
    "estimate": {
        "approvalAddress": DIAMOND,
        "toAmount": "58000", "toAmountMin": "56000",
    },
}
SWAP_COMPLETED = Web3.keccak(
    text="LiFiGenericSwapCompleted(bytes32,string,string,address,address,address,uint256,uint256)"
)


def _swap_receipt(to_amount, token=TO_INFO.token_address, receiver=POOL, emitter=DIAMOND):
    data = encode(
        ["string", "string", "address", "address", "address", "uint256", "uint256"],
        ["privana-services", "", receiver, FROM_INFO.token_address, token, 1_000_000, to_amount],
    )
    log = {
        "address": emitter,
        "topics": [SWAP_COMPLETED, HexBytes("01" * 32)],
        "data": HexBytes(data),
    }
    return {"status": 1, "to": DIAMOND, "logs": [log]}


def _bridged(amount, chain_id=1, substatus="COMPLETED"):
    """LiFi's status for a finished bridge that paid the swap pool."""
    return {
        "status": "DONE", "substatus": substatus, "toAddress": POOL,
        "receiving": {
            "chainId": chain_id, "amount": str(amount),
            "token": {"address": TO_INFO.token_address},
        },
    }


def _erc20(fn, to, amount):
    """The calldata of an ERC-20 `fn(address,uint256)` call."""
    selector = Web3.keccak(text=f"{fn}(address,uint256)")[:4]
    return Web3.to_hex(selector + encode(["address", "uint256"], [to, amount]))


def _in_turn(*sends):
    """Each call runs the next of `sends`. An extra call fails: a StopIteration
    would hang the awaiting task."""
    calls = iter(sends)

    def send(*args, **kwargs):
        step = next(calls, None)
        if step is None:
            raise AssertionError(f"more than {len(sends)} sends")
        return step(*args, **kwargs)
    return send


def _reads(*values):
    """Each call returns the next of `values`. An extra call fails, as in `_in_turn`."""
    return _in_turn(*(lambda *_, value=value: value for value in values))


def _evm():
    """The swap pool wallet on one chain, funded."""
    evm = MagicMock()
    evm.address = POOL
    evm.balance_of = MagicMock(return_value=10**18)
    evm.max_gas_cost = MagicMock(return_value=10**15)
    return evm


class FakeBatcher:
    """The batcher's real queue on the test DB, with no chain behind it: a
    queued tx settles when it is polled, by `outcomes[kind]` (the owner's last
    part). "mined" (the default) or "reverted" take the kind's hash and, for
    the swap, `receipt`'s logs; "failed" fails it unsigned, "refused" fails it
    after an attempt was signed; "queued" leaves it waiting."""

    HASHES = {"lifi": LIFI_TX, "deposit": DEPOSIT_TX, "refund": DEPOSIT_TX, "approve": APPROVE_TX}

    def __init__(self, receipt=None):
        self.outcomes: dict[str, str] = {}
        self.receipt = receipt or _swap_receipt(60000)

    def submit(self, chain_id, *intents):
        return evm_batcher.submit(chain_id, *intents)

    def result(self, tx_id):
        row = evm_batcher.result(tx_id)
        kind = row["owner"].rsplit(":", 1)[1]
        outcome = self.outcomes.get(kind, "mined")
        if row["status"] != "queued" or outcome == "queued":
            return row
        if outcome == "failed":
            _settle(tx_id, "failed", error="simulation failed")
        elif outcome == "refused":
            _settle(tx_id, "failed", error="insufficient funds", attempts=f"7:{self.HASHES[kind]}")
        else:
            logs = self.receipt["logs"] if kind == "lifi" else []
            _settle(tx_id, outcome, tx_hash=self.HASHES[kind], nonce=7, logs=Web3.to_json(logs))
        return evm_batcher.result(tx_id)


def _settle(tx_id, status, tx_hash=None, nonce=None, logs=None, error=None, attempts=None):
    db_write(get_db(), "UPDATE evm_transactions SET status = ?, tx_hash = ?, nonce = ?, logs = ?, "
             "error = ?, attempts = COALESCE(?, attempts) WHERE id = ?",
             (status, tx_hash, nonce, logs, error, attempts, tx_id))


async def _recovered(pipeline):
    """Recover in-flight swaps and wait for every runner it started."""
    from src.services.swap.lifi_pipeline import recover_inflight_lifi_swaps
    await recover_inflight_lifi_swaps(pipeline=pipeline)
    await asyncio.gather(*pipeline._tasks.values())


def _txs():
    """The txs the pipeline queued, oldest first."""
    return [dict(r) for r in get_db().execute("SELECT * FROM evm_transactions ORDER BY id")]


def _kinds():
    """What each queued tx is for, oldest first."""
    return [tx["owner"].rsplit(":", 1)[1] for tx in _txs()]


def _make_pipeline(settings):
    from src.services.swap.lifi_pipeline import LifiSwapPipeline

    accounting = MagicMock()
    accounting.get_transfer_nonce = AsyncMock(side_effect=[6, 70, 71])
    accounting.get_token_info = AsyncMock(side_effect=[FROM_INFO, TO_INFO])

    lifi = MagicMock()
    lifi.get_execution_quote = AsyncMock(return_value=EXEC_QUOTE)
    lifi.get_status = AsyncMock(return_value=_bridged(60000))

    bridge = MagicMock()
    bridge.withdraw_to_chain = AsyncMock(return_value=17)
    bridge.get_deposit_address = AsyncMock(return_value=DEPOSIT_ADDRESS)
    bridge.await_deposit_credit = AsyncMock(return_value=None)

    evm = _evm()
    batcher = FakeBatcher()

    privana = MagicMock()
    privana.transfer_funds = AsyncMock(return_value=MagicMock(status="submitted", detail=None))

    async def privana_factory():
        return privana

    pipeline = LifiSwapPipeline(
        accounting=accounting, lifi=lifi, bridge=bridge, evm=evm, batcher=batcher,
        privana_factory=privana_factory, poll_interval_sec=0.0,
    )
    pipeline.settings = replace(settings, fee_bps=10)
    return pipeline


def _seed_swap(test_db, quote, user=USER):
    swap_id = str(uuid.uuid4())
    now = int(time.time())
    db_write(
        test_db,
        """INSERT INTO swaps
           (id, quote_id, user_address, from_token_id, to_token_id,
            from_amount, to_amount_estimate, status, venue, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'scheduled', 'lifi', ?, ?)""",
        (swap_id, quote["id"], user, quote["from_token_id"], quote["to_token_id"],
         quote["from_amount"], quote["to_amount_estimate"], now, now),
    )
    return swap_id


def _swap_row(swap_id):
    return dict(get_db().execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())


async def _swap(pipeline, insert_quote, quote_id="qo"):
    """Launch a swap and drive it to its end."""
    insert_quote(quote_id, venue="lifi", user_address=USER,
                 from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
    quote = _quote(quote_id)
    pipeline.spawn_background = MagicMock()
    swap_id = _seed_swap(get_db(), quote)
    await pipeline.launch(quote, USER, 5, "0x" + "ab" * 65, swap_id)
    await pipeline._run(swap_id, quote, 5)
    return _swap_row(swap_id)


def _quote(quote_id="q_lifi"):
    return {
        "id": quote_id,
        "user_address": USER,
        "from_token_id": FROM_TOKEN,
        "to_token_id": TO_TOKEN,
        "from_amount": "1000000",
        "to_amount_estimate": "57000",
        "to_amount_min": "55000",
        "venue": "lifi",
        "expires_at": int(time.time()) + 300,
    }


class TestLaunch:
    async def test_requires_existing_swap_id(self, settings):
        pipeline = _make_pipeline(settings)
        with pytest.raises(TypeError):
            await pipeline.launch(_quote(), USER, 5, "0x" + "ab" * 65)

    async def test_rejected_input_returns_failed_record(self, test_db, settings, insert_quote):
        insert_quote("q_rej", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        privana = await pipeline._privana_factory()
        privana.transfer_funds = AsyncMock(return_value=MagicMock(status="rejected", detail="bad sig"))
        quote = _quote("q_rej")
        swap_id = _seed_swap(test_db, quote)
        record = await pipeline.launch(quote, USER, 5, "0x" + "ab" * 65, swap_id)
        assert record.status == "failed"
        assert record.venue == "lifi"

    async def test_accepted_input_returns_executing_record(self, test_db, settings, insert_quote):
        insert_quote("q_ok", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline.spawn_background = MagicMock()
        quote = _quote("q_ok")
        swap_id = _seed_swap(test_db, quote)
        record = await pipeline.launch(quote, USER, 5, "0x" + "ab" * 65, swap_id)
        assert record.status == "executing"
        assert record.step == "input_transfer"
        pipeline.spawn_background.assert_called_once()


class TestRun:
    async def _launch_and_run(self, pipeline, quote):
        pipeline.spawn_background = MagicMock()
        swap_id = _seed_swap(get_db(), quote)
        record = await pipeline.launch(quote, USER, 5, "0x" + "ab" * 65, swap_id)
        await pipeline._run(record.id, quote, 5)
        return record.id

    async def test_happy_path_completes_with_actual_amount(self, test_db, settings, insert_quote):
        insert_quote("q1", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        swap_id = await self._launch_and_run(pipeline, _quote("q1"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        credited, _ = calculate_fee(60000, 10)
        assert row["status"] == "completed"
        assert row["step"] == "credit"
        assert row["withdrawal_index"] == 17
        assert row["lifi_tx_hash"] == "0x" + "cd" * 32
        assert row["deposit_tx_hash"] == "0x" + "ef" * 32
        assert row["to_amount_actual"] == str(credited)

    async def test_floor_guard_fails_before_sending_tx(self, test_db, settings, insert_quote):
        insert_quote("q2", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[FROM_INFO, TO_INFO, FROM_INFO])
        low_quote = {**EXEC_QUOTE, "estimate": {**EXEC_QUOTE["estimate"], "toAmountMin": "50000"}}
        pipeline.lifi.get_execution_quote = AsyncMock(return_value=low_quote)
        swap_id = await self._launch_and_run(pipeline, _quote("q2"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        assert row["status"] == "refunded"
        assert _kinds() == ["refund"]

    async def test_credit_retries_until_accepted(self, test_db, settings, insert_quote):
        insert_quote("q4", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        privana = await pipeline._privana_factory()
        privana.transfer_funds = AsyncMock(side_effect=[
            MagicMock(status="submitted", detail=None),
            MagicMock(status="rejected", detail="nonce"),
            MagicMock(status="submitted", detail=None),
        ])
        pipeline.accounting.get_transfer_nonce = AsyncMock(side_effect=[6, 70, 70, 71])
        swap_id = await self._launch_and_run(pipeline, _quote("q4"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        assert row["status"] == "completed"
        assert privana.transfer_funds.await_count == 3

    @pytest.mark.parametrize("outcome", ["reverted", "failed", "refused"])
    async def test_a_deposit_that_is_not_mined_parks(
        self, test_db, settings, insert_quote, outcome
    ):
        insert_quote("q5", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline.batcher.outcomes["deposit"] = outcome
        swap_id = await self._launch_and_run(pipeline, _quote("q5"))
        row = _swap_row(swap_id)
        assert row["status"] == "failed"
        assert row["error"].endswith("manual recovery required")
        assert (row["step"], row["deposit_tx_hash"]) == ("deposit", None)
        # The output stays in the wallet: no second deposit, no refund.
        assert _kinds() == ["approve", "lifi", "deposit"]
        pipeline.bridge.await_deposit_credit.assert_not_awaited()
        privana = await pipeline._privana_factory()
        assert privana.transfer_funds.await_count == 1


def _min_at(to_amount, slippage_bps):
    """LiFi's toAmountMin for `to_amount` at `slippage_bps`."""
    return to_amount * (10_000 - slippage_bps) // 10_000


class TestSlippageFloor:
    """The floor stored on a quote and the execution check must use one slippage.

    The fake LiFi derives toAmountMin from the slippage it is asked for, as the
    live API does, so these tests hold for the invariant rather than for numbers.
    """

    FEE_BPS = 10  # _make_pipeline's fee
    PRICE = 60000  # LiFi's toAmount for the quoted input

    def _floor(self, to_amount, slippage_bps):
        # Mirrors QuoteService: toAmountMin of the priced route, less the fee on gross.
        _, fee = calculate_fee(to_amount, self.FEE_BPS)
        return _min_at(to_amount, slippage_bps) - fee

    def _lifi_at(self, pipeline, *to_amounts):
        """LiFi pricing the input at each of `to_amounts` in turn."""
        prices = iter(to_amounts)

        async def get_execution_quote(**kwargs):
            to_amount = next(prices)
            to_amount_min = _min_at(to_amount, kwargs["slippage_bps"])
            estimate = {**EXEC_QUOTE["estimate"], "toAmount": str(to_amount),
                        "toAmountMin": str(to_amount_min)}
            return {**EXEC_QUOTE, "estimate": estimate}
        pipeline.lifi.get_execution_quote = AsyncMock(side_effect=get_execution_quote)

    async def _run(self, test_db, pipeline, quote):
        pipeline.spawn_background = MagicMock()
        swap_id = _seed_swap(get_db(), quote)
        record = await pipeline.launch(quote, USER, 5, "0x" + "ab" * 65, swap_id)
        await pipeline._run(record.id, quote, 5)
        return dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())

    @pytest.mark.parametrize("slippage_bps", [50, 300, 1000])
    async def test_an_unchanged_price_clears_the_floor(
        self, test_db, settings, insert_quote, slippage_bps
    ):
        insert_quote("qs", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN, slippage_bps=slippage_bps)
        pipeline = _make_pipeline(settings)
        self._lifi_at(pipeline, self.PRICE)
        quote = {**_quote("qs"), "slippage_bps": slippage_bps,
                 "to_amount_min": str(self._floor(self.PRICE, slippage_bps))}

        row = await self._run(test_db, pipeline, quote)

        assert row["status"] == "completed"
        assert pipeline.lifi.get_execution_quote.call_args.kwargs["slippage_bps"] == slippage_bps

    async def test_a_drop_beyond_the_slippage_fails_before_sending(self, test_db, settings, insert_quote):
        insert_quote("qd", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[FROM_INFO, TO_INFO, FROM_INFO])
        self._lifi_at(pipeline, int(self.PRICE * 0.96))
        quote = {**_quote("qd"), "slippage_bps": 300,
                 "to_amount_min": str(self._floor(self.PRICE, 300))}

        row = await self._run(test_db, pipeline, quote)

        assert row["status"] == "refunded"
        assert "execution quote below floor" in row["error"]
        assert pipeline.lifi.get_execution_quote.await_count == 1
        assert _kinds() == ["refund"]

    async def test_a_drop_within_the_slippage_narrows_it_to_keep_the_floor(
        self, test_db, settings, insert_quote
    ):
        insert_quote("qn", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        dropped = int(self.PRICE * 0.99)
        self._lifi_at(pipeline, dropped, dropped)
        floor = self._floor(self.PRICE, 300)
        quote = {**_quote("qn"), "slippage_bps": 300, "to_amount_min": str(floor)}

        row = await self._run(test_db, pipeline, quote)

        assert row["status"] == "completed"
        narrowed = pipeline.lifi.get_execution_quote.call_args_list[1].kwargs["slippage_bps"]
        assert 0 < narrowed < 300
        # The minimum LiFi enforces on chain still credits the floor.
        credited_min, _ = calculate_fee(_min_at(dropped, narrowed), self.FEE_BPS)
        assert credited_min >= floor

    async def test_a_further_drop_before_the_narrowed_quote_fails_before_sending(
        self, test_db, settings, insert_quote
    ):
        insert_quote("qf", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[FROM_INFO, TO_INFO, FROM_INFO])
        self._lifi_at(pipeline, int(self.PRICE * 0.99), int(self.PRICE * 0.95))
        quote = {**_quote("qf"), "slippage_bps": 300,
                 "to_amount_min": str(self._floor(self.PRICE, 300))}

        row = await self._run(test_db, pipeline, quote)

        assert row["status"] == "refunded"
        assert "execution quote below floor" in row["error"]
        assert _kinds() == ["refund"]

    async def test_a_quote_without_a_slippage_executes_at_lifis_default(
        self, test_db, settings, insert_quote
    ):
        # Such a floor was priced at LiFi's 0.5% default.
        insert_quote("ql", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN, slippage_bps=None)
        pipeline = _make_pipeline(settings)
        self._lifi_at(pipeline, self.PRICE)
        quote = {**_quote("ql"), "to_amount_min": str(self._floor(self.PRICE, 50))}

        row = await self._run(test_db, pipeline, quote)

        assert row["status"] == "completed"
        assert pipeline.lifi.get_execution_quote.call_args.kwargs["slippage_bps"] == 50


class TestSlippageBpsToFloor:
    @pytest.mark.parametrize("fee_bps", [0, 10, 150])
    @pytest.mark.parametrize("to_amount", [10**6, 58_141, 10**18 + 7])
    @pytest.mark.parametrize("shortfall_bps", [0, 10, 200])
    def test_its_minimum_credits_the_floor(self, fee_bps, to_amount, shortfall_bps):
        from src.services.swap.lifi_pipeline import _slippage_bps_to_floor

        gross_floor = _min_at(to_amount, shortfall_bps)
        floor, _ = calculate_fee(gross_floor, fee_bps)
        slippage_bps = _slippage_bps_to_floor(to_amount, floor, fee_bps)
        assert slippage_bps is not None and slippage_bps >= 0
        credited, _ = calculate_fee(_min_at(to_amount, slippage_bps), fee_bps)
        assert credited >= floor

    def test_none_when_the_expected_output_misses_the_floor(self):
        from src.services.swap.lifi_pipeline import _slippage_bps_to_floor

        assert _slippage_bps_to_floor(57_000, 58_140, 10) is None


class TestRefund:
    async def _launch_and_run(self, pipeline, quote):
        pipeline.spawn_background = MagicMock()
        swap_id = _seed_swap(get_db(), quote)
        record = await pipeline.launch(quote, USER, 5, "0x" + "ab" * 65, swap_id)
        await pipeline._run(record.id, quote, 5)
        return record.id

    async def test_withdraw_failure_refunds_input(self, test_db, settings, insert_quote):
        insert_quote("q_w", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline.bridge.withdraw_to_chain = AsyncMock(side_effect=RuntimeError("relay down"))
        pipeline.accounting.get_transfer_nonce = AsyncMock(side_effect=[6, 70, 71])
        swap_id = await self._launch_and_run(pipeline, _quote("q_w"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        assert row["status"] == "refunded"
        privana = await pipeline._privana_factory()
        refund_call = privana.transfer_funds.await_args_list[-1].args[0]
        assert refund_call.to_address == USER
        assert refund_call.token_id == FROM_TOKEN
        assert refund_call.amount == 1000000

    async def test_a_lifi_tx_that_never_signed_redeposits_then_refunds(
        self, test_db, settings, insert_quote
    ):
        insert_quote("q_e", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        # Its simulation reverted, so no attempt was ever signed.
        pipeline.batcher.outcomes["lifi"] = "failed"
        pipeline.accounting.get_transfer_nonce = AsyncMock(side_effect=[6, 70, 71])
        pipeline.accounting.get_token_info = AsyncMock(
            side_effect=[FROM_INFO, TO_INFO, FROM_INFO])
        swap_id = await self._launch_and_run(pipeline, _quote("q_e"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        assert row["status"] == "refunded"
        assert _kinds() == ["approve", "lifi", "refund"]
        pipeline.bridge.await_deposit_credit.assert_awaited_once_with(
            FROM_INFO.chain_id, DEPOSIT_TX, 1000000)

    async def test_unconfirmed_redeposit_is_awaited_not_resent(self, test_db, settings, insert_quote):
        insert_quote("q_p", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline.accounting.get_token_info = AsyncMock(return_value=FROM_INFO)
        # The queued redeposit already moved the input out of the wallet.
        pipeline.evm.balance_of = MagicMock(return_value=0)
        swap_id = _seed_swap(test_db, _quote("q_p"))
        pipeline._update_swap(swap_id, status="refunding", step="lifi_execute")
        pipeline.batcher.submit(FROM_INFO.chain_id, transfer_intent(
            f"swap:{swap_id}:refund", FROM_INFO.token_address, DEPOSIT_ADDRESS, 1000000))
        await _recovered(pipeline)
        assert _swap_row(swap_id)["status"] == "refunded"
        assert _kinds() == ["refund"]
        pipeline.bridge.get_deposit_address.assert_not_awaited()
        pipeline.bridge.await_deposit_credit.assert_awaited_once_with(
            FROM_INFO.chain_id, DEPOSIT_TX, 1000000)

    async def test_deposit_exhaustion_fails_without_refund(self, test_db, settings, insert_quote):
        insert_quote("q_d", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline.bridge.await_deposit_credit = AsyncMock(side_effect=RuntimeError("relay stuck"))
        swap_id = await self._launch_and_run(pipeline, _quote("q_d"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        assert row["status"] == "failed"
        assert "relay stuck" in row["error"]
        assert _kinds() == ["approve", "lifi", "deposit"]
        pipeline.bridge.await_deposit_credit.assert_awaited_once()

    async def test_credit_exhaustion_fails_without_refund(self, test_db, settings, insert_quote):
        insert_quote("q_c", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline._credit_max_retries = 2
        privana = await pipeline._privana_factory()
        privana.transfer_funds = AsyncMock(side_effect=[
            MagicMock(status="submitted", detail=None),
            MagicMock(status="rejected", detail="boom"),
            MagicMock(status="rejected", detail="boom"),
        ])
        pipeline.accounting.get_transfer_nonce = AsyncMock(side_effect=[6, 70, 70, 71])
        swap_id = await self._launch_and_run(pipeline, _quote("q_c"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        assert row["status"] == "failed"
        assert "credit" in row["error"]
        assert row["error"].endswith("manual recovery required")


class TestRecovery:
    def _insert_swap(self, test_db, swap_id, step, status="executing", venue="lifi"):
        import time as _t

        from src.core.db import db_write
        now = int(_t.time())
        db_write(
            test_db,
            """INSERT INTO swaps
               (id, quote_id, user_address, from_token_id, to_token_id, from_amount,
                to_amount_estimate, status, venue, step, input_nonce, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (swap_id, "q_rec", USER, FROM_TOKEN, TO_TOKEN,
             "1000000", "57000", status, venue, step, "5", now, now),
        )

    async def test_inflight_swaps_routed_to_refund_or_failed(
        self, test_db, settings, insert_quote
    ):
        insert_quote("q_rec", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        self._insert_swap(test_db, "s_withdraw", "withdraw")
        self._insert_swap(test_db, "s_credit", "credit")
        pipeline = _make_pipeline(settings)
        pipeline._refund = AsyncMock()
        await _recovered(pipeline)
        refunded_ids = [c.args[0] for c in pipeline._refund.await_args_list]
        assert "s_withdraw" in refunded_ids
        credit_row = dict(test_db.execute("SELECT * FROM swaps WHERE id='s_credit'").fetchone())
        assert credit_row["status"] == "failed"
        assert "manual" in credit_row["error"]

    async def test_internal_swaps_untouched(self, test_db, settings):
        import time as _t

        from src.core.db import db_write
        from src.services.swap.lifi_pipeline import recover_inflight_lifi_swaps
        now = int(_t.time())
        db_write(
            test_db,
            """INSERT INTO swaps
               (id, quote_id, user_address, from_token_id, to_token_id,
                from_amount, to_amount_estimate, status, venue, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("s_int", "q_i", USER, FROM_TOKEN, TO_TOKEN, "1", "1", "pending", "internal", now, now),
        )
        pipeline = _make_pipeline(settings)
        pipeline._refund = AsyncMock()
        await recover_inflight_lifi_swaps(pipeline=pipeline)
        pipeline._refund.assert_not_awaited()

    async def test_an_upgrade_parks_swaps_the_old_pipeline_left_in_flight(
        self, test_db, settings, insert_quote
    ):
        from src.core.db import _run_migrations
        from src.services.swap.lifi_pipeline import recover_inflight_lifi_swaps
        insert_quote("q_rec", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        # Left by the old pipeline, which saved a LiFi tx only once mined.
        self._insert_swap(test_db, "s_executing", "lifi_execute")
        self._insert_swap(test_db, "s_refunding", "lifi_execute", status="refunding")
        db_write(test_db, "UPDATE swaps SET error = 'transaction reverted' WHERE id = 's_refunding'")
        self._insert_swap(test_db, "s_completed", "credit", status="completed")
        self._insert_swap(test_db, "s_internal", None, venue="internal")
        db_write(test_db, "UPDATE swaps SET updated_at = 1")
        db_write(test_db, "DELETE FROM data_migrations WHERE name = 'lifi_park_inflight_v1'")
        upgraded_at = int(time.time())
        _run_migrations(test_db)
        pipeline = _make_pipeline(settings)
        pipeline.spawn_background = MagicMock()
        pipeline._refund = AsyncMock()

        await recover_inflight_lifi_swaps(pipeline=pipeline)

        pipeline.spawn_background.assert_not_called()
        pipeline._refund.assert_not_awaited()
        assert {r["id"]: (r["status"], r["error"]) for r in map(_swap_row, (
            "s_executing", "s_refunding", "s_completed", "s_internal",
        ))} == {
            "s_executing": ("failed", "interrupted by upgrade; manual recovery required"),
            "s_refunding": (
                "failed",
                "transaction reverted; interrupted by upgrade; manual recovery required",
            ),
            "s_completed": ("completed", None),
            "s_internal": ("executing", None),
        }
        # Unsettled operations sort by updated_at, so a parked swap shows as just changed.
        assert _swap_row("s_executing")["updated_at"] >= upgraded_at
        # Once: later swaps are the new pipeline's own.
        self._insert_swap(test_db, "s_new", "lifi_execute")
        _run_migrations(test_db)
        assert _swap_row("s_new")["status"] == "executing"


class TestLpNonceCoordination:
    async def test_waits_for_ledger_confirmation_while_holding_lock(self, settings):
        from src.services.swap.lifi_pipeline import lp_transfer_lock as imported_lock
        from src.services.swap.worker import lp_transfer_lock

        assert imported_lock is lp_transfer_lock
        pipeline = _make_pipeline(settings)
        values = iter([70, 70, 71])

        async def nonce(address):
            assert lp_transfer_lock.locked()
            return next(values)

        pipeline.accounting.get_transfer_nonce = AsyncMock(side_effect=nonce)
        await pipeline._lp_transfer(USER, TO_TOKEN, 100)
        assert pipeline.accounting.get_transfer_nonce.await_count == 3
        assert not lp_transfer_lock.locked()

    async def test_waits_beyond_retry_limit_until_internal_swap_settles(
        self, settings, test_db, monkeypatch
    ):
        from src.services.swap.worker import lp_transfer_lock

        pipeline = _make_pipeline(settings)
        swap_id = _seed_swap(test_db, _quote())
        pipeline._update_swap(
            swap_id, venue="internal", status="executing", output_signature="0xab"
        )
        pipeline._credit_max_retries = 2
        privana = await pipeline._privana_factory()
        pipeline.accounting.get_transfer_nonce = AsyncMock(side_effect=[70, 71])
        waits = 0

        async def settle_after_delay(delay):
            nonlocal waits
            waits += 1
            assert not lp_transfer_lock.locked()
            privana.transfer_funds.assert_not_awaited()
            pipeline.accounting.get_transfer_nonce.assert_not_awaited()
            if waits > pipeline._credit_max_retries:
                # The internal worker can acquire the lock to reconcile its tx.
                async with lp_transfer_lock:
                    pipeline._update_swap(swap_id, status="completed")

        monkeypatch.setattr(asyncio, "sleep", settle_after_delay)
        await asyncio.wait_for(pipeline._lp_transfer(USER, TO_TOKEN, 100), timeout=1)
        assert waits == 3
        privana.transfer_funds.assert_awaited_once()
        payout = privana.transfer_funds.await_args.args[0]
        assert (payout.to_address, payout.token_id, payout.amount) == (USER, TO_TOKEN, 100)


class TestNativeCoins:
    """HYPE on chain 999 and the like: accounting reports no token address,
    LiFi takes the zero address, and each leg runs on its own chain."""

    NATIVE = "0x0000000000000000000000000000000000000000"

    @staticmethod
    def _info(base, chain_id, token_address):
        return TokenInfo(**{**base.__dict__, "chain_id": chain_id, "token_address": token_address})


    async def _run(self, pipeline, test_db, insert_quote):
        insert_quote("qn", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline.spawn_background = MagicMock()
        quote = _quote("qn")
        swap_id = _seed_swap(test_db, quote)
        await pipeline.launch(quote, USER, 5, "0x" + "ab" * 65, swap_id)
        await pipeline._run(swap_id, quote, 5)
        return dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())

    @staticmethod
    def _per_chain(pipeline, monkeypatch, *chains):
        """A wallet and a batcher on each of `chains`, as outside tests."""
        pipeline.evm = None
        clients = {chain: _evm() for chain in chains}
        monkeypatch.setattr("src.services.swap.lifi_pipeline.get_evm_client", clients.__getitem__)
        return clients, pipeline.batcher

    async def test_a_native_coin_goes_to_lifi_as_the_zero_address(
        self, test_db, settings, insert_quote, monkeypatch
    ):
        pipeline = _make_pipeline(settings)
        clients, _ = self._per_chain(pipeline, monkeypatch, 999, 84532)
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[
            self._info(FROM_INFO, 999, ""), TO_INFO,
        ])
        pipeline.lifi.get_status = AsyncMock(return_value=_bridged(60000, chain_id=84532))

        row = await self._run(pipeline, test_db, insert_quote)

        assert row["status"] == "completed"
        quoted = pipeline.lifi.get_execution_quote.call_args.kwargs
        assert quoted["from_token_address"] == self.NATIVE
        assert quoted["from_chain_id"] == 999
        # The swap is sent on HyperEVM; the output is returned on Base.
        assert [(tx["chain_id"], kind) for tx, kind in zip(_txs(), _kinds())] == [
            (999, "lifi"), (84532, "deposit")]
        clients[999].balance_of.assert_called_with("", POOL)
        pipeline.lifi.get_status.assert_awaited_once_with(LIFI_TX, 999, 84532)

    async def test_native_output_on_the_same_chain_is_not_short_by_the_gas(
        self, test_db, settings, insert_quote
    ):
        pipeline = _make_pipeline(settings)
        # Gas moves the wallet balance, not the event.
        pipeline.batcher.receipt = _swap_receipt(60000, token=self.NATIVE)
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[
            FROM_INFO, self._info(TO_INFO, 84532, ""),
        ])

        row = await self._run(pipeline, test_db, insert_quote)

        credited, _ = calculate_fee(60000, 10)
        assert row["status"] == "completed"
        assert row["to_amount_actual"] == str(credited)
        assert pipeline.lifi.get_execution_quote.call_args.kwargs["to_token_address"] == self.NATIVE

    async def test_a_refund_of_a_native_coin_goes_back_on_its_own_chain(
        self, test_db, settings, insert_quote, monkeypatch
    ):
        pipeline = _make_pipeline(settings)
        _, batcher = self._per_chain(pipeline, monkeypatch, 999, 84532)
        batcher.outcomes["lifi"] = "reverted"
        native = self._info(FROM_INFO, 999, None)
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[native, TO_INFO, native])
        pipeline.accounting.get_transfer_nonce = AsyncMock(side_effect=[6, 70, 71])

        row = await self._run(pipeline, test_db, insert_quote)

        assert row["status"] == "refunded"
        _, refund = _txs()
        assert _kinds() == ["lifi", "refund"]
        assert (refund["chain_id"], refund["to_address"], refund["value"], refund["data"]) == (
            999, DEPOSIT_ADDRESS, "1000000", "0x")


class TestSettleByOutcome:
    """A LiFi tx settles by the batcher's row: its logs, or LiFi's status for a bridge."""

    async def test_a_receipt_after_the_send_timed_out_credits_and_never_refunds(
        self, settings, insert_quote
    ):
        # The batcher's receipt wait timed out: the row stays queued until a later round.
        pipeline = _make_pipeline(settings)
        pipeline.batcher.outcomes["lifi"] = "queued"
        polls = 0
        result = pipeline.batcher.result

        def mined_after_twenty_polls(tx_id):
            nonlocal polls
            polls += 1
            if polls > 20:
                pipeline.batcher.outcomes["lifi"] = "mined"
            return result(tx_id)
        pipeline.batcher.result = mined_after_twenty_polls
        pipeline._refund = AsyncMock()

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "completed"
        assert row["to_amount_actual"] == str(calculate_fee(60000, 10)[0])
        pipeline._refund.assert_not_awaited()
        assert _kinds() == ["approve", "lifi", "deposit"]
        pipeline.bridge.await_deposit_credit.assert_awaited_once_with(
            TO_INFO.chain_id, DEPOSIT_TX, 60000)

    async def test_a_reverted_lifi_tx_refunds_once(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        pipeline.batcher.outcomes["lifi"] = "reverted"
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[FROM_INFO, TO_INFO, FROM_INFO])

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "refunded"
        *_, refund = _txs()
        assert _kinds() == ["approve", "lifi", "refund"]
        assert (refund["to_address"], refund["data"]) == (
            FROM_INFO.token_address, _erc20("transfer", DEPOSIT_ADDRESS, 1000000))
        pipeline.bridge.await_deposit_credit.assert_awaited_once_with(
            FROM_INFO.chain_id, DEPOSIT_TX, 1000000)
        privana = await pipeline._privana_factory()
        payouts = [c.args[0] for c in privana.transfer_funds.await_args_list[1:]]
        paid = [(p.to_address, p.token_id, p.amount) for p in payouts]
        assert paid == [(USER, FROM_TOKEN, 1000000)]

    async def test_a_lifi_tx_that_failed_after_a_signed_attempt_parks(self, settings, insert_quote):
        # A signed attempt may have reached a node and spent the input.
        pipeline = _make_pipeline(settings)
        pipeline.batcher.outcomes["lifi"] = "refused"

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "failed"
        assert row["error"].endswith("manual recovery required")
        assert _kinds() == ["approve", "lifi"]
        privana = await pipeline._privana_factory()
        assert privana.transfer_funds.await_count == 1

    async def test_parallel_swaps_into_one_token_each_credit_their_own_output(
        self, settings, insert_quote
    ):
        pipeline = _make_pipeline(settings)
        infos = {FROM_TOKEN: FROM_INFO, TO_TOKEN: TO_INFO}
        pipeline.accounting.get_token_info = AsyncMock(side_effect=infos.__getitem__)
        pool_nonce = 70
        privana = await pipeline._privana_factory()

        async def transfer_funds(request):
            nonlocal pool_nonce
            if request.to_address == USER:
                pool_nonce += 1
            return MagicMock(status="submitted", detail=None)

        async def get_transfer_nonce(address):
            return 6 if address == USER else pool_nonce
        privana.transfer_funds = AsyncMock(side_effect=transfer_funds)
        pipeline.accounting.get_transfer_nonce = AsyncMock(side_effect=get_transfer_nonce)
        pipeline.spawn_background = MagicMock()
        swaps = []
        for quote_id in ("qp1", "qp2"):
            insert_quote(quote_id, venue="lifi", user_address=USER,
                         from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
            quote = _quote(quote_id)
            swap_id = _seed_swap(get_db(), quote)
            await pipeline.launch(quote, USER, 5, "0x" + "ab" * 65, swap_id)
            swaps.append((swap_id, quote))
        paid = {swaps[0][0]: 60000, swaps[1][0]: 70000}
        batcher = pipeline.batcher

        def result_with_its_swaps_output(tx_id):
            swap_id = evm_batcher.result(tx_id)["owner"].split(":")[1]
            batcher.receipt = _swap_receipt(paid[swap_id])
            return FakeBatcher.result(batcher, tx_id)
        batcher.result = result_with_its_swaps_output

        await asyncio.gather(*(pipeline._run(swap_id, quote, 5) for swap_id, quote in swaps))

        ids = {tx["owner"]: tx["id"] for tx in _txs()}
        for swap_id, _ in swaps:
            row = _swap_row(swap_id)
            assert row["status"] == "completed"
            assert row["to_amount_actual"] == str(calculate_fee(paid[swap_id], 10)[0])
            assert ids[f"swap:{swap_id}:lifi"] == ids[f"swap:{swap_id}:approve"] + 1

    @pytest.mark.parametrize("receipt", [
        {"status": 1, "to": DIAMOND, "logs": []},
        _swap_receipt(60000, emitter="0x" + "66" * 20),
        _swap_receipt(60000, receiver="0x" + "77" * 20),
        _swap_receipt(60000, token="0x" + "99" * 20),
    ], ids=["no event", "another emitter", "another receiver", "another token"])
    async def test_an_output_the_receipt_does_not_show_parks(
        self, settings, insert_quote, receipt
    ):
        pipeline = _make_pipeline(settings)
        pipeline.batcher.receipt = receipt
        pipeline._refund = AsyncMock()

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "failed"
        assert "manual recovery required" in row["error"]
        pipeline._refund.assert_not_awaited()
        assert _kinds() == ["approve", "lifi"]

    def test_reads_the_output_of_a_real_swap(self):
        # Base, 10-02: USDC to LINK for the swap pool, tx 0x0183af8e...
        from src.services.swap.lifi_pipeline import _swap_output
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


class TestBridgeOutcome:
    """A cross-chain swap credits what LiFi reports delivering to the swap pool."""

    TO_ETH = TO_INFO.model_copy(update={"chain_id": 1})

    def _pipeline(self, settings, *statuses):
        pipeline = _make_pipeline(settings)
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[FROM_INFO, self.TO_ETH])
        pipeline.lifi.get_status = AsyncMock(side_effect=list(statuses))
        return pipeline

    async def test_credits_the_delivery_once_lifi_has_indexed_it(
        self, settings, insert_quote, caplog
    ):
        pipeline = self._pipeline(
            settings,
            httpx.HTTPStatusError(
                "not indexed", request=MagicMock(), response=httpx.Response(404)
            ),
            httpx.ConnectError("lifi down"),
            {"status": "PENDING", "substatus": "WAIT_DESTINATION_TRANSACTION"},
            _bridged(60000, chain_id=1),
        )

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "completed"
        assert row["to_amount_actual"] == str(calculate_fee(60000, 10)[0])
        assert row["lifi_tx_hash"] == LIFI_TX
        assert pipeline.lifi.get_status.await_args.args == (LIFI_TX, FROM_INFO.chain_id, 1)
        pipeline.bridge.await_deposit_credit.assert_awaited_once_with(1, DEPOSIT_TX, 60000)
        # The 404 is LiFi not having indexed the tx yet. Only the outage warns.
        assert caplog.text.count("lifi status of") == 1

    @pytest.mark.parametrize("status", [
        {"status": "FAILED"},
        {"status": "INVALID"},
        _bridged(60000, chain_id=1, substatus="PARTIAL"),
        _bridged(60000, chain_id=1, substatus="REFUNDED"),
        {**_bridged(60000, chain_id=1), "toAddress": "0x" + "77" * 20},
        _bridged(60000, chain_id=10),
        {**_bridged(60000, chain_id=1), "receiving": {
            "chainId": 1, "amount": "60000", "token": {"address": "0x" + "99" * 20}}},
    ], ids=["failed", "invalid", "partial", "refunded", "another recipient",
            "another chain", "another token"])
    async def test_anything_but_full_delivery_to_the_pool_parks(
        self, settings, insert_quote, status
    ):
        pipeline = self._pipeline(settings, status)
        pipeline._refund = AsyncMock()

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "failed"
        assert "manual recovery required" in row["error"]
        pipeline._refund.assert_not_awaited()
        assert _kinds() == ["approve", "lifi"]


class TestFundingGate:
    """The LiFi tx spends the withdrawn input, so it waits for it on chain."""

    async def test_waits_for_the_input_before_quoting_and_sending(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        pipeline.evm.balance_of = MagicMock(side_effect=_reads(0, 999_999, 1_000_000, 1_000_000))

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "completed"
        pipeline.lifi.get_execution_quote.assert_awaited_once()
        assert _kinds() == ["approve", "lifi", "deposit"]
        # An ERC-20 input leaves the gas to the native balance.
        pipeline.evm.max_gas_cost.assert_not_called()

    async def test_a_balance_another_swap_spent_meanwhile_is_waited_for(
        self, settings, insert_quote
    ):
        pipeline = _make_pipeline(settings)
        pipeline.evm.balance_of = MagicMock(side_effect=_reads(1_000_000, 0, 1_000_000, 1_000_000))

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "completed"
        assert pipeline.lifi.get_execution_quote.await_count == 2
        assert _kinds() == ["approve", "lifi", "deposit"]

    async def test_a_native_input_also_waits_for_the_gas_its_tx_pays(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        native = TokenInfo(**{**FROM_INFO.__dict__, "token_address": None})
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[native, TO_INFO])
        pipeline.evm.max_gas_cost = MagicMock(return_value=5_000)
        # The input is there before the gas is.
        pipeline.evm.balance_of = MagicMock(side_effect=_reads(1_000_000, 1_000_000, 1_005_000, 1_005_000))

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "completed"
        pipeline.evm.max_gas_cost.assert_called_with(int(EXEC_QUOTE["transactionRequest"]["gasLimit"], 16))
        assert pipeline.lifi.get_execution_quote.await_count == 2
        assert _kinds() == ["lifi", "deposit"]

    async def test_an_input_that_never_arrives_parks_without_sending(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        pipeline.evm.balance_of = MagicMock(return_value=0)
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[FROM_INFO, TO_INFO, FROM_INFO])
        privana = await pipeline._privana_factory()

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "failed"
        assert "manual recovery required" in row["error"]
        pipeline.lifi.get_execution_quote.assert_not_awaited()
        assert _txs() == []
        # Only the user's input transfer: no refund while the input is off chain.
        assert privana.transfer_funds.await_count == 1


class TestQueuedTxs:
    """What a swap hands the batcher to send."""

    async def test_the_approval_and_the_swap_are_queued_together(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        spender = "0x" + "5a" * 20
        estimate = {**EXEC_QUOTE["estimate"], "approvalAddress": spender}
        pipeline.lifi.get_execution_quote = AsyncMock(return_value={**EXEC_QUOTE, "estimate": estimate})

        row = await _swap(pipeline, insert_quote)

        approve, swap, deposit = _txs()
        assert row["status"] == "completed"
        # One submit: no other swap's approval lands between them.
        assert swap["id"] == approve["id"] + 1
        assert (approve["owner"], approve["to_address"], approve["data"], approve["value"]) == (
            f"swap:{row['id']}:approve", FROM_INFO.token_address,
            _erc20("approve", spender, 1_000_000), "0")
        # It cannot be simulated before its approval is mined.
        assert (swap["owner"], swap["to_address"], swap["data"], swap["value"], swap["gas"],
                swap["simulate"]) == (f"swap:{row['id']}:lifi", DIAMOND, "0xdead", "0", 0x15fcbf, 0)
        assert (deposit["owner"], deposit["to_address"], deposit["data"]) == (
            f"swap:{row['id']}:deposit", TO_INFO.token_address,
            _erc20("transfer", DEPOSIT_ADDRESS, 60000))

    async def test_a_native_input_queues_only_the_swap_simulated_with_its_value(
        self, settings, insert_quote
    ):
        pipeline = _make_pipeline(settings)
        native = TokenInfo(**{**FROM_INFO.__dict__, "token_address": None})
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[native, TO_INFO])
        request = {**EXEC_QUOTE["transactionRequest"], "value": hex(1_000_000)}
        pipeline.lifi.get_execution_quote = AsyncMock(
            return_value={**EXEC_QUOTE, "transactionRequest": request})

        row = await _swap(pipeline, insert_quote)

        swap, _ = _txs()
        assert row["status"] == "completed"
        assert _kinds() == ["lifi", "deposit"]
        assert (swap["to_address"], swap["data"], swap["value"], swap["simulate"]) == (
            DIAMOND, "0xdead", "1000000", 1)

    async def test_a_tx_built_for_another_chain_is_never_queued(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[FROM_INFO, TO_INFO, FROM_INFO])
        request = {**EXEC_QUOTE["transactionRequest"], "chainId": 1}
        pipeline.lifi.get_execution_quote = AsyncMock(
            return_value={**EXEC_QUOTE, "transactionRequest": request})

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "refunded"
        assert f"built for chain 1 cannot be sent on chain {FROM_INFO.chain_id}" in row["error"]
        assert _kinds() == ["refund"]


class TestResume:
    """A restart picks a swap up where its row says it stopped."""

    def _seed(self, pipeline, insert_quote, quote_id, **fields):
        insert_quote(quote_id, venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        quote = _quote(quote_id)
        swap_id = _seed_swap(get_db(), quote)
        pipeline._update_swap(
            swap_id, status="executing", input_nonce="5", withdrawal_index=17, **fields)
        return swap_id, quote

    async def test_a_sent_lifi_tx_is_settled_not_sent_again(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        swap_id, quote = self._seed(pipeline, insert_quote, "qr1", step="lifi_execute")
        pipeline.batcher.submit(FROM_INFO.chain_id, TxIntent(
            owner=f"swap:{swap_id}:lifi", to=DIAMOND, gas=0x15fcbf, data="0xdead", simulate=False))

        await pipeline._run(swap_id, quote, 5)

        row = _swap_row(swap_id)
        assert row["status"] == "completed"
        assert row["to_amount_actual"] == str(calculate_fee(60000, 10)[0])
        assert _kinds() == ["lifi", "deposit"]
        pipeline.lifi.get_execution_quote.assert_not_awaited()
        pipeline.bridge.withdraw_to_chain.assert_not_awaited()

    async def test_an_unsent_lifi_tx_is_sent(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        swap_id, quote = self._seed(pipeline, insert_quote, "qr2", step="lifi_execute")

        await pipeline._run(swap_id, quote, 5)

        assert _swap_row(swap_id)["status"] == "completed"
        assert _kinds() == ["approve", "lifi", "deposit"]
        pipeline.bridge.withdraw_to_chain.assert_not_awaited()

    async def test_a_sent_deposit_is_awaited_not_sent_again(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        pipeline.accounting.get_token_info = AsyncMock(return_value=TO_INFO)
        swap_id, quote = self._seed(
            pipeline, insert_quote, "qr3", step="deposit", lifi_tx_hash=LIFI_TX,
            to_amount_received="60000")
        pipeline.batcher.submit(TO_INFO.chain_id, transfer_intent(
            f"swap:{swap_id}:deposit", TO_INFO.token_address, DEPOSIT_ADDRESS, 60000))

        await pipeline._run(swap_id, quote, 5)

        assert _swap_row(swap_id)["status"] == "completed"
        assert _kinds() == ["deposit"]
        pipeline.bridge.get_deposit_address.assert_not_awaited()
        pipeline.bridge.await_deposit_credit.assert_awaited_once_with(
            TO_INFO.chain_id, DEPOSIT_TX, 60000)

    async def test_an_unsent_deposit_is_sent(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        pipeline.accounting.get_token_info = AsyncMock(return_value=TO_INFO)
        swap_id, quote = self._seed(
            pipeline, insert_quote, "qr4", step="deposit", lifi_tx_hash=LIFI_TX,
            to_amount_received="60000")

        await pipeline._run(swap_id, quote, 5)

        assert _swap_row(swap_id)["status"] == "completed"
        [deposit] = _txs()
        assert (deposit["owner"], deposit["to_address"], deposit["data"]) == (
            f"swap:{swap_id}:deposit", TO_INFO.token_address,
            _erc20("transfer", DEPOSIT_ADDRESS, 60000))

    async def test_recovery_resumes_swaps_mid_execution(self, settings, insert_quote):
        from src.services.swap.lifi_pipeline import recover_inflight_lifi_swaps
        pipeline = _make_pipeline(settings)
        pipeline.spawn_background = MagicMock()
        pipeline._refund = AsyncMock()
        executing, _ = self._seed(pipeline, insert_quote, "qr5", step="lifi_execute")
        depositing, _ = self._seed(
            pipeline, insert_quote, "qr6", step="deposit", to_amount_received="60000")

        await recover_inflight_lifi_swaps(pipeline=pipeline)

        resumed = {c.args[0]: c.args for c in pipeline.spawn_background.call_args_list}
        assert set(resumed) == {executing, depositing}
        stored = dict(get_db().execute("SELECT * FROM quotes WHERE id='qr5'").fetchone())
        assert resumed[executing][1:] == (stored, 5)
        pipeline._refund.assert_not_awaited()

    @pytest.mark.parametrize(
        "runner_started", [False, True], ids=["runner_queued", "runner_polling"]
    )
    async def test_a_retried_recovery_pays_a_resumed_swap_once(
        self, settings, insert_quote, runner_started
    ):
        from src.services.swap.lifi_pipeline import recover_inflight_lifi_swaps
        pipeline = _make_pipeline(settings)
        pipeline.accounting.get_token_info = AsyncMock(return_value=TO_INFO)
        resumed, _ = self._seed(
            pipeline, insert_quote, "qr9", step="deposit", lifi_tx_hash=LIFI_TX,
            to_amount_received="60000")
        later, _ = self._seed(pipeline, insert_quote, "qr10", step="credit")
        # The first pass fails at the later row, so the worker runs it again.
        park = pipeline._park

        def park_after_a_lock(swap_id, reason):
            pipeline._park = park
            raise sqlite3.OperationalError("database is locked")
        pipeline._park = park_after_a_lock
        # The rerun comes before the resumed runner starts, or while it awaits its credit.
        polling = asyncio.Event()
        credited = asyncio.Event()

        async def await_credit(*_):
            polling.set()
            await credited.wait()
        pipeline.bridge.await_deposit_credit = AsyncMock(side_effect=await_credit)

        with pytest.raises(sqlite3.OperationalError):
            await recover_inflight_lifi_swaps(pipeline=pipeline)
        if runner_started:
            await asyncio.wait_for(polling.wait(), 5)
        await recover_inflight_lifi_swaps(pipeline=pipeline)
        credited.set()
        # Every runner, registered or not.
        await asyncio.gather(*asyncio.all_tasks() - {asyncio.current_task()})

        privana = await pipeline._privana_factory()
        privana.transfer_funds.assert_awaited_once()
        assert _swap_row(resumed)["status"] == "completed"
        assert "manual recovery required" in _swap_row(later)["error"]

    async def test_a_swap_gets_one_runner(self, settings):
        pipeline = _make_pipeline(settings)
        released = asyncio.Event()

        async def run(*_):
            await released.wait()
        pipeline._run = AsyncMock(side_effect=run)
        pipeline.spawn_background("swap", {}, 1)
        pipeline.spawn_background("swap", {}, 2)
        released.set()
        await pipeline._tasks["swap"]

        pipeline._run.assert_called_once_with("swap", {}, 1)
        assert pipeline._tasks == {}

    async def test_recovery_leaves_a_swap_to_its_runner(self, settings, insert_quote):
        from src.services.swap.lifi_pipeline import recover_inflight_lifi_swaps
        pipeline = _make_pipeline(settings)
        released = asyncio.Event()

        async def run(*_):
            await released.wait()
        pipeline._run = AsyncMock(side_effect=run)
        pipeline._refund = AsyncMock()
        owned = [
            self._seed(pipeline, insert_quote, "qr11", step="deposit", to_amount_received="60000"),
            self._seed(pipeline, insert_quote, "qr12", step="credit"),
            self._seed(pipeline, insert_quote, "qr13", step="lifi_execute"),
        ]
        pipeline._update_swap(owned[2][0], status="refunding")
        for swap_id, quote in owned:
            pipeline.spawn_background(swap_id, quote, 5)

        await recover_inflight_lifi_swaps(pipeline=pipeline)
        released.set()
        await asyncio.gather(*pipeline._tasks.values())

        assert pipeline._run.call_count == 3
        pipeline._refund.assert_not_awaited()
        assert [_swap_row(swap_id)["status"] for swap_id, _ in owned] == [
            "executing", "executing", "refunding"]

    async def test_recovery_refunds_in_the_background_and_goes_on(self, settings, insert_quote):
        # A refund waiting on its tx must not hold up the swaps after it.
        from src.services.swap.lifi_pipeline import recover_inflight_lifi_swaps
        pipeline = _make_pipeline(settings)
        refunding, _ = self._seed(pipeline, insert_quote, "qr14", step="withdraw")
        resumed, _ = self._seed(pipeline, insert_quote, "qr15", step="lifi_execute")
        released = asyncio.Event()

        async def refund(*_):
            await released.wait()
        pipeline._refund = AsyncMock(side_effect=refund)
        pipeline._run = AsyncMock()

        await recover_inflight_lifi_swaps(pipeline=pipeline)

        assert set(pipeline._tasks) == {refunding, resumed}
        released.set()
        await asyncio.gather(*pipeline._tasks.values())
        assert [c.args[0] for c in pipeline._refund.await_args_list] == [refunding]
        pipeline._run.assert_awaited_once()
