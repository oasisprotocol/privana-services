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
    service._update_transaction = MagicMock()

    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        await EarnWorker().run_once()

    service._update_transaction.assert_called_once()
    assert service._update_transaction.call_args.args[0] == "t1"
    assert service._update_transaction.call_args.kwargs["status"] == "failed"


def test_start_fails_rows_left_executing_by_a_restart(test_db):
    # Replaying one could pay twice: its accounting transfer may already be on
    # chain. Surface it instead.
    _row(test_db, "t1", status="executing")

    EarnWorker._fail_orphans()

    assert _status(test_db, "t1") == "failed"
