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


_EARN_MANAGER = "0x1111111111111111111111111111111111111111"


def _settlement_service(*, operation="deposit", assets_moved=None):
    from src.services.earn.vault_service import VaultService

    service = MagicMock()
    service._update_transaction.side_effect = lambda tx_id, **fields: (
        VaultService._update_transaction(service, tx_id, **fields)
    )
    sign = 1 if operation == "deposit" else -1
    assets_moved = sign * 1000 if assets_moved is None else assets_moved
    service.contract_address = _EARN_MANAGER
    service.sapphire.w3.eth.get_transaction_receipt.return_value = {
        "status": 1, "blockNumber": 42, "to": _EARN_MANAGER,
    }
    service.sapphire.w3.eth.get_block.return_value = {"timestamp": 1234}
    service.sapphire.w3_unwrapped = service.sapphire.w3

    def pool(block_identifier):
        if block_identifier == 41:
            return (b"", "", 1000, 10000, True)
        assert block_identifier == 42
        return (b"", "", 1000 + sign * 100, 10000 + assets_moved, True)

    service._history.functions.pools.return_value.call.side_effect = pool
    return service


def _hashed_row(db, tx_id="t1", *, status="pending", operation="deposit"):
    _row(db, tx_id, status=status, operation=operation, created=100)
    db_write(db, "UPDATE earn_transactions SET tx_hash = ? WHERE id = ?", ("0x" + tx_id, tx_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,status,expected", [
    ("deposit", "pending", "undeployed"), ("deposit", "failed", "undeployed"),
    ("deposit", "completed", "completed"), ("withdraw", "pending", "completed"),
])
async def test_one_path_records_outcome_shares_and_time_once(test_db, operation, status, expected):
    _hashed_row(test_db, status=status, operation=operation)
    service = _settlement_service(operation=operation)
    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        assert await EarnWorker()._recover() is False
        service.sapphire.w3.eth.get_transaction_receipt.reset_mock()
        assert await EarnWorker()._recover() is False
    result = test_db.execute("SELECT * FROM earn_transactions").fetchone()
    assert result["status"] == expected
    assert result["shares_delta"] == ("100" if operation == "deposit" else "-100")
    assert result["exchange_rate"] == "10"
    assert result["settled_at"] == 1234
    assert result["updated_at"] == (100 if status == expected else 1234)
    service.sapphire.w3.eth.get_transaction_receipt.assert_not_called()
    service.sapphire.wait_for_receipt.assert_not_called()


@pytest.mark.asyncio
async def test_migrated_wrong_non_null_shares_are_rebuilt_by_the_worker(test_db):
    from src.core.db import _run_migrations

    _hashed_row(test_db, status="completed")
    db_write(test_db, "UPDATE earn_transactions SET shares_delta = '999', exchange_rate = '1', settled_at = 100")
    db_write(test_db, "DELETE FROM data_migrations")
    _run_migrations(test_db)
    service = _settlement_service()
    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        assert await EarnWorker()._recover() is False
    result = test_db.execute("SELECT * FROM earn_transactions").fetchone()
    assert (result["shares_delta"], result["settled_at"], result["updated_at"]) == ("100", 1234, 100)
    _run_migrations(test_db)
    service.sapphire.w3.eth.get_transaction_receipt.reset_mock()
    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        await EarnWorker()._recover()
    service.sapphire.w3.eth.get_transaction_receipt.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,blocks", [("pending", True), ("failed", False)])
async def test_missing_receipt_is_not_proof_of_failure(test_db, status, blocks):
    from web3.exceptions import TransactionNotFound

    _hashed_row(test_db, status=status)
    service = _settlement_service()
    service.sapphire.w3.eth.get_transaction_receipt.side_effect = TransactionNotFound("not indexed")
    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        assert await EarnWorker()._recover() is blocks
        result = test_db.execute("SELECT * FROM earn_transactions").fetchone()
        assert result["status"] == status
        assert result["settled_at"] is None
        assert result["updated_at"] == 100
        service.sapphire.w3.eth.get_transaction_receipt.side_effect = None
        assert await EarnWorker()._recover() is False
    assert _status(test_db, "t1") == "undeployed"


@pytest.mark.asyncio
async def test_archive_failure_does_not_hold_outcome_or_prevent_other_rows(test_db):
    _hashed_row(test_db, "t1")
    _hashed_row(test_db, "t2", status="completed")
    service = _settlement_service()
    read_pool = service._history.functions.pools.return_value.call.side_effect
    service._history.functions.pools.return_value.call.side_effect = [
        ConnectionError("archive unavailable"), read_pool(42), read_pool(41),
    ]
    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        assert await EarnWorker()._recover() is False
        rows = test_db.execute("SELECT * FROM earn_transactions ORDER BY id").fetchall()
        assert rows[0]["status"] == "undeployed"
        assert rows[0]["settled_at"] is None
        assert rows[0]["updated_at"] == 1234
        assert rows[1]["shares_delta"] == "100"
        service._history.functions.pools.return_value.call.side_effect = read_pool
        assert await EarnWorker()._recover() is False
    assert test_db.execute("SELECT shares_delta FROM earn_transactions WHERE id = 't1'").fetchone()[0] == "100"


@pytest.mark.asyncio
async def test_reverted_receipt_is_final_and_is_not_polled_again(test_db):
    _hashed_row(test_db)
    service = _settlement_service()
    service.sapphire.w3.eth.get_transaction_receipt.return_value = {
        "status": 0, "blockNumber": 42, "to": _EARN_MANAGER,
    }
    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        assert await EarnWorker()._recover() is False
        service.sapphire.w3.eth.get_transaction_receipt.reset_mock()
        await EarnWorker()._recover()
    result = test_db.execute("SELECT * FROM earn_transactions").fetchone()
    assert result["status"] == "failed"
    assert result["settled_at"] == 1234
    assert result["shares_delta"] is None
    service._history.functions.pools.assert_not_called()
    service.sapphire.w3.eth.get_transaction_receipt.assert_not_called()


@pytest.mark.asyncio
async def test_ambiguous_block_records_no_delta_and_is_not_polled_again(test_db):
    _hashed_row(test_db, status="failed")
    service = _settlement_service(assets_moved=1500)
    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        assert await EarnWorker()._recover() is False
        service.sapphire.w3.eth.get_transaction_receipt.reset_mock()
        await EarnWorker()._recover()
    result = test_db.execute("SELECT * FROM earn_transactions").fetchone()
    assert result["status"] == "undeployed"
    assert result["settled_at"] == 1234
    assert result["shares_delta"] is result["exchange_rate"] is None
    service.sapphire.w3.eth.get_transaction_receipt.assert_not_called()


@pytest.mark.asyncio
async def test_a_tx_to_another_contract_settles_without_shares(test_db):
    _hashed_row(test_db, status="failed")
    service = _settlement_service()
    service.sapphire.w3.eth.get_transaction_receipt.return_value = {
        "status": 1, "blockNumber": 42, "to": "0x" + "22" * 20,
    }
    with patch("src.services.earn.worker.get_vault_service", return_value=service):
        assert await EarnWorker()._recover() is False
        service.sapphire.w3.eth.get_transaction_receipt.reset_mock()
        await EarnWorker()._recover()
    result = test_db.execute("SELECT * FROM earn_transactions").fetchone()
    assert (result["status"], result["settled_at"], result["updated_at"]) == ("undeployed", 1234, 1234)
    assert result["shares_delta"] is None
    service._history.functions.pools.assert_not_called()
    service.sapphire.w3.eth.get_transaction_receipt.assert_not_called()


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
