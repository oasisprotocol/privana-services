import asyncio
import time
from unittest.mock import AsyncMock

from src.core.db import db_write
from src.models.swap import SwapStatus
from src.services.swap.internal import INTERNAL_CLAIM_PARAMS, INTERNAL_CLAIM_SQL
from src.services.swap.lifi import LIFI_CLAIM_PARAMS, LIFI_CLAIM_SQL
from src.services.swap.worker import SwapWorker

USER_ADDRESS = "0x" + "11" * 20


def _insert_swap_row(test_db, swap_id, status, venue="internal"):
    now = int(time.time())
    db_write(
        test_db,
        """INSERT INTO swaps
           (id, quote_id, user_address, from_token_id, to_token_id, from_amount,
            to_amount_estimate, input_nonce, input_signature, status, venue,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            swap_id,
            f"q_{swap_id}",
            USER_ADDRESS,
            "0xaaaa",
            "0xbbbb",
            "1000000",
            "44000000000000000",
            0,
            "0x" + "ab" * 65,
            status,
            venue,
            now,
            now,
        ),
    )


def _internal_worker(execute):
    return SwapWorker(
        name="internal",
        claim_sql=INTERNAL_CLAIM_SQL,
        claim_params=INTERNAL_CLAIM_PARAMS,
        execute=execute,
    )


def _lifi_worker(execute):
    return SwapWorker(
        name="lifi",
        claim_sql=LIFI_CLAIM_SQL,
        claim_params=LIFI_CLAIM_PARAMS,
        execute=execute,
    )


class TestInternalWorker:
    async def test_scheduled_row_goes_to_the_pipeline(self, test_db):
        _insert_swap_row(test_db, "s_int", SwapStatus.SCHEDULED.value)
        execute = AsyncMock()

        await _internal_worker(execute)._process()

        assert execute.await_args.args[0].id == "s_int"

    async def test_executing_row_is_retried(self, test_db):
        _insert_swap_row(test_db, "s_int_run", SwapStatus.EXECUTING.value)
        execute = AsyncMock()

        await _internal_worker(execute)._process()

        assert execute.await_args.args[0].id == "s_int_run"

    async def test_lifi_rows_are_not_claimed(self, test_db):
        _insert_swap_row(test_db, "s_lifi", SwapStatus.SCHEDULED.value, venue="lifi")
        execute = AsyncMock()

        await _internal_worker(execute)._process()

        execute.assert_not_awaited()

    async def test_settled_rows_are_left_alone(self, test_db):
        _insert_swap_row(test_db, "s_done", SwapStatus.COMPLETED.value)
        _insert_swap_row(test_db, "s_failed", SwapStatus.FAILED.value)
        execute = AsyncMock()

        await _internal_worker(execute)._process()

        execute.assert_not_awaited()

    async def test_executes_queued_swaps_oldest_first(self, test_db):
        now = int(time.time())
        _insert_swap_row(test_db, "s_new", SwapStatus.SCHEDULED.value)
        _insert_swap_row(test_db, "s_old", SwapStatus.SCHEDULED.value)
        db_write(test_db, "UPDATE swaps SET created_at = ? WHERE id = ?", (now - 60, "s_old"))
        execute = AsyncMock()

        await _internal_worker(execute)._process()

        assert [c.args[0].id for c in execute.await_args_list] == ["s_old", "s_new"]


class TestLifiWorker:
    async def test_scheduled_row_goes_to_the_pipeline(self, test_db):
        _insert_swap_row(test_db, "s_lifi", SwapStatus.SCHEDULED.value, venue="lifi")
        execute = AsyncMock()

        await _lifi_worker(execute)._process()

        assert execute.await_args.args[0].id == "s_lifi"

    async def test_executing_row_left_to_its_background_task(self, test_db):
        _insert_swap_row(test_db, "s_lifi_run", SwapStatus.EXECUTING.value, venue="lifi")
        execute = AsyncMock()

        await _lifi_worker(execute)._process()

        execute.assert_not_awaited()

    async def test_internal_rows_are_not_claimed(self, test_db):
        _insert_swap_row(test_db, "s_int", SwapStatus.SCHEDULED.value)
        execute = AsyncMock()

        await _lifi_worker(execute)._process()

        execute.assert_not_awaited()


class TestWorkerLoop:
    async def test_a_row_that_raises_does_not_abort_the_pass(self, test_db):
        _insert_swap_row(test_db, "s_boom", SwapStatus.SCHEDULED.value)
        _insert_swap_row(test_db, "s_ok", SwapStatus.SCHEDULED.value)
        calls = []

        async def execute(swap):
            calls.append(swap.id)
            if swap.id == "s_boom":
                raise RuntimeError("pipeline exploded")

        await _internal_worker(execute)._process()

        assert calls == ["s_boom", "s_ok"]

    async def test_start_drains_the_queue_and_stop_cancels(self, test_db):
        _insert_swap_row(test_db, "s_worker", SwapStatus.SCHEDULED.value)
        execute = AsyncMock()
        worker = _internal_worker(execute)

        worker.start()
        try:
            for _ in range(200):
                if execute.await_count:
                    break
                await asyncio.sleep(0.01)
        finally:
            await worker.stop()

        assert execute.await_args.args[0].id == "s_worker"
        assert worker._task is None

    async def test_worker_survives_a_failing_pass(self, test_db):
        _insert_swap_row(test_db, "s_boom", SwapStatus.SCHEDULED.value)
        execute = AsyncMock(side_effect=RuntimeError("pipeline exploded"))
        worker = _internal_worker(execute)

        worker.start()
        try:
            for _ in range(200):
                if execute.await_count:
                    break
                await asyncio.sleep(0.01)
        finally:
            await worker.stop()

        assert worker._task is None

    async def test_stop_without_start_is_a_noop(self, test_db):
        worker = _internal_worker(AsyncMock())
        await worker.stop()


class TestExecuteBatch:
    async def test_batch_callback_receives_every_claimed_row_at_once(self, test_db):
        _insert_swap_row(test_db, "s_a", SwapStatus.SCHEDULED.value)
        _insert_swap_row(test_db, "s_b", SwapStatus.SCHEDULED.value)
        execute_batch = AsyncMock()
        worker = SwapWorker(
            name="internal",
            claim_sql=INTERNAL_CLAIM_SQL,
            claim_params=INTERNAL_CLAIM_PARAMS,
            execute_batch=execute_batch,
        )

        await worker._process()

        execute_batch.assert_awaited_once()
        claimed_ids = [s.id for s in execute_batch.await_args.args[0]]
        assert claimed_ids == ["s_a", "s_b"]

    async def test_batch_callback_failure_does_not_abort_the_pass(self, test_db):
        _insert_swap_row(test_db, "s_a", SwapStatus.SCHEDULED.value)
        execute_batch = AsyncMock(side_effect=RuntimeError("batch exploded"))
        worker = SwapWorker(
            name="internal",
            claim_sql=INTERNAL_CLAIM_SQL,
            claim_params=INTERNAL_CLAIM_PARAMS,
            execute_batch=execute_batch,
        )

        await worker._process()

        execute_batch.assert_awaited_once()
