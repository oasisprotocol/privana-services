import sqlite3
import time

import httpx
import pytest
from dotenv import load_dotenv

load_dotenv(".env.localnet")

import src.core.db as db_module
from src.core.config import load_settings
from src.core.db import db_write

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


@pytest.fixture
def insert_quote(test_db):
    def _insert(quote_id, expires_at=None, **overrides):
        now = int(time.time())
        if expires_at is None:
            expires_at = now + 300
        defaults = {
            "user_address": "0xuser",
            "from_token_id": "0xaaa",
            "to_token_id": "0xbbb",
            "from_chain_id": 84532,
            "to_chain_id": 84532,
            "from_amount": "1000000",
            "to_amount_gross": "1000000",
            "to_amount_estimate": "990000",
            "to_amount_min": "980000",
            "route_tool": "uniswap",
            "liquidity_provider": _settings.liquidity_provider_address,
            "created_at": now,
            "venue": "internal",
        }
        defaults.update(overrides)
        db_write(
            test_db,
            """INSERT INTO quotes
               (id, user_address, from_token_id, to_token_id, from_chain_id, to_chain_id,
                from_amount, to_amount_gross, to_amount_estimate, to_amount_min,
                route_tool, liquidity_provider, expires_at, created_at, venue)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                quote_id, defaults["user_address"], defaults["from_token_id"],
                defaults["to_token_id"], defaults["from_chain_id"], defaults["to_chain_id"],
                defaults["from_amount"], defaults["to_amount_gross"],
                defaults["to_amount_estimate"], defaults["to_amount_min"],
                defaults["route_tool"], defaults["liquidity_provider"],
                expires_at, defaults["created_at"], defaults["venue"],
            ),
        )
    return _insert


@pytest.fixture
async def api_client():
    import src.clients.accounting as acct_mod
    import src.clients.lifi as lifi_mod
    import src.clients.sapphire as saph_mod
    import src.services.earn.vault_service as vs_mod
    import src.services.swap.executor as se_mod
    import src.services.swap.quote_service as qs_mod
    acct_mod._client_instance = None
    lifi_mod._client_instance = None
    saph_mod._client_instance = None
    qs_mod._service_instance = None
    se_mod._executor_instance = None
    vs_mod._service_instance = None

    from src.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, timeout=30, base_url="http://test") as c:
        yield c

    acct_mod._client_instance = None
    lifi_mod._client_instance = None
    saph_mod._client_instance = None
    qs_mod._service_instance = None
    se_mod._executor_instance = None
    vs_mod._service_instance = None
