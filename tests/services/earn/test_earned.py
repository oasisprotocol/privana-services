from src.core.db import db_write, get_db
from src.services.earn.earned import (
    STATUS_LEDGER_INCOMPLETE,
    STATUS_OK,
    STATUS_PENDING_SETTLEMENT,
    STATUS_UNSUPPORTED,
    earned_active,
)

POOL = "0x" + "ab" * 32
USER = "0x" + "cd" * 20
OTHER = "0x" + "ef" * 20
T0 = 1787000000

_seq = iter(range(1000))


def _cashflow(
    operation="deposit",
    amount="100",
    shares_delta="100",
    status="completed",
    created_at=T0,
    user=USER,
    consent_signer=None,
    pool=POOL,
):
    tx_id = f"tx-{next(_seq)}"
    db_write(
        get_db(),
        """INSERT INTO earn_transactions
           (id, operation, pool_id, user_address, token_id, amount,
            signer_address, nonce, signature, status, created_at, updated_at,
            consent_signer, shares_delta)
           VALUES (?, ?, ?, ?, '0xtok', ?, '0xsig', 0, '0xsig', ?, ?, ?, ?, ?)""",
        (
            tx_id, operation, pool, user.lower(), amount, status,
            created_at, created_at,
            consent_signer.lower() if consent_signer else None,
            shares_delta,
        ),
    )
    return tx_id


class TestEarnedActive:
    def test_deposit_then_yield(self, test_db):
        # Deposit 100 for 100 shares (rate 1.0), pool now worth 1.05/share.
        _cashflow(amount="100", shares_delta="100")
        result = earned_active(USER, POOL, 100, 1050, 1000)
        assert result.status == STATUS_OK
        assert result.active == "5"
        assert result.cost_basis == "100"
        assert result.deposit_count == 1
        assert result.first_deposit_at == T0

    def test_brand_new_position_earns_zero_not_null(self, test_db):
        _cashflow(amount="100", shares_delta="100")
        result = earned_active(USER, POOL, 100, 1000, 1000)
        assert result.status == STATUS_OK
        assert result.active == "0"

    def test_loss_is_negative(self, test_db):
        _cashflow(amount="100", shares_delta="100")
        result = earned_active(USER, POOL, 100, 970, 1000)
        assert result.status == STATUS_OK
        assert result.active == "-3"

    def test_multiple_deposits_blend_the_basis(self, test_db):
        _cashflow(amount="100", shares_delta="100", created_at=T0)
        _cashflow(amount="105", shares_delta="100", created_at=T0 + 10)
        result = earned_active(USER, POOL, 200, 2200, 2000)
        # basis 205, value 200 * 1.1 = 220
        assert result.status == STATUS_OK
        assert result.cost_basis == "205"
        assert result.active == "15"

    def test_withdrawal_moves_yield_from_active_to_realised(self, test_db):
        # Spec worked example rows 1-3: deposit 100, rate to 1.05, withdraw 15.
        _cashflow(amount="100", shares_delta="100", created_at=T0)
        _cashflow(
            operation="withdraw", amount="15", shares_delta="-14",
            created_at=T0 + 10, user=OTHER, consent_signer=USER,
        )
        result = earned_active(USER, POOL, 86, 1050, 1000)
        assert result.status == STATUS_OK
        # basis out = 100 * 14 // 100 = 14, realised = 15 - 14 = 1
        assert result.realised == "1"
        assert result.cost_basis == "86"
        # value 86 * 1.05 = 90 (floor), active = 90 - 86
        assert result.active == "4"

    def test_invariant_holds_across_deposit_withdraw_redeposit(self, test_db):
        # active + realised == value_now - (deposited - withdrawn)
        _cashflow(amount="1000", shares_delta="1000", created_at=T0)
        _cashflow(
            operation="withdraw", amount="300", shares_delta="-280",
            created_at=T0 + 10, user=OTHER, consent_signer=USER,
        )
        _cashflow(amount="500", shares_delta="450", created_at=T0 + 20)

        shares = 1000 - 280 + 450
        total_assets, total_shares = 13_000, 10_000
        result = earned_active(USER, POOL, shares, total_assets, total_shares)
        assert result.status == STATUS_OK

        value_now = shares * total_assets // total_shares
        deposited, withdrawn = 1000 + 500, 300
        assert int(result.active) + int(result.realised) == value_now - (
            deposited - withdrawn
        )

    def test_full_exit_then_redeposit_starts_clean(self, test_db):
        _cashflow(amount="100", shares_delta="100", created_at=T0)
        _cashflow(
            operation="withdraw", amount="120", shares_delta="-100",
            created_at=T0 + 10, user=USER, consent_signer=USER,
        )
        _cashflow(amount="50", shares_delta="50", created_at=T0 + 20)

        result = earned_active(USER, POOL, 50, 1000, 1000)
        assert result.status == STATUS_OK
        assert result.realised == "20"
        assert result.cost_basis == "50"
        assert result.active == "0"


