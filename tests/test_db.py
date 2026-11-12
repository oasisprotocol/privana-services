import sqlite3

import pytest

import src.core.db as db_module
from src.core.db import close_db, db_write


class TestMigrations:
    def test_creates_quotes_table(self, test_db):
        rows = test_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='quotes'"
        ).fetchall()
        assert len(rows) == 1

    def test_creates_swaps_table(self, test_db):
        rows = test_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='swaps'"
        ).fetchall()
        assert len(rows) == 1

    def test_creates_swaps_status_index(self, test_db):
        rows = test_db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_swaps_status'"
        ).fetchall()
        assert len(rows) == 1

    def test_queued_evm_txs_get_increasing_ids(self, test_db):
        insert = (
            "INSERT INTO evm_transactions (chain_id, address, owner, to_address, data, value, gas, "
            "created_at, updated_at) VALUES (8453, '0xlp', ?, '0xto', '0x', '0', 21000, 0, 0)"
        )
        first = test_db.execute(insert, ("swap:a",)).lastrowid
        second = test_db.execute(insert, ("swap:b",)).lastrowid
        row = test_db.execute("SELECT status, simulate FROM evm_transactions WHERE id = ?", (first,)).fetchone()
        assert second > first
        assert (row["status"], row["simulate"]) == ("queued", 1)

    def test_creates_swaps_user_index(self, test_db):
        rows = test_db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_swaps_user'"
        ).fetchall()
        assert len(rows) == 1

    def test_creates_quotes_expires_index(self, test_db):
        rows = test_db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_quotes_expires'"
        ).fetchall()
        assert len(rows) == 1

    def test_migrations_are_idempotent(self, test_db):
        db_module._run_migrations(test_db)
        db_module._run_migrations(test_db)
        rows = test_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [r["name"] for r in rows]
        assert "quotes" in table_names
        assert "swaps" in table_names


class TestBatcherUpgrade:
    def test_the_batcher_upgrade_parks_every_lifi_swap_in_flight_once(self, test_db):
        rows = {
            "executing": ("executing", "lifi"),
            "refunding": ("refunding", "lifi"),
            "done": ("completed", "lifi"),
            "internal": ("executing", "internal"),
        }

        def insert(swap_id, status, venue):
            test_db.execute(
                "INSERT INTO swaps (id, quote_id, user_address, from_token_id, to_token_id, from_amount, "
                "to_amount_estimate, status, venue, created_at, updated_at) "
                "VALUES (?, 'q', '0xuser', '0xaa', '0xbb', '1', '1', ?, ?, 0, 0)",
                (swap_id, status, venue),
            )

        for swap_id, (status, venue) in rows.items():
            insert(swap_id, status, venue)
        test_db.execute("DELETE FROM data_migrations WHERE name = 'lifi_park_unbatched_v1'")

        db_module._run_migrations(test_db)
        insert("after_upgrade", "executing", "lifi")
        db_module._run_migrations(test_db)

        statuses = dict(test_db.execute("SELECT id, status FROM swaps").fetchall())
        assert statuses == {
            "executing": "failed",
            "refunding": "failed",
            "done": "completed",
            "internal": "executing",
            "after_upgrade": "executing",
        }
        error = test_db.execute("SELECT error FROM swaps WHERE id = 'refunding'").fetchone()[0]
        assert error == "in flight at the batcher upgrade; manual recovery required"


class TestDbWrite:
    def test_a_failed_write_leaves_no_transaction_open(self, test_db):
        db_write(test_db, "INSERT INTO data_migrations (name) VALUES ('once')")
        with pytest.raises(sqlite3.IntegrityError):
            db_write(test_db, "INSERT INTO data_migrations (name) VALUES ('once')")

        assert not test_db.in_transaction

    def test_inserts_and_commits(self, test_db):
        db_write(
            test_db,
            "INSERT INTO quotes (id, user_address, from_token_id, to_token_id, "
            "from_chain_id, to_chain_id, from_amount, to_amount_gross, "
            "to_amount_estimate, to_amount_min, route_tool, liquidity_provider, "
            "expires_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("q1", "0xuser", "0xaaa", "0xbbb", 1, 1,
             "100", "100", "99", "98", "uni", "0xlp", 9999999999, 1000),
        )
        row = test_db.execute("SELECT * FROM quotes WHERE id = 'q1'").fetchone()
        assert row is not None
        assert row["user_address"] == "0xuser"

    def test_returns_cursor_with_rowcount(self, test_db):
        db_write(
            test_db,
            "INSERT INTO quotes (id, user_address, from_token_id, to_token_id, "
            "from_chain_id, to_chain_id, from_amount, to_amount_gross, "
            "to_amount_estimate, to_amount_min, route_tool, liquidity_provider, "
            "expires_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("q1", "0xuser", "0xaaa", "0xbbb", 1, 1,
             "100", "100", "99", "98", "uni", "0xlp", 9999999999, 1000),
        )
        cursor = db_write(test_db, "DELETE FROM quotes WHERE id = 'q1'")
        assert cursor.rowcount == 1


class TestCloseDb:
    def test_close_sets_connection_to_none(self):
        conn = sqlite3.connect(":memory:")
        db_module._connection = conn
        close_db()
        assert db_module._connection is None

    def test_close_when_no_connection(self):
        db_module._connection = None
        close_db()
        assert db_module._connection is None
