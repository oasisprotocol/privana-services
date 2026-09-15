import time
import uuid
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.db import db_write, get_db
from src.core.fees import calculate_fee
from src.models.common import TokenInfo

USER = "0x" + "a" * 40
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
        "to": "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE",
        "data": "0xdead", "value": "0x0",
        "gasLimit": "0x15fcbf", "gasPrice": "0x3b9aca00",
    },
    "estimate": {
        "approvalAddress": "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE",
        "toAmount": "58000", "toAmountMin": "56000",
    },
}


def _make_pipeline(settings):
    from src.services.swap.lifi_pipeline import LifiSwapPipeline

    accounting = MagicMock()
    accounting.get_transfer_nonce = AsyncMock(side_effect=[6, 70, 71])
    accounting.get_token_info = AsyncMock(side_effect=[FROM_INFO, TO_INFO])

    lifi = MagicMock()
    lifi.get_execution_quote = AsyncMock(return_value=EXEC_QUOTE)
    lifi.get_status = AsyncMock(return_value={"status": "DONE"})

    bridge = MagicMock()
    bridge.withdraw_to_chain = AsyncMock(return_value=17)
    bridge.get_deposit_address = AsyncMock(return_value="0x" + "dd" * 20)
    bridge.lp_internal_balance = AsyncMock(return_value=100)
    bridge.await_deposit_credit = AsyncMock(return_value=None)

    evm = MagicMock()
    evm.address = "0x152E6a7125665764a4F1F1df80E8f5D49Bf0239c"
    evm.erc20_balance = MagicMock(side_effect=[0, 60000])
    evm.ensure_allowance = MagicMock(return_value=None)
    evm.send_transaction_request = MagicMock(return_value="0x" + "cd" * 32)
    evm.transfer_erc20 = MagicMock(return_value="0x" + "ef" * 32)

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
        low_quote = {**EXEC_QUOTE, "estimate": {**EXEC_QUOTE["estimate"], "toAmountMin": "50000"}}
        pipeline.lifi.get_execution_quote = AsyncMock(return_value=low_quote)
        swap_id = await self._launch_and_run(pipeline, _quote("q2"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        assert row["status"] == "failed"
        pipeline.evm.send_transaction_request.assert_not_called()

    async def test_lifi_status_failed_fails_swap(self, test_db, settings, insert_quote):
        insert_quote("q3", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline.lifi.get_status = AsyncMock(return_value={"status": "FAILED"})
        swap_id = await self._launch_and_run(pipeline, _quote("q3"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        assert row["status"] == "failed"

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

    async def test_lifi_execute_failure_redeposits_then_refunds(self, test_db, settings, insert_quote):
        insert_quote("q_e", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline.evm.send_transaction_request = MagicMock(side_effect=RuntimeError("revert"))
        pipeline.evm.erc20_balance = MagicMock(side_effect=[0, 1000000])
        pipeline.accounting.get_transfer_nonce = AsyncMock(side_effect=[6, 70, 71])
        pipeline.accounting.get_token_info = AsyncMock(
            side_effect=[FROM_INFO, TO_INFO, FROM_INFO])
        swap_id = await self._launch_and_run(pipeline, _quote("q_e"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        assert row["status"] == "refunded"
        pipeline.evm.transfer_erc20.assert_called_once()
        pipeline.bridge.await_deposit_credit.assert_awaited_once()

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


class TestRecovery:
    def _insert_swap(self, test_db, swap_id, step, status="executing"):
        import time as _t

        from src.core.db import db_write
        now = int(_t.time())
        db_write(
            test_db,
            """INSERT INTO swaps
               (id, quote_id, user_address, from_token_id, to_token_id,
                from_amount, to_amount_estimate, status, venue, step, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (swap_id, "q_rec", USER, FROM_TOKEN, TO_TOKEN,
             "1000000", "57000", status, "lifi", step, now, now),
        )

    async def test_inflight_swaps_routed_to_refund_or_failed(self, test_db, settings):
        from src.services.swap.lifi_pipeline import recover_inflight_lifi_swaps
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



class TestQuoteFeeBps:
    def test_uses_stored_fee(self, settings):
        pipeline = _make_pipeline(settings)
        assert pipeline._quote_fee_bps({"fee_bps": 25}) == 25
        assert pipeline._quote_fee_bps({"fee_bps": 0}) == 0

    def test_falls_back_to_global_fee(self, settings):
        pipeline = _make_pipeline(settings)
        assert pipeline._quote_fee_bps({}) == pipeline.settings.fee_bps
        assert pipeline._quote_fee_bps({"fee_bps": None}) == pipeline.settings.fee_bps

    async def test_credit_deducts_stored_quote_fee(self, settings):
        pipeline = _make_pipeline(settings)
        pipeline._lp_transfer = AsyncMock()
        quote = {"user_address": "0x" + "ab" * 20, "to_token_id": "0xbbbb", "fee_bps": 100}
        credited = await pipeline._credit(quote, 10_000)
        assert credited == 9_900
        pipeline._lp_transfer.assert_awaited_once_with("0x" + "ab" * 20, "0xbbbb", 9_900)


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

    async def test_unresolved_internal_swap_blocks_lp_credit(self, settings, test_db):
        import pytest

        pipeline = _make_pipeline(settings)
        swap_id = _seed_swap(test_db, _quote())
        pipeline._update_swap(
            swap_id, venue="internal", status="executing", output_signature="0xab"
        )
        pipeline._credit_max_retries = 2
        privana = await pipeline._privana_factory()
        with pytest.raises(RuntimeError, match="internal swap settlement is unresolved"):
            await pipeline._lp_transfer(USER, TO_TOKEN, 100)
        privana.transfer_funds.assert_not_awaited()