class TestEarnedStatuses:
    def test_no_identity_is_unsupported(self, test_db):
        _cashflow()
        assert earned_active(None, POOL, 100, 1050, 1000).status == STATUS_UNSUPPORTED

    def test_pending_row_blocks_the_figure(self, test_db):
        _cashflow(status="pending")
        result = earned_active(USER, POOL, 100, 1050, 1000)
        assert result.status == STATUS_PENDING_SETTLEMENT
        assert result.active is None

    def test_missing_shares_delta_is_incomplete(self, test_db):
        _cashflow(shares_delta=None)
        result = earned_active(USER, POOL, 100, 1050, 1000)
        assert result.status == STATUS_LEDGER_INCOMPLETE
        assert result.active is None

    def test_share_count_mismatch_is_incomplete(self, test_db):
        # Chain says 500 shares, ledger only accounts for 100.
        _cashflow(amount="100", shares_delta="100")
        result = earned_active(USER, POOL, 500, 1050, 1000)
        assert result.status == STATUS_LEDGER_INCOMPLETE
        assert result.active is None

    def test_unattributed_withdrawal_is_caught_by_the_share_check(self, test_db):
        # A legacy withdraw row with no consent_signer cannot be attributed,
        # so the ledger over-counts shares and must refuse to report.
        _cashflow(amount="100", shares_delta="100", created_at=T0)
        _cashflow(
            operation="withdraw", amount="50", shares_delta="-50",
            created_at=T0 + 10, user=USER, consent_signer=None,
        )
        result = earned_active(USER, POOL, 50, 1050, 1000)
        assert result.status == STATUS_LEDGER_INCOMPLETE

    def test_failed_rows_are_ignored(self, test_db):
        _cashflow(amount="100", shares_delta="100", created_at=T0)
        _cashflow(
            amount="999", shares_delta="999", status="failed", created_at=T0 + 10
        )
        assert earned_active(USER, POOL, 100, 1050, 1000).status == STATUS_OK

    def test_undeployed_rows_count_as_settled(self, test_db):
        _cashflow(amount="100", shares_delta="100", status="undeployed")
        result = earned_active(USER, POOL, 100, 1050, 1000)
        assert result.status == STATUS_OK
        assert result.active == "5"

    def test_other_users_cashflows_are_not_borrowed(self, test_db):
        _cashflow(amount="100", shares_delta="100", user=OTHER)
        result = earned_active(USER, POOL, 100, 1050, 1000)
        assert result.status == STATUS_LEDGER_INCOMPLETE

    def test_other_pools_cashflows_are_not_borrowed(self, test_db):
        _cashflow(amount="100", shares_delta="100", pool="0x" + "99" * 32)
        result = earned_active(USER, POOL, 100, 1050, 1000)
        assert result.status == STATUS_LEDGER_INCOMPLETE

    def test_burn_against_empty_position_is_incomplete(self, test_db):
        _cashflow(
            operation="withdraw", amount="50", shares_delta="-50",
            user=OTHER, consent_signer=USER,
        )
        result = earned_active(USER, POOL, 0, 1050, 1000)
        assert result.status == STATUS_LEDGER_INCOMPLETE
