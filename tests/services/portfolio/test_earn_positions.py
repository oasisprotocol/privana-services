import uuid
from decimal import Decimal

from src.core.db import db_write, get_db
from src.services.earn.value_history import EarnCashflow, read_user_earn_cashflows
from src.services.portfolio.earn_positions import PrincipalPoint, principal_series

USER = "0xD8991364507fafc256eaff950d28618735753476"
POOL_A = "0xeeed5d5fb4fdf07abc1f232dc05d0cd551bae3a1c9a83dc1cbd196893afedd29"
POOL_B = "0x639bf8459fb3bffbb88265646fd8cc6c66ff8ba0664d190799de3dc7bc01c1c1"
USDC = "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514"


def _flow(timestamp, signed_amount, pool_id=POOL_A):
    return EarnCashflow(
        timestamp=timestamp,
        pool_id=pool_id,
        token_id=USDC,
        signed_amount=Decimal(signed_amount),
    )


def _insert_tx(operation, amount, updated_at, status="completed", pool_id=POOL_A):
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
            USER.lower(),
            USDC,
            amount,
            USER.lower(),
            0,
            "0xsig",
            status,
            updated_at - 30,
            updated_at,
        ),
    )


class TestPrincipalSeries:
    def test_deposits_accumulate_and_withdrawals_reduce(self):
        flows = [_flow(100, 1000), _flow(200, 500), _flow(300, -400)]

        series = principal_series(flows)

        assert series[POOL_A] == [
            PrincipalPoint(timestamp=100, principal=1000),
            PrincipalPoint(timestamp=200, principal=1500),
            PrincipalPoint(timestamp=300, principal=1100),
        ]

    def test_pools_accumulate_independently(self):
        flows = [_flow(100, 1000), _flow(150, 70, pool_id=POOL_B)]

        series = principal_series(flows)

        assert series[POOL_A] == [PrincipalPoint(timestamp=100, principal=1000)]
        assert series[POOL_B] == [PrincipalPoint(timestamp=150, principal=70)]

    def test_withdrawing_more_than_deposited_goes_negative(self):
        flows = [_flow(100, 1000), _flow(200, -1050)]

        series = principal_series(flows)

        assert series[POOL_A][-1].principal == -50

    def test_same_timestamp_flows_collapse_to_final_state(self):
        flows = [_flow(100, 1000), _flow(100, -300)]

        series = principal_series(flows)

        assert series[POOL_A] == [PrincipalPoint(timestamp=100, principal=700)]

    def test_empty_flows_yield_empty_series(self):
        assert principal_series([]) == {}


class TestFromCashflowRows:
    def test_db_rows_to_principal_series(self):
        _insert_tx("deposit", "1000", updated_at=100)
        _insert_tx("deposit", "9999", updated_at=150, status="pending")
        _insert_tx("withdraw", "400", updated_at=200)

        series = principal_series(read_user_earn_cashflows(USER))

        assert series[POOL_A] == [
            PrincipalPoint(timestamp=100, principal=1000),
            PrincipalPoint(timestamp=200, principal=600),
        ]

    def test_undeployed_deposits_count_as_principal(self):
        _insert_tx("deposit", "1000", updated_at=100, status="undeployed")

        series = principal_series(read_user_earn_cashflows(USER))

        assert series[POOL_A] == [PrincipalPoint(timestamp=100, principal=1000)]

    def test_failed_and_pending_rows_do_not(self):
        _insert_tx("deposit", "1000", updated_at=100, status="failed")
        _insert_tx("withdraw", "500", updated_at=150, status="pending")

        assert principal_series(read_user_earn_cashflows(USER)) == {}
