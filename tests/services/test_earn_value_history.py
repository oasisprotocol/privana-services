from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from src.clients.coingecko import PricePoint
from src.core.db import db_write, get_db
from src.services.earn.value_history import (
    EarnCashflow,
    _price_e8_at,
    earn_value_series,
    read_user_earn_cashflows,
)
from src.services.price_history import store_points

USER = "0x1111111111111111111111111111111111111111"
POOL_A = "0xaaaa000000000000000000000000000000000000000000000000000000000001"
POOL_B = "0xbbbb000000000000000000000000000000000000000000000000000000000002"
TOKEN_USDC = "0x" + "aa" * 32
TOKEN_WETH = "0x" + "bb" * 32

JUN15 = 1781481600  # 2026-06-15 00:00:00 UTC
DAY = 86400

_rows_inserted = 0


def insert_earn_tx(
    *,
    operation="deposit",
    pool_id=POOL_A,
    user_address=USER,
    token_id=TOKEN_USDC,
    amount="1000000",
    status="completed",
    updated_at=JUN15,
):
    global _rows_inserted
    _rows_inserted += 1
    db_write(
        get_db(),
        """INSERT INTO earn_transactions
           (id, operation, pool_id, user_address, token_id, amount,
            signer_address, nonce, signature, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"tx-{_rows_inserted}", operation, pool_id, user_address.lower(), token_id,
            amount, user_address.lower(), _rows_inserted, "0xsig", status,
            updated_at, updated_at,
        ),
    )


class FlatResolver:
    async def growth_factor(self, pool_id, from_ts, to_ts):
        return Decimal(1)


class DoublingResolver:
    """2x per whole day elapsed. Multiplicative over adjacent windows, so the
    stepwise evaluation must reproduce the direct identity exactly."""

    async def growth_factor(self, pool_id, from_ts, to_ts):
        return Decimal(2) ** ((to_ts - from_ts) // DAY)


class TestReadUserEarnCashflows:
    def test_signs_and_order(self):
        insert_earn_tx(operation="withdraw", amount="300", updated_at=JUN15 + DAY)
        insert_earn_tx(operation="deposit", amount="1000", updated_at=JUN15)

        flows = read_user_earn_cashflows(USER)

        assert [f.signed_amount for f in flows] == [Decimal(1000), Decimal(-300)]
        assert [f.timestamp for f in flows] == [JUN15, JUN15 + DAY]

    def test_undeployed_deposit_counts_but_unsettled_rows_do_not(self):
        insert_earn_tx(status="undeployed", amount="500")
        insert_earn_tx(status="pending", amount="111")
        insert_earn_tx(status="failed", amount="222")
        insert_earn_tx(operation="withdraw", status="undeployed", amount="333")

        flows = read_user_earn_cashflows(USER)

        assert [f.signed_amount for f in flows] == [Decimal(500)]

    def test_matches_user_case_insensitively(self):
        insert_earn_tx()

        assert read_user_earn_cashflows(USER.upper().replace("0X", "0x")) != []

    def test_other_users_rows_are_invisible(self):
        insert_earn_tx(user_address="0x2222222222222222222222222222222222222222")

        assert read_user_earn_cashflows(USER) == []


@patch("src.services.earn.value_history._token_decimals", new=AsyncMock(return_value=None))
class TestEarnValueSeries:
    async def test_flat_rate_reports_principal(self):
        insert_earn_tx(amount="1000000", updated_at=JUN15)

        points = await earn_value_series(USER, [JUN15, JUN15 + DAY], resolver=FlatResolver())

        assert [(p.timestamp, p.earn_value_base) for p in points] == [
            (JUN15, 1000000),
            (JUN15 + DAY, 1000000),
        ]

    async def test_growth_compounds_from_event_time(self):
        insert_earn_tx(amount="1000", updated_at=JUN15)

        points = await earn_value_series(
            USER, [JUN15, JUN15 + DAY, JUN15 + 2 * DAY], resolver=DoublingResolver()
        )

        assert [p.earn_value_base for p in points] == [1000, 2000, 4000]

    async def test_event_before_grid_start_is_included(self):
        insert_earn_tx(amount="1000", updated_at=JUN15 - 2 * DAY)

        points = await earn_value_series(USER, [JUN15], resolver=DoublingResolver())

        assert [p.earn_value_base for p in points] == [4000]

    async def test_withdraw_removes_grown_value(self):
        # Deposit 1000, worth 2000 a day later; withdraw all 2000. The negative
        # cashflow grows alongside what remains, so the value is exactly zero
        # from the withdrawal on.
        insert_earn_tx(amount="1000", updated_at=JUN15)
        insert_earn_tx(operation="withdraw", amount="2000", updated_at=JUN15 + DAY)

        points = await earn_value_series(
            USER, [JUN15, JUN15 + DAY, JUN15 + 2 * DAY], resolver=DoublingResolver()
        )

        assert [p.earn_value_base for p in points] == [1000, 0, 0]

    async def test_full_exit_residual_is_clamped_to_zero(self):
        # The contract paid better than the modeled rate: the user withdrew
        # 2100 while the model says the position was only worth 2000. The
        # identity would go negative; the report clamps to zero.
        insert_earn_tx(amount="1000", updated_at=JUN15)
        insert_earn_tx(operation="withdraw", amount="2100", updated_at=JUN15 + DAY)

        points = await earn_value_series(
            USER, [JUN15 + DAY, JUN15 + 2 * DAY], resolver=DoublingResolver()
        )

        assert [p.earn_value_base for p in points] == [0, 0]

    async def test_stepwise_matches_direct_identity(self):
        insert_earn_tx(amount="1000", updated_at=JUN15)
        insert_earn_tx(amount="500", updated_at=JUN15 + DAY)
        insert_earn_tx(operation="withdraw", amount="800", updated_at=JUN15 + 2 * DAY)

        t = JUN15 + 3 * DAY
        points = await earn_value_series(USER, [t], resolver=DoublingResolver())

        # 1000*2^3 + 500*2^2 - 800*2^1 evaluated directly.
        assert points[0].earn_value_base == 8000 + 2000 - 1600

    async def test_pools_aggregate_per_token(self):
        insert_earn_tx(pool_id=POOL_A, token_id=TOKEN_USDC, amount="1000")
        insert_earn_tx(pool_id=POOL_B, token_id=TOKEN_USDC, amount="200")
        insert_earn_tx(pool_id=POOL_B, token_id=TOKEN_WETH, amount="7")

        points = await earn_value_series(USER, [JUN15], resolver=FlatResolver())

        by_token = {p.token_id: p.earn_value_base for p in points}
        assert by_token == {TOKEN_USDC: 1200, TOKEN_WETH: 7}

    async def test_grid_is_deduplicated_and_sorted(self):
        insert_earn_tx(amount="1000")

        points = await earn_value_series(
            USER, [JUN15 + DAY, JUN15, JUN15 + DAY], resolver=FlatResolver()
        )

        assert [p.timestamp for p in points] == [JUN15, JUN15 + DAY]

    async def test_base_value_rounds_half_up(self):
        class HalfGrowthResolver:
            async def growth_factor(self, pool_id, from_ts, to_ts):
                return Decimal("1.5") ** ((to_ts - from_ts) // DAY)

        insert_earn_tx(amount="3", updated_at=JUN15)

        points = await earn_value_series(USER, [JUN15 + DAY], resolver=HalfGrowthResolver())

        assert points[0].earn_value_base == 5  # 4.5 rounds half-up

    async def test_no_flows_yields_empty_series(self):
        assert await earn_value_series(USER, [JUN15], resolver=FlatResolver()) == []

    async def test_no_timestamps_yields_empty_series(self):
        insert_earn_tx()

        assert await earn_value_series(USER, [], resolver=FlatResolver()) == []

    async def test_fiat_is_none_without_price_or_decimals(self):
        insert_earn_tx(amount="1000")

        points = await earn_value_series(USER, [JUN15], resolver=FlatResolver())

        assert points[0].earn_value_fiat is None


def _settings():
    settings = MagicMock()
    settings.coingecko_token_ids = f'{{"{TOKEN_USDC}": "usd-coin"}}'
    return settings


class TestFiatConversion:

    async def test_converts_with_price_step_function(self):
        insert_earn_tx(amount="1500000", updated_at=JUN15)  # 1.5 USDC at 6 decimals
        store_points("usd-coin", [PricePoint(timestamp=JUN15, price_e8=2 * 10**8)])

        with (
            patch(
                "src.services.earn.value_history.load_settings",
                return_value=_settings(),
            ),
            patch(
                "src.services.earn.value_history._token_decimals",
                new=AsyncMock(return_value=6),
            ),
        ):
            points = await earn_value_series(
                USER, [JUN15, JUN15 + DAY], resolver=FlatResolver()
            )

        assert [p.earn_value_fiat for p in points] == [Decimal(3), Decimal(3)]

    async def test_uses_last_price_at_or_before_each_point(self):
        insert_earn_tx(amount="1000000", updated_at=JUN15 - DAY)
        store_points(
            "usd-coin",
            [
                PricePoint(timestamp=JUN15, price_e8=1 * 10**8),
                PricePoint(timestamp=JUN15 + DAY, price_e8=3 * 10**8),
            ],
        )

        with (
            patch(
                "src.services.earn.value_history.load_settings",
                return_value=_settings(),
            ),
            patch(
                "src.services.earn.value_history._token_decimals",
                new=AsyncMock(return_value=6),
            ),
        ):
            points = await earn_value_series(
                # JUN15 - DAY predates the series: flat backward extrapolation.
                USER, [JUN15 - DAY, JUN15, JUN15 + 2 * DAY], resolver=FlatResolver()
            )

        assert [p.earn_value_fiat for p in points] == [Decimal(1), Decimal(1), Decimal(3)]

    async def test_decimals_failure_degrades_to_none_fiat(self):
        insert_earn_tx(amount="1000000")
        store_points("usd-coin", [PricePoint(timestamp=JUN15, price_e8=10**8)])

        with (
            patch(
                "src.services.earn.value_history.load_settings",
                return_value=_settings(),
            ),
            patch(
                "src.services.earn.value_history._token_decimals",
                new=AsyncMock(return_value=None),
            ),
        ):
            points = await earn_value_series(USER, [JUN15], resolver=FlatResolver())

        assert points[0].earn_value_fiat is None
        assert points[0].earn_value_base == 1000000


class TestPriceAt:
    def test_empty_series_is_none(self):
        assert _price_e8_at([], JUN15) is None

    def test_step_function_and_flat_backward_extrapolation(self):
        points = [
            PricePoint(timestamp=JUN15, price_e8=100),
            PricePoint(timestamp=JUN15 + DAY, price_e8=200),
        ]

        assert _price_e8_at(points, JUN15 - DAY) == Decimal(100)
        assert _price_e8_at(points, JUN15 + DAY // 2) == Decimal(100)
        assert _price_e8_at(points, JUN15 + 2 * DAY) == Decimal(200)


class TestEarnCashflowShape:
    def test_dataclass_is_frozen(self):
        flow = EarnCashflow(
            timestamp=JUN15, pool_id=POOL_A, token_id=TOKEN_USDC, signed_amount=Decimal(1)
        )
        try:
            flow.timestamp = 0  # type: ignore[misc]
        except Exception:
            return
        raise AssertionError("EarnCashflow should be immutable")
