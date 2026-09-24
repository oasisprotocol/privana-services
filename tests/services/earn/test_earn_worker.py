import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.db import db_write
from src.services.earn.worker import EarnWorker

POOL = "0x" + "aa" * 32
USER_A = "0x" + "11" * 20
USER_B = "0x" + "22" * 20


def _row(db, tx_id, *, operation="deposit", user=USER_A, status="scheduled", amount="1000", created=None):
    now = created if created is not None else int(time.time())
    db_write(
        db,
        """INSERT INTO earn_transactions
           (id, operation, pool_id, user_address, token_id, amount, signer_address,
            nonce, signature, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (tx_id, operation, POOL, user.lower(), "", amount, user.lower(),
         0, "0x" + "cc" * 65, status, now, now),
    )


def _status(db, tx_id):
    return db.execute("SELECT status FROM earn_transactions WHERE id = ?", (tx_id,)).fetchone()["status"]


def test_claim_takes_a_scheduled_row_and_marks_it_executing(test_db):
    _row(test_db, "t1")

    claimed = EarnWorker._claim(5)

    assert [c["id"] for c in claimed] == ["t1"]
    assert _status(test_db, "t1") == "executing"


def test_claim_ignores_rows_that_are_not_scheduled(test_db):
    _row(test_db, "t1", status="pending")
    _row(test_db, "t2", status="completed")

    assert EarnWorker._claim(5) == []


def test_claim_takes_only_one_operation_per_user(test_db):
    # Both legs spend the same accounting transfer nonce, so a second request
    # from one user has to wait for the first to consume it.
    _row(test_db, "t1", user=USER_A, created=100)
    _row(test_db, "t2", user=USER_A, created=200)
    _row(test_db, "t3", user=USER_B, created=300)

    claimed = [c["id"] for c in EarnWorker._claim(5)]

    assert claimed == ["t1", "t3"]
    assert _status(test_db, "t2") == "scheduled"


def test_claim_is_oldest_first(test_db):
    _row(test_db, "new", user=USER_A, created=900)
    _row(test_db, "old", user=USER_B, created=100)

    assert [c["id"] for c in EarnWorker._claim(5)] == ["old", "new"]


@pytest.mark.asyncio
async def test_run_once_executes_a_deposit_against_its_own_row(test_db):
    _row(test_db, "t1", operation="deposit")
    service = MagicMock()
    service.deposit = AsyncMock(return_value={})

    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        await EarnWorker().run_once()

    service.deposit.assert_awaited_once()
    assert service.deposit.await_args.kwargs["scheduled_id"] == "t1"
    service.withdraw.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_routes_a_withdraw_to_withdraw(test_db):
    _row(test_db, "t1", operation="withdraw")
    service = MagicMock()
    service.withdraw = AsyncMock(return_value={})

    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        await EarnWorker().run_once()

    service.withdraw.assert_awaited_once()
    service.deposit.assert_not_called()


@pytest.mark.asyncio
async def test_a_raising_operation_settles_its_row_as_failed(test_db):
    _row(test_db, "t1")
    service = MagicMock()
    service.deposit = AsyncMock(side_effect=ValueError("Pool is not active"))

    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        await EarnWorker().run_once()

    row = test_db.execute(
        "SELECT status, error FROM earn_transactions WHERE id = ?", ("t1",)
    ).fetchone()
    assert row["status"] == "failed"
    assert "Pool is not active" in row["error"]


@pytest.mark.asyncio
async def test_an_error_after_settlement_does_not_overwrite_the_outcome(test_db):
    """deposit/withdraw settle their own row. If something throws after that —
    a read taken once the transaction already landed — the worker must not
    report a deposit that succeeded as failed."""
    _row(test_db, "t1")

    async def settle_then_raise(**kwargs):
        db_write(
            test_db,
            "UPDATE earn_transactions SET status = 'completed' WHERE id = ?", ("t1",),
        )
        raise RuntimeError("post-settlement read failed")

    service = MagicMock()
    service.deposit = AsyncMock(side_effect=settle_then_raise)

    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        await EarnWorker().run_once()

    assert _status(test_db, "t1") == "completed"


@pytest.mark.asyncio
async def test_a_row_that_never_reached_the_contract_goes_back_on_the_queue(test_db):
    _row(test_db, "t1", status="executing")
    service = MagicMock()

    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        assert await EarnWorker()._recover() is True

    service._update_transaction.assert_called_once_with("t1", status="scheduled")


@pytest.mark.asyncio
async def test_a_row_with_a_hash_is_settled_from_its_receipt(test_db):
    _row(test_db, "t1", status="executing")
    db_write(test_db, "UPDATE earn_transactions SET tx_hash = ? WHERE id = ?", ("0xabc", "t1"))
    service = MagicMock()
    service.sapphire.wait_for_receipt = MagicMock(return_value={"status": 1, "blockNumber": 42})
    service._record_share_delta = AsyncMock()

    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        await EarnWorker()._recover()

    service._record_share_delta.assert_awaited_once_with("t1", bytes.fromhex("aa" * 32), 42, "deposit", "1000")
    # Routing never ran, so the idle deployer completes it.
    assert service._update_transaction.call_args.kwargs["status"] == "undeployed"


@pytest.mark.asyncio
async def test_a_recovered_withdraw_is_completed(test_db):
    _row(test_db, "t1", operation="withdraw", status="pending")
    db_write(test_db, "UPDATE earn_transactions SET tx_hash = ? WHERE id = ?", ("0xabc", "t1"))
    service = MagicMock()
    service.sapphire.wait_for_receipt = MagicMock(return_value={"status": 1, "blockNumber": 42})
    service._record_share_delta = AsyncMock()

    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        await EarnWorker()._recover()

    assert service._update_transaction.call_args.kwargs["status"] == "completed"


@pytest.mark.asyncio
async def test_an_unknown_receipt_leaves_the_row_pending(test_db):
    _row(test_db, "t1", status="pending")
    db_write(test_db, "UPDATE earn_transactions SET tx_hash = ? WHERE id = ?", ("0xabc", "t1"))
    service = MagicMock()
    service.sapphire.wait_for_receipt = MagicMock(side_effect=TimeoutError("timed out"))

    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        assert await EarnWorker()._recover() is True

    assert "status" not in service._update_transaction.call_args.kwargs


@pytest.mark.asyncio
async def test_the_share_ledger_repair_keeps_running_between_operations(test_db):
    worker = EarnWorker()
    service = MagicMock()
    service.repair_share_ledger = AsyncMock(side_effect=[RuntimeError("rpc down"), None, None])
    iterations = []

    async def run_once():
        iterations.append(1)
        if len(iterations) == 3:
            worker._stop.set()

    worker.run_once = run_once
    with patch("src.services.earn.worker.get_vault_service", return_value=service), \
         patch("src.services.earn.worker.REPAIR_INTERVAL_SEC", 0), \
         patch("src.services.earn.worker.POLL_INTERVAL", 0):
        await worker._loop()

    assert service.repair_share_ledger.await_count == 3


@pytest.mark.asyncio
async def test_a_withdraw_short_of_liquidity_waits_instead_of_failing(test_db):
    from src.services.earn.strategies.base import LiquidityUnavailable

    _row(test_db, "t1", operation="withdraw")
    service = MagicMock()
    service.withdraw = AsyncMock(side_effect=LiquidityUnavailable("short"))

    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        await EarnWorker().run_once()

    row = test_db.execute(
        "SELECT status, error, history FROM earn_transactions WHERE id = ?", ("t1",)
    ).fetchone()
    assert row["status"] == "awaiting_liquidity"
    assert row["error"] is None
    assert '"awaiting_liquidity"' in row["history"]


@pytest.mark.asyncio
async def test_a_waiting_withdraw_goes_back_on_the_queue_once_liquidity_is_back(test_db):
    _row(test_db, "t1", operation="withdraw", status="awaiting_liquidity")
    strategy = MagicMock()
    strategy.withdraw_ready = AsyncMock(return_value=True)
    service = MagicMock()
    service._registry.get.return_value = strategy
    service.withdraw = AsyncMock(return_value={})

    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        await EarnWorker().run_once()

    strategy.withdraw_ready.assert_awaited_once_with(1000)
    # Released and claimed in the same pass, with its original signature.
    service.withdraw.assert_awaited_once()
    assert service.withdraw.await_args.kwargs["scheduled_id"] == "t1"


@pytest.mark.asyncio
async def test_a_waiting_withdraw_stays_put_while_liquidity_is_short(test_db):
    _row(test_db, "t1", operation="withdraw", status="awaiting_liquidity")
    strategy = MagicMock()
    strategy.withdraw_ready = AsyncMock(return_value=False)
    service = MagicMock()
    service._registry.get.return_value = strategy
    service.withdraw = AsyncMock()

    # A freshly booted host: the monotonic clock is still below the interval.
    with patch("src.services.earn.worker.get_vault_service", return_value=service), patch(
        "src.services.earn.worker.time.monotonic", return_value=5.0
    ):
        worker = EarnWorker()
        await worker.run_once()
        await worker.run_once()

    assert _status(test_db, "t1") == "awaiting_liquidity"
    service.withdraw.assert_not_called()
    # Rechecked on its own interval, not on every pass.
    strategy.withdraw_ready.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_waiting_withdraw_does_not_block_other_users(test_db):
    _row(test_db, "t1", operation="withdraw", status="awaiting_liquidity", created=100)
    _row(test_db, "t2", user=USER_B, created=200)
    strategy = MagicMock()
    strategy.withdraw_ready = AsyncMock(return_value=False)
    service = MagicMock()
    service._registry.get.return_value = strategy
    service.deposit = AsyncMock(return_value={})

    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        await EarnWorker().run_once()

    assert service.deposit.await_args.kwargs["scheduled_id"] == "t2"
