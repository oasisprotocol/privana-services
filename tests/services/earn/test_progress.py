import json

from src.core.db import db_write
from src.services.earn import progress


def _row(db, tx_id: str = "tx-1") -> None:
    db_write(
        db,
        """INSERT INTO earn_transactions
           (id, operation, pool_id, user_address, token_id, amount,
            signer_address, nonce, signature, status, created_at, updated_at)
           VALUES (?, 'withdraw', '0xpool', '0xuser', '0xtok', '1', '0xuser', 1, '0xsig',
                   'executing', 100, 100)""",
        (tx_id,),
    )


def _stages(db, tx_id: str = "tx-1") -> list[dict]:
    raw = db.execute("SELECT stages FROM earn_transactions WHERE id = ?", (tx_id,)).fetchone()[0]
    return json.loads(raw) if raw else []


def test_records_each_stage_in_order_for_the_tracked_operation(test_db):
    _row(test_db)
    with progress.tracking("tx-1"):
        progress.report(progress.RECLAIMING)
        progress.report(progress.RETURNING)
        progress.report(progress.PAYING_OUT)

    assert [s["stage"] for s in _stages(test_db)] == ["reclaiming", "returning", "paying_out"]


def test_a_repeated_stage_updates_its_detail_instead_of_adding_an_entry(test_db):
    _row(test_db)
    with progress.tracking("tx-1"):
        progress.report_finality("400 Bad Request: Insufficient finality: 9/32 confirmations")
        progress.report_finality("400 Bad Request: Insufficient finality: 20/32 confirmations")

    stages = _stages(test_db)
    assert len(stages) == 1
    assert stages[0]["stage"] == "finality"
    assert stages[0]["detail"] == {"confirmations": 20, "required": 32}


def test_reports_nothing_outside_a_tracked_operation(test_db):
    _row(test_db)
    progress.report(progress.BRIDGING)

    assert _stages(test_db) == []


def test_an_error_without_a_confirmation_count_is_ignored(test_db):
    _row(test_db)
    with progress.tracking("tx-1"):
        progress.report_finality("connection reset")

    assert _stages(test_db) == []
