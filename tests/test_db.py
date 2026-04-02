import sqlite3

import src.core.db as db_module
from src.core.db import db_write, close_db


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


class TestDbWrite:
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
