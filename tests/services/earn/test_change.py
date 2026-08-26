
from src.core.db import db_write, get_db
from src.services.earn.change import MAX_SAMPLE_AGE_SEC, WINDOW_SEC, change_24h
from src.services.pool_rate_history import PoolRatePoint, store_point

POOL = "0xaaaa000000000000000000000000000000000000000000000000000000000001"
USER = "0x" + "ab" * 20
NOW = 1787184000  # aligned to the sampling grid so store_point keeps timestamps
# A comfortably-old anchor: one sampling interval beyond the 24h boundary.
ANCHOR = NOW - WINDOW_SEC - 21600


def _store_rate(ts, total_assets, total_shares):
    store_point(POOL, PoolRatePoint(ts, str(total_assets), str(total_shares)))


def _insert_cashflow(status="completed", created_at=None, updated_at=None, user=USER):
    now = created_at if created_at is not None else NOW
    db_write(
        get_db(),
        """INSERT INTO earn_transactions
           (id, operation, pool_id, user_address, token_id, amount,
            signer_address, nonce, signature, status, created_at, updated_at)
           VALUES (?, 'deposit', ?, ?, '0xtok', '100', ?, 0, '0xsig', ?, ?, ?)""",
        (
            f"tx-{status}-{now}", POOL, user.lower(), user.lower(), status,
            now, updated_at if updated_at is not None else now,
        ),
    )


