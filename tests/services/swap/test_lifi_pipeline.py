import asyncio
import sqlite3
import time
import uuid
from dataclasses import replace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx
import pytest
from eth_abi import encode
from hexbytes import HexBytes
from web3 import Web3

from src.core.db import db_write, get_db
from src.core.fees import calculate_fee
from src.models.common import TokenInfo

USER = "0x" + "a" * 40
POOL = "0x152E6a7125665764a4F1F1df80E8f5D49Bf0239c"
DIAMOND = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"
LIFI_TX = "0x" + "cd" * 32
DEPOSIT_TX = "0x" + "ef" * 32
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
MINED = {"status": 1, "to": None, "logs": []}
REVERTED = {"status": 0, "to": DIAMOND, "logs": []}


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


def _raw(tx_hash):
    """Fake signed bytes for `tx_hash`."""
    return "0x02" + tx_hash[2:]


def _sends(tx_hash, nonce, error=None):
    """A send that hands over the signed tx before broadcast, then mines or raises."""
    def send(*_, on_signed):
        on_signed(tx_hash, nonce, _raw(tx_hash))
        if error is not None:
            raise error
        return tx_hash
    return send


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


def _evm(receipts=None):
    """The swap pool wallet on one chain: funded, and every send mines at once."""
    receipts = {LIFI_TX: _swap_receipt(60000), **(receipts or {})}
    evm = MagicMock()
    evm.address = POOL
    evm.tx_lock = asyncio.Lock()
    evm.balance_of = MagicMock(return_value=10**18)
    evm.max_gas_cost = MagicMock(return_value=10**15)
    evm.ensure_allowance = MagicMock(return_value=None)
    evm.send_transaction_request = MagicMock(side_effect=_sends(LIFI_TX, 40))
    evm.transfer = MagicMock(side_effect=_sends(DEPOSIT_TX, 41))
    evm.get_receipt = MagicMock(side_effect=lambda tx_hash: receipts.get(tx_hash, MINED))
    return evm


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
    bridge.get_deposit_address = AsyncMock(return_value="0x" + "dd" * 20)
    bridge.await_deposit_credit = AsyncMock(return_value=None)

    evm = _evm()

    privana = MagicMock()
    privana.transfer_funds = AsyncMock(return_value=MagicMock(status="submitted", detail=None))

    async def privana_factory():
        return privana

    pipeline = LifiSwapPipeline(
        accounting=accounting, lifi=lifi, bridge=bridge, evm=evm,
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
        pipeline.evm.send_transaction_request.assert_not_called()

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

    async def test_a_reverted_deposit_transfer_is_resent(self, test_db, settings, insert_quote):
        insert_quote("q5", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        reverted = "0x" + "e0" * 32
        pipeline.evm = _evm({reverted: REVERTED})
        pipeline.evm.transfer = MagicMock(side_effect=_in_turn(
            _sends(reverted, 41, error=RuntimeError(f"transaction reverted: {reverted}")),
            _sends(DEPOSIT_TX, 42),
        ))
        swap_id = await self._launch_and_run(pipeline, _quote("q5"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        assert row["status"] == "completed"
        assert (row["deposit_tx_hash"], row["deposit_tx_raw"]) == (DEPOSIT_TX, _raw(DEPOSIT_TX))
        assert pipeline.evm.transfer.call_count == 2
        pipeline.bridge.await_deposit_credit.assert_awaited_once_with(
            TO_INFO.chain_id, DEPOSIT_TX, 60000)

    async def test_a_re_signed_deposit_is_credited_by_the_version_mined(
        self, test_db, settings, insert_quote
    ):
        from src.clients.base_evm import TransactionPendingError
        insert_quote("q9", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        re_signed = "0x" + "e2" * 32
        pipeline.evm.transfer = MagicMock(
            side_effect=_sends(DEPOSIT_TX, 41, error=TransactionPendingError(DEPOSIT_TX)))
        receipts = {LIFI_TX: _swap_receipt(60000)}

        def rebroadcast(raw, on_signed):
            on_signed(re_signed, 41, _raw(re_signed))
            receipts[re_signed] = MINED
        pipeline.evm.rebroadcast = MagicMock(side_effect=rebroadcast)
        pipeline.evm.get_receipt = MagicMock(side_effect=receipts.get)
        swap_id = await self._launch_and_run(pipeline, _quote("q9"))
        row = _swap_row(swap_id)
        assert row["status"] == "completed"
        assert (row["deposit_tx_hash"], row["deposit_tx_replaced"]) == (re_signed, DEPOSIT_TX)
        pipeline.evm.transfer.assert_called_once()
        pipeline.bridge.await_deposit_credit.assert_awaited_once_with(
            TO_INFO.chain_id, re_signed, 60000)

    async def test_a_re_signed_deposit_whose_first_version_reverted_is_sent_anew(
        self, test_db, settings, insert_quote
    ):
        from src.clients.base_evm import TransactionPendingError
        insert_quote("q10", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        first, re_signed = "0x" + "e1" * 32, "0x" + "e2" * 32
        pipeline.evm.transfer = MagicMock(side_effect=_in_turn(
            _sends(first, 41, error=TransactionPendingError(first)), _sends(DEPOSIT_TX, 42),
        ))
        receipts = {LIFI_TX: _swap_receipt(60000)}

        def rebroadcast(raw, on_signed):
            if raw == _raw(first):
                on_signed(re_signed, 41, _raw(re_signed))
                # The first version takes nonce 41, and reverts.
                receipts[first] = REVERTED
            else:
                receipts[DEPOSIT_TX] = MINED
        pipeline.evm.rebroadcast = MagicMock(side_effect=rebroadcast)
        pipeline.evm.get_receipt = MagicMock(side_effect=receipts.get)
        swap_id = await self._launch_and_run(pipeline, _quote("q10"))
        row = _swap_row(swap_id)
        assert row["status"] == "completed"
        # The new transfer starts its own versions: the old revert never settles it.
        assert (row["deposit_tx_hash"], row["deposit_tx_replaced"]) == (DEPOSIT_TX, None)
        assert pipeline.evm.transfer.call_count == 2
        pipeline.bridge.await_deposit_credit.assert_awaited_once_with(
            TO_INFO.chain_id, DEPOSIT_TX, 60000)

    async def test_a_deposit_transfer_that_never_signed_is_retried(
        self, test_db, settings, insert_quote
    ):
        insert_quote("q7", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline.evm.transfer = MagicMock(side_effect=_in_turn(
            MagicMock(side_effect=ConnectionError("rpc down")), _sends(DEPOSIT_TX, 41),
        ))
        swap_id = await self._launch_and_run(pipeline, _quote("q7"))
        assert _swap_row(swap_id)["status"] == "completed"
        assert pipeline.evm.transfer.call_count == 2

    async def test_a_lost_deposit_send_is_awaited_not_resent(
        self, test_db, settings, insert_quote
    ):
        # The send's response is lost and the probed node lags: it lacks the tx
        # and still reports nonce 7 free. A resend would take nonce 8 and pay twice.
        from web3 import Web3
        from web3.exceptions import TransactionNotFound

        from src.clients.base_evm import EvmClient
        insert_quote("q6", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        client = EvmClient("http://localhost:1", "0x" + "11" * 32)
        client.w3 = MagicMock()
        client.w3.eth.max_priority_fee = 0
        client.w3.eth.get_block.return_value = {"baseFeePerGas": 1}
        token = client.w3.eth.contract.return_value
        token.functions.transfer.return_value.build_transaction = lambda tx: tx
        # The tx's nonce and the probe's latest and pending are 7. A resend gets 8.
        nonces = iter([7, 7, 7])
        client.w3.eth.get_transaction_count.side_effect = lambda *_: next(nonces, 8)
        client.w3.eth.send_raw_transaction.side_effect = ValueError("already known")
        client.w3.eth.get_transaction.side_effect = TransactionNotFound("unknown")
        pipeline.evm.transfer = client.transfer
        signed = MagicMock(raw_transaction=b"raw")
        with patch.object(client._account, "sign_transaction", return_value=signed):
            swap_id = await self._launch_and_run(pipeline, _quote("q6"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        sent = Web3.to_hex(Web3.keccak(b"raw"))
        assert row["status"] == "completed"
        assert row["deposit_tx_hash"] == sent
        client.w3.eth.send_raw_transaction.assert_called_once()
        pipeline.bridge.await_deposit_credit.assert_awaited_once_with(
            TO_INFO.chain_id, sent, 60000
        )

    async def test_a_deposit_whose_receipt_never_shows_is_not_sent_again(
        self, test_db, settings, insert_quote
    ):
        # A lagging node shows no receipt for a mined deposit, and a second deposit pays twice.
        insert_quote("q8", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline._deposit_max_retries = 2
        pipeline.evm.get_receipt = MagicMock(
            side_effect=lambda tx_hash: None if tx_hash == DEPOSIT_TX else _swap_receipt(60000))
        swap_id = await self._launch_and_run(pipeline, _quote("q8"))
        row = _swap_row(swap_id)
        assert row["status"] == "failed"
        assert "deposit retries exhausted" in row["error"]
        assert row["error"].endswith("manual recovery required")
        assert row["deposit_tx_hash"] == DEPOSIT_TX
        pipeline.evm.transfer.assert_called_once()
        pipeline.bridge.await_deposit_credit.assert_not_awaited()


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
        pipeline.evm.send_transaction_request.assert_not_called()

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
        pipeline.evm.send_transaction_request.assert_not_called()

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
        pipeline.evm.send_transaction_request = MagicMock(side_effect=ConnectionError("rpc down"))
        pipeline.accounting.get_transfer_nonce = AsyncMock(side_effect=[6, 70, 71])
        pipeline.accounting.get_token_info = AsyncMock(
            side_effect=[FROM_INFO, TO_INFO, FROM_INFO])
        swap_id = await self._launch_and_run(pipeline, _quote("q_e"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        assert row["status"] == "refunded"
        pipeline.evm.transfer.assert_called_once()
        pipeline.bridge.await_deposit_credit.assert_awaited_once()

    async def test_unconfirmed_redeposit_is_awaited_not_resent(self, test_db, settings, insert_quote):
        from src.clients.base_evm import TransactionPendingError
        insert_quote("q_p", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline.evm.send_transaction_request = MagicMock(side_effect=ConnectionError("rpc down"))
        pending = "0x" + "aa" * 32
        pipeline.evm.transfer = MagicMock(
            side_effect=_sends(pending, 41, error=TransactionPendingError(pending)))
        pipeline.accounting.get_transfer_nonce = AsyncMock(side_effect=[6, 70, 71])
        pipeline.accounting.get_token_info = AsyncMock(
            side_effect=[FROM_INFO, TO_INFO, FROM_INFO])
        swap_id = await self._launch_and_run(pipeline, _quote("q_p"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        assert row["status"] == "refunded"
        pipeline.evm.transfer.assert_called_once()
        pipeline.bridge.await_deposit_credit.assert_awaited_once_with(
            FROM_INFO.chain_id, pending, 1000000)

    async def test_deposit_exhaustion_fails_without_refund(self, test_db, settings, insert_quote):
        insert_quote("q_d", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline._deposit_max_retries = 2
        pipeline.bridge.await_deposit_credit = AsyncMock(side_effect=RuntimeError("relay stuck"))
        swap_id = await self._launch_and_run(pipeline, _quote("q_d"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        assert row["status"] == "failed"
        assert "deposit" in row["error"]
        pipeline.evm.transfer.assert_called_once()
        assert pipeline.bridge.await_deposit_credit.await_count == 2

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
        from src.services.swap.lifi_pipeline import recover_inflight_lifi_swaps
        insert_quote("q_rec", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        self._insert_swap(test_db, "s_withdraw", "withdraw")
        self._insert_swap(test_db, "s_credit", "credit")
        pipeline = _make_pipeline(settings)
        pipeline._refund = AsyncMock()
        await recover_inflight_lifi_swaps(pipeline=pipeline)
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

    async def test_a_native_coin_goes_to_lifi_as_the_zero_address(
        self, test_db, settings, insert_quote, monkeypatch
    ):
        pipeline = _make_pipeline(settings)
        pipeline.evm = None
        hyperevm, base = _evm(), _evm()
        clients = {999: hyperevm, 84532: base}
        monkeypatch.setattr("src.services.swap.lifi_pipeline.get_evm_client", clients.__getitem__)
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[
            self._info(FROM_INFO, 999, ""), TO_INFO,
        ])
        pipeline.lifi.get_status = AsyncMock(return_value=_bridged(60000, chain_id=84532))

        row = await self._run(pipeline, test_db, insert_quote)

        assert row["status"] == "completed"
        call = pipeline.lifi.get_execution_quote.call_args.kwargs
        assert call["from_token_address"] == self.NATIVE
        assert call["from_chain_id"] == 999
        # The swap is signed on HyperEVM; the output is returned on Base.
        hyperevm.send_transaction_request.assert_called_once()
        hyperevm.balance_of.assert_called_with("", POOL)
        hyperevm.get_receipt.assert_any_call(LIFI_TX)
        base.send_transaction_request.assert_not_called()
        base.transfer.assert_called_once()
        hyperevm.transfer.assert_not_called()

    async def test_native_output_on_the_same_chain_is_not_short_by_the_gas(
        self, test_db, settings, insert_quote
    ):
        pipeline = _make_pipeline(settings)
        # Gas moves the wallet balance, not the event.
        pipeline.evm = _evm({LIFI_TX: _swap_receipt(60000, token=self.NATIVE)})
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
        pipeline.evm = None
        hyperevm, base = _evm({LIFI_TX: REVERTED}), _evm()
        hyperevm.send_transaction_request = MagicMock(
            side_effect=_sends(LIFI_TX, 40, error=RuntimeError(f"transaction reverted: {LIFI_TX}")))
        clients = {999: hyperevm, 84532: base}
        monkeypatch.setattr("src.services.swap.lifi_pipeline.get_evm_client", clients.__getitem__)
        native = self._info(FROM_INFO, 999, None)
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[native, TO_INFO, native])
        pipeline.accounting.get_transfer_nonce = AsyncMock(side_effect=[6, 70, 71])

        row = await self._run(pipeline, test_db, insert_quote)

        assert row["status"] == "refunded"
        hyperevm.transfer.assert_called_once()
        assert hyperevm.transfer.call_args.args[0] is None
        base.transfer.assert_not_called()


class TestSettleByOutcome:
    """A sent LiFi tx settles by its receipt and LiFi's status, never by an error."""

    async def test_a_receipt_after_the_send_timed_out_credits_and_never_refunds(
        self, settings, insert_quote
    ):
        from src.clients.base_evm import TransactionPendingError
        pipeline = _make_pipeline(settings)
        pipeline.evm.send_transaction_request = MagicMock(
            side_effect=_sends(LIFI_TX, 40, error=TransactionPendingError(LIFI_TX)))
        # Behind a load-balanced RPC, the node asked can lag the block for many polls.
        late = iter([None] * 20 + [_swap_receipt(60000)])
        pipeline.evm.get_receipt = MagicMock(
            side_effect=lambda tx_hash: next(late) if tx_hash == LIFI_TX else MINED)
        pipeline._refund = AsyncMock()

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "completed"
        assert row["to_amount_actual"] == str(calculate_fee(60000, 10)[0])
        pipeline._refund.assert_not_awaited()
        pipeline.evm.send_transaction_request.assert_called_once()
        pipeline.bridge.await_deposit_credit.assert_awaited_once_with(
            TO_INFO.chain_id, DEPOSIT_TX, 60000)

    async def test_the_tx_is_recorded_before_it_is_broadcast(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        recorded = []

        def send(*_, on_signed):
            on_signed(LIFI_TX, 40, _raw(LIFI_TX))
            row = _swap_row(swap_id)
            recorded.append((row["lifi_tx_hash"], row["lifi_tx_raw"]))
            return LIFI_TX
        pipeline.evm.send_transaction_request = MagicMock(side_effect=send)
        insert_quote("qb", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        quote = _quote("qb")
        pipeline.spawn_background = MagicMock()
        swap_id = _seed_swap(get_db(), quote)
        await pipeline.launch(quote, USER, 5, "0x" + "ab" * 65, swap_id)

        await pipeline._run(swap_id, quote, 5)

        assert recorded == [(LIFI_TX, _raw(LIFI_TX))]

    async def test_a_reverted_lifi_tx_refunds_once(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        pipeline.evm = _evm({LIFI_TX: REVERTED})
        pipeline.evm.send_transaction_request = MagicMock(
            side_effect=_sends(LIFI_TX, 40, error=RuntimeError(f"transaction reverted: {LIFI_TX}")))
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[FROM_INFO, TO_INFO, FROM_INFO])

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "refunded"
        pipeline.evm.transfer.assert_called_once()
        assert pipeline.evm.transfer.call_args.args[0] == FROM_INFO.token_address
        pipeline.bridge.await_deposit_credit.assert_awaited_once_with(
            FROM_INFO.chain_id, DEPOSIT_TX, 1000000)
        privana = await pipeline._privana_factory()
        payouts = [c.args[0] for c in privana.transfer_funds.await_args_list[1:]]
        paid = [(p.to_address, p.token_id, p.amount) for p in payouts]
        assert paid == [(USER, FROM_TOKEN, 1000000)]

    async def test_a_receipt_that_never_shows_parks(self, settings, insert_quote):
        # A missing receipt cannot tell a dropped tx from a lagging node, whatever the nonce.
        from src.clients.base_evm import TransactionPendingError
        from src.services.swap.lifi_pipeline import TX_RECEIPT_POLLS
        pipeline = _make_pipeline(settings)
        pipeline.evm.send_transaction_request = MagicMock(
            side_effect=_sends(LIFI_TX, 40, error=TransactionPendingError(LIFI_TX)))
        pipeline.evm.get_receipt = MagicMock(
            side_effect=lambda tx_hash: None if tx_hash == LIFI_TX else MINED)
        pipeline._refund = AsyncMock()

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "failed"
        assert "manual recovery required" in row["error"]
        pipeline._refund.assert_not_awaited()
        pipeline.evm.transfer.assert_not_called()
        assert pipeline.evm.get_receipt.call_count == TX_RECEIPT_POLLS

    async def test_a_tx_no_node_holds_is_sent_again_unchanged(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        lost = iter([None, _swap_receipt(60000)])
        pipeline.evm.get_receipt = MagicMock(
            side_effect=lambda tx_hash: next(lost) if tx_hash == LIFI_TX else MINED)

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "completed"
        pipeline.evm.rebroadcast.assert_called_once_with(_raw(LIFI_TX), ANY)
        pipeline.evm.send_transaction_request.assert_called_once()

    @pytest.mark.parametrize("mined", [LIFI_TX, "0x" + "c1" * 32], ids=["original", "re-signed"])
    async def test_a_re_signed_tx_settles_by_the_version_mined(
        self, settings, insert_quote, mined, caplog
    ):
        caplog.set_level("INFO")
        # Cross-chain: LiFi reports the bridge by the source tx hash.
        re_signed = "0x" + "c1" * 32
        pipeline = _make_pipeline(settings)
        pipeline.accounting.get_token_info = AsyncMock(
            side_effect=[FROM_INFO, TO_INFO.model_copy(update={"chain_id": 1})])
        pipeline.lifi.get_status = AsyncMock(return_value=_bridged(60000, chain_id=1))
        receipts = {}

        def rebroadcast(raw, on_signed):
            on_signed(re_signed, 40, _raw(re_signed))
            # Both versions hold nonce 40: whichever is mined settles the swap.
            receipts[mined] = MINED
        pipeline.evm.rebroadcast = MagicMock(side_effect=rebroadcast)
        pipeline.evm.get_receipt = MagicMock(side_effect=lambda tx_hash: (
            receipts.get(tx_hash) if tx_hash in (LIFI_TX, re_signed) else MINED))

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "completed"
        # The version not mined stays a candidate, since a receipt is not final.
        not_mined = re_signed if mined == LIFI_TX else LIFI_TX
        assert (row["lifi_tx_hash"], row["lifi_tx_replaced"]) == (mined, not_mined)
        assert row["lifi_tx_raw"] == _raw(re_signed)
        pipeline.evm.rebroadcast.assert_called_once_with(_raw(LIFI_TX), ANY)
        pipeline.lifi.get_status.assert_awaited_once_with(mined, FROM_INFO.chain_id, 1)
        pipeline.evm.send_transaction_request.assert_called_once()
        assert f"submitting lifi tx {re_signed} at nonce 40" in caplog.text

    async def test_lookup_errors_are_not_outcomes(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        flaky = iter([ConnectionError("rpc down"), _swap_receipt(60000)])

        def get_receipt(tx_hash):
            if tx_hash != LIFI_TX:
                return MINED
            outcome = next(flaky)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        pipeline.evm.get_receipt = MagicMock(side_effect=get_receipt)

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "completed"

    async def test_parallel_swaps_into_one_token_each_credit_their_own_output(
        self, settings, insert_quote
    ):
        pipeline = _make_pipeline(settings)
        second = "0x" + "ce" * 32
        pipeline.evm = _evm({second: _swap_receipt(70000)})
        pipeline.evm.send_transaction_request = MagicMock(
            side_effect=_in_turn(_sends(LIFI_TX, 40), _sends(second, 41)))
        pipeline.evm.transfer = MagicMock(
            side_effect=_in_turn(_sends("0x" + "e1" * 32, 42), _sends("0x" + "e2" * 32, 43)))
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

        await asyncio.gather(*(pipeline._run(swap_id, quote, 5) for swap_id, quote in swaps))

        paid = {LIFI_TX: 60000, second: 70000}
        for swap_id, _ in swaps:
            row = _swap_row(swap_id)
            assert row["status"] == "completed"
            assert row["to_amount_actual"] == str(calculate_fee(paid[row["lifi_tx_hash"]], 10)[0])

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
        pipeline.evm = _evm({LIFI_TX: receipt})
        pipeline._refund = AsyncMock()

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "failed"
        assert "manual recovery required" in row["error"]
        pipeline._refund.assert_not_awaited()
        pipeline.evm.transfer.assert_not_called()

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
        receipt = {"status": 1, "to": "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE", "logs": [log]}

        received = _swap_output(
            receipt, "0xD1cbC91Ab86C43f975A18126cc6AD77cf1F22b8a",
            "0x88Fb150BDc53A65fe94Dea0c9BA0a6dAf8C6e196",
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
        pipeline.evm.transfer.assert_not_called()


class TestFundingGate:
    """The LiFi tx spends the withdrawn input, so it waits for it on chain."""

    async def test_waits_for_the_input_before_quoting_and_sending(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        pipeline.evm.balance_of = MagicMock(side_effect=_reads(0, 999_999, 1_000_000, 1_000_000))

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "completed"
        pipeline.lifi.get_execution_quote.assert_awaited_once()
        pipeline.evm.send_transaction_request.assert_called_once()
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
        pipeline.evm.send_transaction_request.assert_called_once()

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
        pipeline.evm.send_transaction_request.assert_called_once()

    async def test_an_input_that_never_arrives_parks_without_sending(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        pipeline.evm.balance_of = MagicMock(return_value=0)
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[FROM_INFO, TO_INFO, FROM_INFO])
        privana = await pipeline._privana_factory()

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "failed"
        assert "manual recovery required" in row["error"]
        pipeline.lifi.get_execution_quote.assert_not_awaited()
        pipeline.evm.send_transaction_request.assert_not_called()
        pipeline.evm.transfer.assert_not_called()
        # Only the user's input transfer: no refund while the input is off chain.
        assert privana.transfer_funds.await_count == 1


class TestApproval:
    """A receipt timeout does not cancel an approval, so the swap waits for its allowance."""

    APPROVAL_TX = "0x" + "a1" * 32

    def _late(self, pipeline):
        """An approval signed and handed over, whose receipt timed out."""
        from src.clients.base_evm import TransactionPendingError
        pipeline.evm.ensure_allowance = MagicMock(side_effect=_sends(
            self.APPROVAL_TX, 39, error=TransactionPendingError(self.APPROVAL_TX)))

    async def test_a_late_approval_is_waited_for_then_the_swap_is_sent(
        self, settings, insert_quote
    ):
        pipeline = _make_pipeline(settings)
        self._late(pipeline)
        pipeline.evm.allowance = MagicMock(side_effect=_reads(0, 1_000_000))

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "completed"
        pipeline.evm.ensure_allowance.assert_called_once()
        assert pipeline.evm.allowance.call_args.args == (FROM_INFO.token_address, DIAMOND)
        # Kept alive like the swap's txs: priced out, it holds up every later tx of the wallet.
        pipeline.evm.rebroadcast.assert_called_once_with(_raw(self.APPROVAL_TX), ANY)
        pipeline.evm.send_transaction_request.assert_called_once()

    async def test_a_re_signed_approval_is_the_version_rebroadcast(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        self._late(pipeline)
        re_signed = "0x" + "a2" * 32
        pipeline.evm.allowance = MagicMock(side_effect=_reads(0, 0, 1_000_000))
        pipeline.evm.rebroadcast = MagicMock(side_effect=_in_turn(
            lambda raw, on_signed: on_signed(re_signed, 39, _raw(re_signed)),
            lambda raw, on_signed: None,
        ))

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "completed"
        assert [c.args[0] for c in pipeline.evm.rebroadcast.call_args_list] == [
            _raw(self.APPROVAL_TX), _raw(re_signed)]

    async def test_an_approval_that_never_takes_effect_refunds_without_sending(
        self, settings, insert_quote
    ):
        from src.services.swap.lifi_pipeline import APPROVAL_POLLS
        pipeline = _make_pipeline(settings)
        self._late(pipeline)
        pipeline.evm.allowance = MagicMock(return_value=0)
        pipeline.accounting.get_token_info = AsyncMock(side_effect=[FROM_INFO, TO_INFO, FROM_INFO])

        row = await _swap(pipeline, insert_quote)

        assert row["status"] == "refunded"
        assert pipeline.evm.allowance.call_count == APPROVAL_POLLS
        pipeline.evm.send_transaction_request.assert_not_called()


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
        swap_id, quote = self._seed(
            pipeline, insert_quote, "qr1", step="lifi_execute",
            lifi_tx_hash=LIFI_TX, lifi_tx_raw=_raw(LIFI_TX))

        await pipeline._run(swap_id, quote, 5)

        row = _swap_row(swap_id)
        assert row["status"] == "completed"
        assert row["to_amount_actual"] == str(calculate_fee(60000, 10)[0])
        pipeline.evm.send_transaction_request.assert_not_called()
        pipeline.bridge.withdraw_to_chain.assert_not_awaited()

    async def test_a_lifi_tx_recorded_but_never_broadcast_is_broadcast(
        self, settings, insert_quote
    ):
        # Saved, then stopped before broadcast. A fresh client has only the row.
        from eth_account import Account

        from src.clients.base_evm import EvmClient
        key = "0x" + "11" * 32
        raw = Web3.to_hex(Account.from_key(key).sign_transaction({
            "chainId": FROM_INFO.chain_id, "nonce": 40, "to": DIAMOND, "value": 0,
            "data": "0xdead", "gas": 100_000, "maxFeePerGas": 2_100, "maxPriorityFeePerGas": 100,
        }).raw_transaction)
        pipeline = _make_pipeline(settings)
        swap_id, quote = self._seed(
            pipeline, insert_quote, "qr8", step="lifi_execute",
            lifi_tx_hash=LIFI_TX, lifi_tx_raw=raw)
        restarted = EvmClient("http://localhost:1", key)
        restarted.w3 = MagicMock()
        restarted.w3.eth.get_block.return_value = {"baseFeePerGas": 1_000}
        pipeline.evm.rebroadcast = restarted.rebroadcast
        mined = iter([None, _swap_receipt(60000)])
        pipeline.evm.get_receipt = MagicMock(
            side_effect=lambda tx_hash: next(mined) if tx_hash == LIFI_TX else MINED)

        await pipeline._run(swap_id, quote, 5)

        assert _swap_row(swap_id)["status"] == "completed"
        restarted.w3.eth.send_raw_transaction.assert_called_once_with(raw)
        pipeline.evm.send_transaction_request.assert_not_called()

    async def test_an_unsent_lifi_tx_is_sent(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        swap_id, quote = self._seed(pipeline, insert_quote, "qr2", step="lifi_execute")

        await pipeline._run(swap_id, quote, 5)

        assert _swap_row(swap_id)["status"] == "completed"
        pipeline.evm.send_transaction_request.assert_called_once()
        pipeline.bridge.withdraw_to_chain.assert_not_awaited()

    async def test_a_sent_deposit_is_awaited_not_sent_again(self, settings, insert_quote):
        pipeline = _make_pipeline(settings)
        pipeline.accounting.get_token_info = AsyncMock(return_value=TO_INFO)
        swap_id, quote = self._seed(
            pipeline, insert_quote, "qr3", step="deposit", lifi_tx_hash=LIFI_TX,
            to_amount_received="60000", deposit_tx_hash=DEPOSIT_TX,
            deposit_tx_raw=_raw(DEPOSIT_TX))

        await pipeline._run(swap_id, quote, 5)

        assert _swap_row(swap_id)["status"] == "completed"
        pipeline.evm.transfer.assert_not_called()
        pipeline.evm.send_transaction_request.assert_not_called()
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
        pipeline.evm.transfer.assert_called_once()
        assert pipeline.evm.transfer.call_args.args[2] == 60000

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
            to_amount_received="60000", deposit_tx_hash=DEPOSIT_TX,
            deposit_tx_raw=_raw(DEPOSIT_TX))
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

    async def test_recovery_acts_on_each_row_as_it_is_at_its_turn(self, settings, insert_quote):
        from src.services.swap.lifi_pipeline import recover_inflight_lifi_swaps
        pipeline = _make_pipeline(settings)
        refunded, _ = self._seed(pipeline, insert_quote, "qr14", step="withdraw")
        settled, _ = self._seed(pipeline, insert_quote, "qr15", step="lifi_execute")

        # A runner from the failed pass settles its swap while this pass refunds.
        async def refund(*_):
            pipeline._update_swap(settled, status="completed")
        pipeline._refund = AsyncMock(side_effect=refund)
        pipeline.spawn_background = MagicMock()

        await recover_inflight_lifi_swaps(pipeline=pipeline)

        assert [c.args[0] for c in pipeline._refund.await_args_list] == [refunded]
        pipeline.spawn_background.assert_not_called()
