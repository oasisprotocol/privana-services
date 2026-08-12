import uuid

from src.core.db import db_write, get_db
from src.services.portfolio.earn_positions import (
    EarnFlow,
    PrincipalPoint,
    earn_flows,
    principal_series,
)

USER = "0xD8991364507fafc256eaff950d28618735753476"
OTHER_USER = "0x705B2433B76C383c20aE0D60803334F0Ad13B6E8"
POOL_A = "0xeeed5d5fb4fdf07abc1f232dc05d0cd551bae3a1c9a83dc1cbd196893afedd29"
POOL_B = "0x639bf8459fb3bffbb88265646fd8cc6c66ff8ba0664d190799de3dc7bc01c1c1"
USDC = "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514"


def _insert_tx(
    operation,
    amount,
    updated_at,
    user=USER,
    pool_id=POOL_A,
    status="completed",
    created_at=None,
):
    db_write(
        get_db(),
        """INSERT INTO earn_transactions
           (id, operation, pool_id, user_address, token_id, amount,
            signer_address, nonce, signature, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()),
            operation,
            pool_id,
            user.lower(),
            USDC,
            amount,
            user.lower(),
            0,
            "0xsig",
            status,
            created_at if created_at is not None else updated_at - 30,
            updated_at,
        ),
    )


class TestEarnFlows:
    def test_returns_completed_flows_oldest_first(self):
        _insert_tx("withdraw", "400", updated_at=200)
        _insert_tx("deposit", "1000", updated_at=100)

        flows = earn_flows(USER)

        assert [(f.operation, f.amount, f.timestamp) for f in flows] == [
            ("deposit", 1000, 100),
            ("withdraw", 400, 200),
        ]
        assert flows[0].pool_id == POOL_A
        assert flows[0].token_id == USDC

    def test_pending_failed_and_undeployed_rows_are_excluded(self):
        for status in ("pending", "failed", "undeployed"):
            _insert_tx("deposit", "1000", updated_at=100, status=status)

        assert earn_flows(USER) == []

    def test_other_users_rows_are_excluded(self):
        _insert_tx("deposit", "1000", updated_at=100, user=OTHER_USER)

        assert earn_flows(USER) == []

    def test_user_address_matching_is_case_insensitive(self):
        _insert_tx("deposit", "1000", updated_at=100)

        assert len(earn_flows(USER.upper().replace("0X", "0x"))) == 1

    def test_uses_settlement_time_not_signing_time(self):
        _insert_tx("deposit", "1000", updated_at=500, created_at=100)

        assert earn_flows(USER)[0].timestamp == 500


class TestPrincipalSeries:
    def test_deposits_accumulate_and_withdrawals_reduce(self):
        flows = [
            EarnFlow(timestamp=100, operation="deposit", pool_id=POOL_A, token_id=USDC, amount=1000),
            EarnFlow(timestamp=200, operation="deposit", pool_id=POOL_A, token_id=USDC, amount=500),
            EarnFlow(timestamp=300, operation="withdraw", pool_id=POOL_A, token_id=USDC, amount=400),
        ]

        series = principal_series(flows)

        assert series[POOL_A] == [
            PrincipalPoint(timestamp=100, principal=1000),
            PrincipalPoint(timestamp=200, principal=1500),
            PrincipalPoint(timestamp=300, principal=1100),
        ]

    def test_pools_accumulate_independently(self):
        flows = [
            EarnFlow(timestamp=100, operation="deposit", pool_id=POOL_A, token_id=USDC, amount=1000),
            EarnFlow(timestamp=150, operation="deposit", pool_id=POOL_B, token_id=USDC, amount=70),
        ]

        series = principal_series(flows)

        assert series[POOL_A] == [PrincipalPoint(timestamp=100, principal=1000)]
        assert series[POOL_B] == [PrincipalPoint(timestamp=150, principal=70)]

    def test_withdrawing_more_than_deposited_goes_negative(self):
        flows = [
            EarnFlow(timestamp=100, operation="deposit", pool_id=POOL_A, token_id=USDC, amount=1000),
            EarnFlow(timestamp=200, operation="withdraw", pool_id=POOL_A, token_id=USDC, amount=1050),
        ]

        series = principal_series(flows)

        assert series[POOL_A][-1].principal == -50

    def test_same_timestamp_flows_collapse_to_final_state(self):
        flows = [
            EarnFlow(timestamp=100, operation="deposit", pool_id=POOL_A, token_id=USDC, amount=1000),
            EarnFlow(timestamp=100, operation="withdraw", pool_id=POOL_A, token_id=USDC, amount=300),
        ]

        series = principal_series(flows)

        assert series[POOL_A] == [PrincipalPoint(timestamp=100, principal=700)]

    def test_unknown_operations_are_ignored(self):
        flows = [
            EarnFlow(timestamp=100, operation="rebalance", pool_id=POOL_A, token_id=USDC, amount=5),
        ]

        assert principal_series(flows) == {}

    def test_empty_flows_yield_empty_series(self):
        assert principal_series([]) == {}


class TestEndToEnd:
    def test_db_rows_to_principal_series(self):
        _insert_tx("deposit", "1000", updated_at=100)
        _insert_tx("deposit", "9999", updated_at=150, status="pending")
        _insert_tx("withdraw", "400", updated_at=200)

        series = principal_series(earn_flows(USER))

        assert series[POOL_A] == [
            PrincipalPoint(timestamp=100, principal=1000),
            PrincipalPoint(timestamp=200, principal=600),
        ]
