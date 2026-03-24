import json
import sqlite3
import time

import pytest

import src.db as db_module
from src.db import db_write
from src.fees import calculate_fee
from src.models.types import Settings
from src.validation import validate_address, validate_amount, validate_token_id


@pytest.fixture(autouse=True)
def test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db_module._run_migrations(conn)
    db_module._connection = conn
    yield conn
    conn.close()
    db_module._connection = None


def _insert_quote(conn: sqlite3.Connection, quote_id: str, expires_at: int, **overrides) -> None:
    now = int(time.time())
    defaults = {
        "user_address": "0xuser",
        "from_token_id": "0xaaa",
        "to_token_id": "0xbbb",
        "from_chain_id": 1,
        "to_chain_id": 1,
        "from_amount": "1000000",
        "to_amount_estimate": "990000",
        "to_amount_min": "980000",
        "lifi_response": json.dumps({"estimate": {"toAmount": "1000000", "toAmountMin": "990000"}, "tool": "uniswap"}),
        "approval_address": None,
        "created_at": now,
    }
    defaults.update(overrides)
    db_write(
        conn,
        """INSERT INTO quotes
           (id, user_address, from_token_id, to_token_id, from_chain_id, to_chain_id,
            from_amount, to_amount_estimate, to_amount_min, lifi_response,
            approval_address, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            quote_id, defaults["user_address"], defaults["from_token_id"], defaults["to_token_id"],
            defaults["from_chain_id"], defaults["to_chain_id"], defaults["from_amount"],
            defaults["to_amount_estimate"], defaults["to_amount_min"], defaults["lifi_response"],
            defaults["approval_address"], expires_at, defaults["created_at"],
        ),
    )


class TestCalculateFee:
    def test_basic_fee(self):
        net, fee = calculate_fee(1_000_000, 10)
        assert fee == 1_000
        assert net == 999_000

    def test_zero_amount(self):
        net, fee = calculate_fee(0, 10)
        assert fee == 0
        assert net == 0

    def test_zero_bps(self):
        net, fee = calculate_fee(1_000_000, 0)
        assert fee == 0
        assert net == 1_000_000

    def test_truncation_on_small_amount(self):
        net, fee = calculate_fee(99, 10)
        assert fee == 0
        assert net == 99

    def test_100_bps_is_one_percent(self):
        net, fee = calculate_fee(10_000, 100)
        assert fee == 100
        assert net == 9_900

    def test_net_plus_fee_equals_gross(self):
        gross = 123_456_789
        net, fee = calculate_fee(gross, 25)
        assert net + fee == gross

    def test_wei_scale_amount(self):
        gross = 1_000_000_000_000_000_000
        net, fee = calculate_fee(gross, 10)
        assert fee == 1_000_000_000_000_000
        assert net == 999_000_000_000_000_000


class TestValidateTokenId:
    def test_valid_hex(self):
        validate_token_id("0xabcdef1234567890")

    def test_rejects_no_prefix(self):
        with pytest.raises(ValueError, match="hex string"):
            validate_token_id("abcdef")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="hex string"):
            validate_token_id("")

    def test_rejects_non_hex(self):
        with pytest.raises(ValueError, match="hex string"):
            validate_token_id("0xghijkl")

    def test_rejects_bare_0x(self):
        with pytest.raises(ValueError, match="hex string"):
            validate_token_id("0x")


class TestValidateAddress:
    def test_valid_address(self):
        validate_address("0x" + "a" * 40)

    def test_rejects_short(self):
        with pytest.raises(ValueError, match="hex address"):
            validate_address("0x" + "a" * 39)

    def test_rejects_long(self):
        with pytest.raises(ValueError, match="hex address"):
            validate_address("0x" + "a" * 41)

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="hex address"):
            validate_address("")


class TestValidateAmount:
    def test_valid_amount(self):
        validate_amount("1000000")

    def test_rejects_zero(self):
        with pytest.raises(ValueError, match="greater than zero"):
            validate_amount("0")

    def test_rejects_negative(self):
        with pytest.raises(ValueError, match="greater than zero"):
            validate_amount("-100")

    def test_rejects_non_numeric(self):
        with pytest.raises(ValueError, match="valid integer"):
            validate_amount("abc")

    def test_rejects_float_string(self):
        with pytest.raises(ValueError, match="valid integer"):
            validate_amount("1.5")


class TestQuoteDeduplication:
    def _make_service(self):
        from src.services.quote_service import QuoteService
        service = QuoteService.__new__(QuoteService)
        service.settings = Settings()
        service._last_cleanup = 0
        return service

    def test_returns_existing_unexpired_quote(self, test_db):
        future = int(time.time()) + 300
        _insert_quote(test_db, "q1", expires_at=future)
        service = self._make_service()
        result = service._find_existing_quote("0xuser", "0xaaa", "0xbbb", "1000000")
        assert result is not None
        assert result.quote_id == "q1"

    def test_returns_none_for_expired_quote(self, test_db):
        past = int(time.time()) - 10
        _insert_quote(test_db, "q2", expires_at=past)
        service = self._make_service()
        result = service._find_existing_quote("0xuser", "0xaaa", "0xbbb", "1000000")
        assert result is None

    def test_returns_none_for_different_user(self, test_db):
        future = int(time.time()) + 300
        _insert_quote(test_db, "q3", expires_at=future, user_address="0xother")
        service = self._make_service()
        result = service._find_existing_quote("0xuser", "0xaaa", "0xbbb", "1000000")
        assert result is None

    def test_returns_none_for_different_amount(self, test_db):
        future = int(time.time()) + 300
        _insert_quote(test_db, "q4", expires_at=future)
        service = self._make_service()
        result = service._find_existing_quote("0xuser", "0xaaa", "0xbbb", "9999999")
        assert result is None


class TestExpiredQuoteCleanup:
    def _make_service(self):
        from src.services.quote_service import QuoteService
        service = QuoteService.__new__(QuoteService)
        service.settings = Settings()
        service._last_cleanup = 0
        return service

    def test_deletes_expired_quotes(self, test_db):
        past = int(time.time()) - 10
        _insert_quote(test_db, "expired_1", expires_at=past)
        _insert_quote(test_db, "expired_2", expires_at=past)
        service = self._make_service()
        deleted = service.cleanup_expired_quotes()
        assert deleted == 2
        row = test_db.execute("SELECT COUNT(*) as cnt FROM quotes").fetchone()
        assert row["cnt"] == 0

    def test_preserves_valid_quotes(self, test_db):
        future = int(time.time()) + 300
        past = int(time.time()) - 10
        _insert_quote(test_db, "valid_1", expires_at=future)
        _insert_quote(test_db, "expired_1", expires_at=past)
        service = self._make_service()
        deleted = service.cleanup_expired_quotes()
        assert deleted == 1
        row = test_db.execute("SELECT id FROM quotes").fetchone()
        assert row["id"] == "valid_1"

    def test_throttles_cleanup(self, test_db):
        past = int(time.time()) - 10
        _insert_quote(test_db, "expired_1", expires_at=past)
        service = self._make_service()
        service.cleanup_expired_quotes()
        _insert_quote(test_db, "expired_2", expires_at=past)
        deleted = service.cleanup_expired_quotes()
        assert deleted == 0
        row = test_db.execute("SELECT COUNT(*) as cnt FROM quotes").fetchone()
        assert row["cnt"] == 1
