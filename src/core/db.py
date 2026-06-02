import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent.parent.parent / "data" / "privana-services.db"
_connection: Optional[sqlite3.Connection] = None
_write_lock = threading.Lock()

MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS quotes (
        id TEXT PRIMARY KEY,
        user_address TEXT NOT NULL,
        from_token_id TEXT NOT NULL,
        to_token_id TEXT NOT NULL,
        from_chain_id INTEGER NOT NULL,
        to_chain_id INTEGER NOT NULL,
        from_amount TEXT NOT NULL,
        to_amount_gross TEXT NOT NULL,
        to_amount_estimate TEXT NOT NULL,
        to_amount_min TEXT NOT NULL,
        route_tool TEXT,
        liquidity_provider TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS swaps (
        id TEXT PRIMARY KEY,
        quote_id TEXT NOT NULL,
        user_address TEXT NOT NULL,
        from_token_id TEXT NOT NULL,
        to_token_id TEXT NOT NULL,
        from_amount TEXT NOT NULL,
        to_amount_estimate TEXT NOT NULL,
        to_amount_actual TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        swap_tx_hash TEXT,
        error TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_swaps_status ON swaps(status);",
    "CREATE INDEX IF NOT EXISTS idx_swaps_user ON swaps(user_address);",
    "CREATE INDEX IF NOT EXISTS idx_quotes_expires ON quotes(expires_at);",
    """
    CREATE TABLE IF NOT EXISTS earn_transactions (
        id TEXT PRIMARY KEY,
        operation TEXT NOT NULL,
        pool_id TEXT NOT NULL,
        user_address TEXT NOT NULL,
        token_id TEXT NOT NULL,
        amount TEXT NOT NULL,
        signer_address TEXT NOT NULL,
        nonce INTEGER NOT NULL,
        signature TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        tx_hash TEXT,
        error TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_earn_tx_status ON earn_transactions(status);",
    "CREATE INDEX IF NOT EXISTS idx_earn_tx_user ON earn_transactions(user_address);",
    "ALTER TABLE swaps ADD COLUMN output_nonce INTEGER;",
    "ALTER TABLE swaps ADD COLUMN output_signature TEXT;",
]


def get_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    global _connection
    if _connection is not None:
        return _connection

    path = db_path or _DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    _connection = sqlite3.connect(str(path), check_same_thread=False)
    _connection.row_factory = sqlite3.Row
    _connection.execute("PRAGMA journal_mode=WAL")
    _connection.execute("PRAGMA foreign_keys=ON")

    _run_migrations(_connection)
    logger.info(f"Database initialized at {path}")
    return _connection


def db_write(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _write_lock:
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor


def close_db() -> None:
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None
        logger.info("Database connection closed")


def _run_migrations(conn: sqlite3.Connection) -> None:
    for sql in MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                continue
            raise
    conn.commit()
