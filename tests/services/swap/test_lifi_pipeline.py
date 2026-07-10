import time
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import load_settings
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
    accounting.get_transfer_nonce = AsyncMock(side_effect=[6, 70])
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
    async def test_rejected_input_returns_failed_record(self, test_db, settings, insert_quote):
        insert_quote("q_rej", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        privana = await pipeline._privana_factory()
        privana.transfer_funds = AsyncMock(return_value=MagicMock(status="rejected", detail="bad sig"))
        record = await pipeline.launch(_quote("q_rej"), USER, 5, "0x" + "ab" * 65)
        assert record.status == "failed"
        assert record.venue == "lifi"

    async def test_accepted_input_returns_executing_record(self, test_db, settings, insert_quote):
        insert_quote("q_ok", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        pipeline = _make_pipeline(settings)
        pipeline.spawn_background = MagicMock()
        record = await pipeline.launch(_quote("q_ok"), USER, 5, "0x" + "ab" * 65)
        assert record.status == "executing"
        assert record.step == "input_transfer"
        pipeline.spawn_background.assert_called_once()


class TestRun:
    async def _launch_and_run(self, pipeline, quote):
        pipeline.spawn_background = MagicMock()
        record = await pipeline.launch(quote, USER, 5, "0x" + "ab" * 65)
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
        pipeline.accounting.get_transfer_nonce = AsyncMock(side_effect=[6, 70, 70])
        swap_id = await self._launch_and_run(pipeline, _quote("q4"))
        row = dict(test_db.execute("SELECT * FROM swaps WHERE id=?", (swap_id,)).fetchone())
        assert row["status"] == "completed"
        assert privana.transfer_funds.await_count == 3


class TestExecutorDispatch:
    async def test_lifi_venue_routes_to_pipeline(self, test_db, settings, insert_quote):
        from unittest.mock import patch
        insert_quote("q_disp", venue="lifi", user_address=USER,
                     from_token_id=FROM_TOKEN, to_token_id=TO_TOKEN)
        fake_record = MagicMock()
        fake_pipeline = MagicMock()
        fake_pipeline.launch = AsyncMock(return_value=fake_record)
        with patch("src.services.swap.executor.get_accounting_client"), \
             patch("src.services.swap.executor.get_sapphire_client"), \
             patch("src.services.swap.executor.load_settings", return_value=settings), \
             patch("src.services.swap.executor.get_lifi_pipeline", return_value=fake_pipeline):
            from src.services.swap.executor import SwapExecutor
            executor = SwapExecutor()
            result = await executor.execute_swap("q_disp", USER, 5, "0x" + "ab" * 65)
        assert result is fake_record
        fake_pipeline.launch.assert_awaited_once()
