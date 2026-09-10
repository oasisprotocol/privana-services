import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_account import Account

from src.core.db import db_write
from src.core.eip712 import sign_transfer
from src.models.swap import SwapStatus

USER_SK = "0x" + "11" * 32
USER_ADDRESS = Account.from_key(USER_SK).address


def _make_executor(settings):
    with patch("src.services.swap.executor.load_settings", return_value=settings):
        from src.services.swap.executor import SwapExecutor
        return SwapExecutor()


def _quote_fields(**overrides):
    fields = dict(
        user_address=USER_ADDRESS.lower(),
        from_token_id="0xaaaa",
        to_token_id="0xbbbb",
        from_amount="1000000",
        to_amount_gross="45000000000000000",
        to_amount_estimate="44000000000000000",
        to_amount_min="43000000000000000",
        route_tool="okx",
    )
    fields.update(overrides)
    return fields


def _sign_input(settings):
    return sign_transfer(
        private_key=USER_SK,
        chain_id=settings.accounting_chain_id,
        verifying_contract=settings.accounting_contract_address,
        to_address=settings.liquidity_provider_address,
        token_id="0xaaaa",
        amount=1000000,
        nonce=0,
    )


def _insert_swap_row(test_db, swap_id, status, venue="internal"):
    now = int(time.time())
    db_write(
        test_db,
        """INSERT INTO swaps
           (id, quote_id, user_address, from_token_id, to_token_id, from_amount,
            to_amount_estimate, input_nonce, input_signature, status, venue,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (swap_id, "q_row", USER_ADDRESS.lower(), "0xaaaa", "0xbbbb", "1000000",
         "44000000000000000", 0, "0x" + "ab" * 65, status, venue, now, now),
    )


def _stub_pipelines():
    """Patch both venue pipelines, returning (internal, lifi) mocks."""
    internal, lifi = MagicMock(), MagicMock()
    internal.execute_swap = AsyncMock()
    lifi.execute_swap = AsyncMock()
    return internal, lifi, patch.multiple(
        "src.services.swap.executor",
        get_internal_pipeline=MagicMock(return_value=internal),
        get_lifi_pipeline=MagicMock(return_value=lifi),
    )


class TestValidateQuote:
    def test_expired_quote_raises(self, test_db, settings, insert_quote):
        insert_quote("expired_q", expires_at=int(time.time()) - 10, user_address="0xuser")
        executor = _make_executor(settings)
        with pytest.raises(ValueError, match="Quote has expired"):
            executor._validate_quote("expired_q")

    def test_quote_at_exact_expiry_raises(self, test_db, settings, insert_quote):
        insert_quote("q_boundary", expires_at=int(time.time()))
        executor = _make_executor(settings)
        with pytest.raises(ValueError, match="Quote has expired"):
            executor._validate_quote("q_boundary")

    def test_missing_quote_raises(self, test_db, settings):
        executor = _make_executor(settings)
        with pytest.raises(ValueError, match="Quote not found"):
            executor._validate_quote("nonexistent")

    def test_valid_quote_returns_dict(self, test_db, settings, insert_quote):
        insert_quote("q_valid", **_quote_fields())
        executor = _make_executor(settings)
        result = executor._validate_quote("q_valid")
        assert isinstance(result, dict)
        assert result["id"] == "q_valid"


class TestScheduleSwap:
    @pytest.fixture
    def input_signature(self, settings):
        return _sign_input(settings)

    async def test_signer_mismatch_rejected(self, test_db, settings, insert_quote, input_signature):
        insert_quote("q_stranger", **_quote_fields(user_address="0x" + "9" * 40))
        executor = _make_executor(settings)
        with pytest.raises(ValueError, match="Quote was not created for this user"):
            await executor.schedule_swap("q_stranger", 0, input_signature)

    async def test_malformed_signature_rejected(self, test_db, settings, insert_quote):
        insert_quote("q_badsig", **_quote_fields())
        executor = _make_executor(settings)
        with pytest.raises(ValueError):
            await executor.schedule_swap("q_badsig", 0, "0xdeadbeef")

    async def test_queues_the_swap_without_executing_it(
        self, test_db, settings, insert_quote, input_signature
    ):
        insert_quote("q_sched", **_quote_fields())
        executor = _make_executor(settings)
        internal, lifi, pipelines = _stub_pipelines()

        with pipelines:
            result = await executor.schedule_swap("q_sched", 0, input_signature)

        assert result.status == SwapStatus.SCHEDULED.value
        assert result.venue == "internal"
        assert result.swap_tx_hash is None
        assert result.input_nonce == 0
        assert result.input_signature == input_signature
        internal.execute_swap.assert_not_awaited()
        lifi.execute_swap.assert_not_awaited()

    async def test_lifi_quote_is_queued_on_the_lifi_venue(
        self, test_db, settings, insert_quote, input_signature
    ):
        insert_quote("q_lifi", **_quote_fields(venue="lifi"))
        executor = _make_executor(settings)

        result = await executor.schedule_swap("q_lifi", 0, input_signature)

        assert result.status == SwapStatus.SCHEDULED.value
        assert result.venue == "lifi"


class TestProcessPendingSwaps:
    async def test_scheduled_internal_row_goes_to_the_internal_pipeline(
        self, test_db, settings
    ):
        _insert_swap_row(test_db, "s_int", SwapStatus.SCHEDULED.value)
        executor = _make_executor(settings)
        internal, lifi, pipelines = _stub_pipelines()

        with pipelines:
            await executor._process_pending_swaps()

        assert internal.execute_swap.await_args.args[0].id == "s_int"
        lifi.execute_swap.assert_not_awaited()

    async def test_executing_internal_row_is_retried(self, test_db, settings):
        _insert_swap_row(test_db, "s_int_run", SwapStatus.EXECUTING.value)
        executor = _make_executor(settings)
        internal, _, pipelines = _stub_pipelines()

        with pipelines:
            await executor._process_pending_swaps()

        assert internal.execute_swap.await_args.args[0].id == "s_int_run"

    async def test_scheduled_lifi_row_goes_to_the_lifi_pipeline(self, test_db, settings):
        _insert_swap_row(test_db, "s_lifi", SwapStatus.SCHEDULED.value, venue="lifi")
        executor = _make_executor(settings)
        internal, lifi, pipelines = _stub_pipelines()

        with pipelines:
            await executor._process_pending_swaps()

        assert lifi.execute_swap.await_args.args[0].id == "s_lifi"
        internal.execute_swap.assert_not_awaited()

    async def test_executing_lifi_row_left_to_its_background_task(self, test_db, settings):
        _insert_swap_row(test_db, "s_lifi_run", SwapStatus.EXECUTING.value, venue="lifi")
        executor = _make_executor(settings)
        _, lifi, pipelines = _stub_pipelines()

        with pipelines:
            await executor._process_pending_swaps()

        lifi.execute_swap.assert_not_awaited()

    async def test_settled_rows_are_left_alone(self, test_db, settings):
        _insert_swap_row(test_db, "s_done", SwapStatus.COMPLETED.value)
        _insert_swap_row(test_db, "s_failed", SwapStatus.FAILED.value)
        executor = _make_executor(settings)
        internal, _, pipelines = _stub_pipelines()

        with pipelines:
            await executor._process_pending_swaps()

        internal.execute_swap.assert_not_awaited()

    async def test_executes_queued_swaps_oldest_first(self, test_db, settings):
        now = int(time.time())
        _insert_swap_row(test_db, "s_new", SwapStatus.SCHEDULED.value)
        _insert_swap_row(test_db, "s_old", SwapStatus.SCHEDULED.value)
        db_write(test_db, "UPDATE swaps SET created_at = ? WHERE id = ?", (now - 60, "s_old"))
        executor = _make_executor(settings)
        internal, _, pipelines = _stub_pipelines()

        with pipelines:
            await executor._process_pending_swaps()

        assert [c.args[0].id for c in internal.execute_swap.await_args_list] == ["s_old", "s_new"]


class TestWorker:
    async def test_start_drains_the_queue_and_stop_cancels(self, test_db, settings):
        _insert_swap_row(test_db, "s_worker", SwapStatus.SCHEDULED.value)
        executor = _make_executor(settings)
        internal, _, pipelines = _stub_pipelines()

        with pipelines:
            await executor.start()
            try:
                for _ in range(200):
                    if internal.execute_swap.await_count:
                        break
                    await asyncio.sleep(0.01)
            finally:
                await executor.stop()

        assert internal.execute_swap.await_args.args[0].id == "s_worker"
        assert executor._worker_task is None

    async def test_worker_survives_a_failing_pass(self, test_db, settings):
        _insert_swap_row(test_db, "s_boom", SwapStatus.SCHEDULED.value)
        executor = _make_executor(settings)
        internal, _, pipelines = _stub_pipelines()
        internal.execute_swap = AsyncMock(side_effect=RuntimeError("pipeline exploded"))

        with pipelines:
            await executor.start()
            try:
                for _ in range(200):
                    if internal.execute_swap.await_count:
                        break
                    await asyncio.sleep(0.01)
                assert not executor._worker_task.done()
            finally:
                await executor.stop()
