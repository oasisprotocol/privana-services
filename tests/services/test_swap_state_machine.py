import sqlite3
import time

import pytest

import src.db as db_module
from src.db import db_write
from src.models.swap import (
    SUBMISSION_ACCEPTED,
    VALID_TRANSITIONS,
    SwapStatus,
)


@pytest.fixture(autouse=True)
def test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db_module._run_migrations(conn)
    db_module._connection = conn
    yield conn
    conn.close()
    db_module._connection = None


def _insert_swap(conn: sqlite3.Connection, swap_id: str, status: SwapStatus) -> None:
    now = int(time.time())
    db_write(
        conn,
        """INSERT INTO swaps
           (id, quote_id, user_address, from_token_id, to_token_id,
            from_chain_id, to_chain_id, from_amount, to_amount_estimate,
            to_amount_min, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            swap_id, "quote_1", "0xuser", "0xtoken_a", "0xtoken_b",
            84532, 84532, "1000000", "990000", "980000",
            status.value, now, now,
        ),
    )


class TestSwapStatusProperties:
    def test_active_states(self):
        expected = {
            SwapStatus.PENDING_LOCK,
            SwapStatus.LOCKED,
            SwapStatus.MONITORING,
            SwapStatus.SETTLING,
            SwapStatus.REFUNDING,
        }
        actual = {s for s in SwapStatus if s.is_active}
        assert actual == expected

    def test_terminal_states(self):
        expected = {SwapStatus.COMPLETED, SwapStatus.REFUNDED}
        actual = {s for s in SwapStatus if s.is_terminal}
        assert actual == expected

    def test_failure_states(self):
        expected = {SwapStatus.SWAP_FAILED, SwapStatus.SETTLE_FAILED}
        actual = {s for s in SwapStatus if s.is_failure}
        assert actual == expected

    def test_quoted_is_inert(self):
        assert not SwapStatus.QUOTED.is_active
        assert not SwapStatus.QUOTED.is_terminal
        assert not SwapStatus.QUOTED.is_failure

    def test_categories_are_disjoint(self):
        for status in SwapStatus:
            flags = [status.is_active, status.is_terminal, status.is_failure]
            assert sum(flags) <= 1, f"{status.value} belongs to multiple categories"


class TestValidTransitions:
    def test_all_active_states_have_transitions(self):
        for status in SwapStatus:
            if status.is_active:
                assert status in VALID_TRANSITIONS, f"{status.value} missing from VALID_TRANSITIONS"

    def test_terminal_states_not_in_map(self):
        for status in SwapStatus:
            if status.is_terminal:
                assert status not in VALID_TRANSITIONS

    def test_no_self_transitions(self):
        for source, targets in VALID_TRANSITIONS.items():
            assert source not in targets, f"{source.value} can transition to itself"

    def test_happy_path_chain(self):
        chain = [
            SwapStatus.PENDING_LOCK,
            SwapStatus.LOCKED,
            SwapStatus.MONITORING,
            SwapStatus.SETTLING,
            SwapStatus.COMPLETED,
        ]
        for i in range(len(chain) - 1):
            source, target = chain[i], chain[i + 1]
            assert target in VALID_TRANSITIONS[source], (
                f"{source.value} → {target.value} not allowed"
            )

    def test_failure_path_chain(self):
        assert SwapStatus.SWAP_FAILED in VALID_TRANSITIONS[SwapStatus.MONITORING]
        assert SwapStatus.REFUNDING in VALID_TRANSITIONS[SwapStatus.SWAP_FAILED]
        assert SwapStatus.REFUNDED in VALID_TRANSITIONS[SwapStatus.REFUNDING]

    def test_settle_failure_allows_refund(self):
        allowed = VALID_TRANSITIONS[SwapStatus.SETTLE_FAILED]
        assert SwapStatus.REFUNDING in allowed
        assert SwapStatus.REFUNDED in allowed

    def test_every_active_state_can_fail(self):
        for status in SwapStatus:
            if status.is_active and not status == SwapStatus.REFUNDING:
                targets = VALID_TRANSITIONS[status]
                has_failure = any(t.is_failure or t == SwapStatus.REFUNDED for t in targets)
                assert has_failure, f"{status.value} has no failure exit"


class TestTransitionValidation:
    def test_valid_transition_succeeds(self, test_db):
        _insert_swap(test_db, "swap_1", SwapStatus.PENDING_LOCK)
        from src.services.swap_executor import SwapExecutor
        executor = SwapExecutor.__new__(SwapExecutor)
        executor._update_swap("swap_1", status=SwapStatus.LOCKED, lock_id=1)
        row = test_db.execute("SELECT status FROM swaps WHERE id = ?", ("swap_1",)).fetchone()
        assert row["status"] == "locked"

    def test_invalid_transition_raises(self, test_db):
        _insert_swap(test_db, "swap_2", SwapStatus.PENDING_LOCK)
        from src.services.swap_executor import SwapExecutor
        executor = SwapExecutor.__new__(SwapExecutor)
        with pytest.raises(ValueError, match="Invalid transition"):
            executor._update_swap("swap_2", status=SwapStatus.COMPLETED)

    def test_update_without_status_skips_validation(self, test_db):
        _insert_swap(test_db, "swap_3", SwapStatus.PENDING_LOCK)
        from src.services.swap_executor import SwapExecutor
        executor = SwapExecutor.__new__(SwapExecutor)
        executor._update_swap("swap_3", error="some error")
        row = test_db.execute("SELECT error FROM swaps WHERE id = ?", ("swap_3",)).fetchone()
        assert row["error"] == "some error"

    def test_sequential_transitions(self, test_db):
        _insert_swap(test_db, "swap_4", SwapStatus.PENDING_LOCK)
        from src.services.swap_executor import SwapExecutor
        executor = SwapExecutor.__new__(SwapExecutor)
        executor._update_swap("swap_4", status=SwapStatus.LOCKED, lock_id=1)
        executor._update_swap("swap_4", status=SwapStatus.MONITORING, swap_tx_hash="0xabc")
        executor._update_swap("swap_4", status=SwapStatus.SETTLING, to_amount_actual="990000")
        executor._update_swap("swap_4", status=SwapStatus.COMPLETED)
        row = test_db.execute("SELECT status FROM swaps WHERE id = ?", ("swap_4",)).fetchone()
        assert row["status"] == "completed"

    def test_skip_state_raises(self, test_db):
        _insert_swap(test_db, "swap_5", SwapStatus.PENDING_LOCK)
        from src.services.swap_executor import SwapExecutor
        executor = SwapExecutor.__new__(SwapExecutor)
        with pytest.raises(ValueError, match="Invalid transition"):
            executor._update_swap("swap_5", status=SwapStatus.MONITORING)


class TestSubmissionAccepted:
    def test_contains_expected_statuses(self):
        assert "submitted" in SUBMISSION_ACCEPTED
        assert "confirmed" in SUBMISSION_ACCEPTED
        assert "pending" in SUBMISSION_ACCEPTED
        assert len(SUBMISSION_ACCEPTED) == 3

    def test_rejects_unknown_status(self):
        assert "failed" not in SUBMISSION_ACCEPTED
        assert "rejected" not in SUBMISSION_ACCEPTED