class TestChange24h:
    def test_positive_yield(self, test_db):
        _store_rate(ANCHOR, 100_000, 100_000)  # rate 1.0
        result = change_24h(USER, POOL, 500, 105_000, 100_000, NOW)  # rate 1.05
        assert result is not None
        assert result.amount == "25"  # 500*1.05 - 500*1.0
        assert result.pct == "0.050000"

    def test_flat_rate_is_zero_not_null(self, test_db):
        _store_rate(ANCHOR, 100_000, 100_000)
        result = change_24h(USER, POOL, 500, 100_000, 100_000, NOW)
        assert result is not None
        assert result.amount == "0"
        assert result.pct == "0.000000"

    def test_loss_is_negative_not_clamped(self, test_db):
        _store_rate(ANCHOR, 100_000, 100_000)
        result = change_24h(USER, POOL, 500, 98_000, 100_000, NOW)
        assert result is not None
        assert result.amount == "-10"
        assert result.pct == "-0.020000"

    def test_uses_newest_sample_at_or_before_window_start(self, test_db):
        _store_rate(ANCHOR - 21600, 90_000, 100_000)
        _store_rate(ANCHOR, 100_000, 100_000)
        result = change_24h(USER, POOL, 500, 105_000, 100_000, NOW)
        assert result.amount == "25"

    def test_no_identity_returns_none(self, test_db):
        _store_rate(ANCHOR, 100_000, 100_000)
        assert change_24h(None, POOL, 500, 105_000, 100_000, NOW) is None
        assert change_24h("", POOL, 500, 105_000, 100_000, NOW) is None

    def test_zero_shares_returns_none(self, test_db):
        _store_rate(ANCHOR, 100_000, 100_000)
        assert change_24h(USER, POOL, 0, 105_000, 100_000, NOW) is None

    def test_no_sample_old_enough_returns_none(self, test_db):
        _store_rate(NOW - WINDOW_SEC + 3600, 100_000, 100_000)  # only 23h old
        assert change_24h(USER, POOL, 500, 105_000, 100_000, NOW) is None

    def test_sample_exactly_at_the_boundary_is_accepted(self, test_db):
        """observed_at records the real reading time, so a sample taken 24h ago
        is 24h old and needs no padding to prove it."""
        _store_rate(NOW - WINDOW_SEC, 100_000, 105_000)

        assert change_24h(USER, POOL, 500, 105_000, 105_000, NOW) is not None

    def test_legacy_row_without_observed_at_is_judged_conservatively(self, test_db):
        """Rows written before observed_at existed only carry a floored label.
        The true reading could be anywhere inside that slot, so the end of the
        slot is used and a borderline row is refused rather than trusted."""
        db_write(
            get_db(),
            "INSERT INTO pool_rate_history (pool_id, timestamp, total_assets, "
            "total_shares, observed_at) VALUES (?, ?, ?, ?, NULL)",
            (POOL, NOW - WINDOW_SEC, "100000", "100000"),
        )

        assert change_24h(USER, POOL, 500, 105_000, 100_000, NOW) is None

    def test_stale_sample_returns_none(self, test_db):
        _store_rate(NOW - MAX_SAMPLE_AGE_SEC - 21600, 100_000, 100_000)
        assert change_24h(USER, POOL, 500, 105_000, 100_000, NOW) is None

    def test_cashflow_in_window_returns_none(self, test_db):
        _store_rate(ANCHOR, 100_000, 100_000)
        _insert_cashflow(created_at=NOW - 3600)
        assert change_24h(USER, POOL, 500, 105_000, 100_000, NOW) is None

    def test_pending_cashflow_in_window_returns_none(self, test_db):
        _store_rate(ANCHOR, 100_000, 100_000)
        _insert_cashflow(status="pending", created_at=NOW - 3600)
        assert change_24h(USER, POOL, 500, 105_000, 100_000, NOW) is None

    def test_failed_cashflow_in_window_is_ignored(self, test_db):
        _store_rate(ANCHOR, 100_000, 100_000)
        _insert_cashflow(status="failed", created_at=NOW - 3600)
        assert change_24h(USER, POOL, 500, 105_000, 100_000, NOW) is not None

    def test_old_cashflow_outside_window_is_ignored(self, test_db):
        _store_rate(ANCHOR, 100_000, 100_000)
        _insert_cashflow(created_at=ANCHOR - 7200)
        assert change_24h(USER, POOL, 500, 105_000, 100_000, NOW) is not None

    def test_recently_updated_old_cashflow_counts(self, test_db):
        # A pending row from last week that settles inside the window moved
        # value inside the window.
        _store_rate(ANCHOR, 100_000, 100_000)
        _insert_cashflow(created_at=NOW - 7 * 86400, updated_at=NOW - 3600)
        assert change_24h(USER, POOL, 500, 105_000, 100_000, NOW) is None

    def test_other_users_cashflow_is_ignored(self, test_db):
        _store_rate(ANCHOR, 100_000, 100_000)
        _insert_cashflow(created_at=NOW - 3600, user="0x" + "cd" * 20)
        assert change_24h(USER, POOL, 500, 105_000, 100_000, NOW) is not None

    def test_user_match_is_case_insensitive(self, test_db):
        _store_rate(ANCHOR, 100_000, 100_000)
        _insert_cashflow(created_at=NOW - 3600)
        assert change_24h(USER.upper().replace("0X", "0x"), POOL, 500, 105_000, 100_000, NOW) is None

    def test_zero_base_returns_none(self, test_db):
        _store_rate(ANCHOR, 0, 100_000)
        assert change_24h(USER, POOL, 500, 105_000, 100_000, NOW) is None

    def test_zero_total_shares_in_sample_returns_none(self, test_db):
        _store_rate(ANCHOR, 100_000, 0)
        assert change_24h(USER, POOL, 500, 105_000, 100_000, NOW) is None

    def test_identity_matches_value_difference(self, test_db):
        # change == value_now - value_then exactly, in integer base units
        _store_rate(ANCHOR, 123_457, 98_765)
        shares = 4321
        result = change_24h(USER, POOL, shares, 130_001, 98_765, NOW)
        expected = shares * 130_001 // 98_765 - shares * 123_457 // 98_765
        assert result.amount == str(expected)

    def test_mixed_case_ledger_pool_id_still_counts(self, test_db):
        _store_rate(ANCHOR, 100_000, 100_000)
        db_write(
            get_db(),
            """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, status, created_at, updated_at)
               VALUES ('tx-case', 'deposit', ?, ?, '0xtok', '100', ?, 0, '0xsig',
                       'completed', ?, ?)""",
            (POOL.upper().replace("0X", "0x"), USER.lower(), USER.lower(),
             NOW - 3600, NOW - 3600),
        )
        assert change_24h(USER, POOL, 500, 105_000, 100_000, NOW) is None

    def test_cashflow_between_anchor_and_window_returns_none(self, test_db):
        # The anchor can be older than the nominal 24h; a deposit in that band
        # changes the share count mid-measurement and must null the badge.
        _store_rate(ANCHOR, 100_000, 100_000)
        _insert_cashflow(created_at=NOW - WINDOW_SEC - 3600)  # ~25h ago
        assert change_24h(USER, POOL, 500, 105_000, 100_000, NOW) is None

    def test_withdraw_consent_signer_counts_as_cashflow(self, test_db):
        # Withdraw rows carry the payout recipient in user_address; the share
        # owner is the consent signer and must trip the guard.
        _store_rate(ANCHOR, 100_000, 100_000)
        db_write(
            get_db(),
            """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, status, created_at, updated_at,
                consent_signer)
               VALUES ('tx-wd', 'withdraw', ?, ?, '0xtok', '100', '0xpool', 0,
                       '0xsig', 'completed', ?, ?, ?)""",
            (POOL, "0x" + "ef" * 20, NOW - 3600, NOW - 3600, USER.lower()),
        )
        assert change_24h(USER, POOL, 500, 105_000, 100_000, NOW) is None
