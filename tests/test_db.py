import sqlite3

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


class TestDedupeSwapQuoteIds:
    """Regression coverage for a pre-existing DB with duplicate quote_ids.

    idx_swaps_quote_id is a UNIQUE index added after duplicates could already
    exist; _dedupe_swap_quote_ids must clear them out first or that index's
    own migration fails on every future boot.
    """

    def _bare_conn(self):
        # Everything up to (but not including) the dedupe step: lets us
        # insert duplicate quote_ids the way an old, pre-fix DB could.
        idx = db_module.MIGRATIONS.index(db_module._dedupe_swap_quote_ids)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        for migration in db_module.MIGRATIONS[:idx]:
            conn.execute(migration)
        conn.commit()
        return conn

    def _insert_swap(self, conn, swap_id, quote_id, created_at=1000):
        conn.execute(
            """INSERT INTO swaps
               (id, quote_id, user_address, from_token_id, to_token_id, from_amount,
                to_amount_estimate, status, created_at, updated_at)
               VALUES (?, ?, 'u', 'a', 'b', '1', '1', 'scheduled', ?, ?)""",
            (swap_id, quote_id, created_at, created_at),
        )
        conn.commit()

    def test_reassigns_all_but_the_first_duplicate(self):
        conn = self._bare_conn()
        self._insert_swap(conn, "s1", "q1", created_at=1000)
        self._insert_swap(conn, "s2", "q1", created_at=2000)
        self._insert_swap(conn, "s3", "q2", created_at=1000)

        db_module._dedupe_swap_quote_ids(conn)

        rows = {r["id"]: r["quote_id"] for r in conn.execute("SELECT id, quote_id FROM swaps")}
        assert rows["s1"] == "q1"
        assert rows["s2"] not in ("q1", "q2")
        assert rows["s3"] == "q2"

    def test_no_duplicates_is_a_noop(self):
        conn = self._bare_conn()
        self._insert_swap(conn, "s1", "q1")
        self._insert_swap(conn, "s2", "q2")

        db_module._dedupe_swap_quote_ids(conn)

        rows = {r["id"]: r["quote_id"] for r in conn.execute("SELECT id, quote_id FROM swaps")}
        assert rows == {"s1": "q1", "s2": "q2"}

    def test_result_is_safe_for_the_unique_index(self):
        conn = self._bare_conn()
        self._insert_swap(conn, "s1", "q1")
        self._insert_swap(conn, "s2", "q1")

        db_module._dedupe_swap_quote_ids(conn)

        conn.execute("CREATE UNIQUE INDEX idx_swaps_quote_id ON swaps(quote_id)")

    def test_run_migrations_recovers_from_pre_existing_duplicates(self):
        conn = self._bare_conn()
        self._insert_swap(conn, "s1", "q1", created_at=1000)
        self._insert_swap(conn, "s2", "q1", created_at=2000)

        db_module._run_migrations(conn)

        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_swaps_quote_id'"
        ).fetchall()
        assert len(rows) == 1


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
