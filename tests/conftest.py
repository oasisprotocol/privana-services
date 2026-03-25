import sqlite3

import pytest
from dotenv import load_dotenv

load_dotenv()

import src.db as db_module
from src.config import load_settings

_settings = load_settings(refresh=True)


@pytest.fixture
def settings():
    return _settings


@pytest.fixture(autouse=True)
def test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db_module._run_migrations(conn)
    db_module._connection = conn
    yield conn
    conn.close()
    db_module._connection = None
