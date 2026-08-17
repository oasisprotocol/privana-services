from decimal import Decimal

from src.clients.coingecko import PricePoint
from src.services.earn.value_history import EarnValuePoint
from src.services.portfolio.reconstruction import BucketPoint
from src.services.portfolio.valuation import (
    BucketValuePoint,
    EarnSeriesValues,
    PortfolioPoint,
    StepSeries,
    compose_portfolio,
    sample_grid,
    value_buckets,
)
from src.services.price_history import SAMPLE_INTERVAL_SEC

USDC = "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514"
WETH = "0x335b5cccd1e63b2fe79863a0db73fce430e4e66902e2b78424f8662621e29fb7"

T0 = SAMPLE_INTERVAL_SEC * 10
T1 = SAMPLE_INTERVAL_SEC * 11
T2 = SAMPLE_INTERVAL_SEC * 12


class TestSampleGrid:
    def test_grid_is_snapped_to_the_sampler_interval(self):
        grid = sample_grid(SAMPLE_INTERVAL_SEC + 5, 3 * SAMPLE_INTERVAL_SEC + 5)

        assert grid == [
            SAMPLE_INTERVAL_SEC,
            2 * SAMPLE_INTERVAL_SEC,
            3 * SAMPLE_INTERVAL_SEC,
        ]

    def test_aligned_bounds_are_included(self):
        grid = sample_grid(SAMPLE_INTERVAL_SEC, 2 * SAMPLE_INTERVAL_SEC)

        assert grid == [SAMPLE_INTERVAL_SEC, 2 * SAMPLE_INTERVAL_SEC]

    def test_range_within_one_slot_yields_a_single_point(self):
        grid = sample_grid(SAMPLE_INTERVAL_SEC + 1, SAMPLE_INTERVAL_SEC + 2)

        assert grid == [SAMPLE_INTERVAL_SEC]

    def test_inverted_range_is_empty(self):
        assert sample_grid(100, 50) == []


class TestStepSeries:
    def test_value_holds_until_the_next_change_point(self):
        series = StepSeries.from_points([(100, 5), (200, 8)])

        assert series.value_at(100) == 5
        assert series.value_at(150) == 5
        assert series.value_at(200) == 8
        assert series.value_at(9999) == 8

    def test_value_before_the_first_point_is_zero(self):
        series = StepSeries.from_points([(100, 5)])

        assert series.value_at(99) == 0

    def test_extended_series_holds_its_first_value_backwards(self):
        series = StepSeries.from_points([(100, 5), (200, 8)], extend_backward=True)

        assert series.value_at(0) == 5
        assert series.value_at(150) == 5

    def test_empty_extended_series_is_still_zero(self):
        series = StepSeries.from_points([], extend_backward=True)

        assert series.value_at(0) == 0

    def test_points_are_sorted_on_construction(self):
        series = StepSeries.from_points([(200, 8), (100, 5)])

        assert series.value_at(150) == 5

    def test_empty_series_is_always_zero(self):
        series = StepSeries.from_points([])

        assert series.value_at(0) == 0
        assert series.value_at(10**12) == 0


class TestValueBuckets:
    def test_values_balance_times_price_normalised_by_decimals(self):
        buckets = {USDC: [BucketPoint(timestamp=T0, available=5_000_000, locked=0)]}
        prices = {USDC: [PricePoint(timestamp=T0, price_e8=100_000_000)]}

        series = value_buckets(buckets, prices, {USDC: 6}, [T0])

        assert series == [
            BucketValuePoint(timestamp=T0, available_e8=500_000_000, locked_e8=0)
        ]

    def test_balances_and_prices_step_between_observations(self):
        buckets = {
            USDC: [
                BucketPoint(timestamp=T0, available=5_000_000, locked=0),
                BucketPoint(timestamp=T2, available=2_000_000, locked=3_000_000),
            ]
        }
        prices = {
            USDC: [
                PricePoint(timestamp=T0, price_e8=100_000_000),
                PricePoint(timestamp=T1, price_e8=200_000_000),
            ]
        }

        series = value_buckets(buckets, prices, {USDC: 6}, [T0, T1, T2])

        assert series == [
            BucketValuePoint(timestamp=T0, available_e8=500_000_000, locked_e8=0),
            BucketValuePoint(timestamp=T1, available_e8=1_000_000_000, locked_e8=0),
            BucketValuePoint(timestamp=T2, available_e8=400_000_000, locked_e8=600_000_000),
        ]

    def test_tokens_sum_into_one_series(self):
        buckets = {
            USDC: [BucketPoint(timestamp=T0, available=5_000_000, locked=0)],
            WETH: [BucketPoint(timestamp=T0, available=2 * 10**18, locked=0)],
        }
        prices = {
            USDC: [PricePoint(timestamp=T0, price_e8=100_000_000)],
            WETH: [PricePoint(timestamp=T0, price_e8=3_000 * 10**8)],
        }

        series = value_buckets(buckets, prices, {USDC: 6, WETH: 18}, [T0])

        assert series[0].available_e8 == 500_000_000 + 6_000 * 10**8

    def test_token_without_price_series_is_skipped(self):
        buckets = {
            USDC: [BucketPoint(timestamp=T0, available=5_000_000, locked=0)],
            WETH: [BucketPoint(timestamp=T0, available=10**18, locked=0)],
        }
        prices = {USDC: [PricePoint(timestamp=T0, price_e8=100_000_000)]}

        series = value_buckets(buckets, prices, {USDC: 6, WETH: 18}, [T0])

        assert series[0].available_e8 == 500_000_000

    def test_token_without_decimals_is_skipped(self):
        buckets = {USDC: [BucketPoint(timestamp=T0, available=5_000_000, locked=0)]}
        prices = {USDC: [PricePoint(timestamp=T0, price_e8=100_000_000)]}

        series = value_buckets(buckets, prices, {}, [T0])

        assert series == [BucketValuePoint(timestamp=T0, available_e8=0, locked_e8=0)]

    def test_balance_before_first_price_sample_uses_the_first_price(self):
        buckets = {USDC: [BucketPoint(timestamp=T0, available=5_000_000, locked=0)]}
        prices = {USDC: [PricePoint(timestamp=T1, price_e8=100_000_000)]}

        series = value_buckets(buckets, prices, {USDC: 6}, [T0, T1])

        assert series[0].available_e8 == 500_000_000
        assert series[1].available_e8 == 500_000_000

    def test_empty_grid_yields_empty_series(self):
        assert value_buckets({}, {}, {}, []) == []


class _FlatEarn:
    def __init__(self, value_e8):
        self._value_e8 = value_e8

    def earn_value_e8(self, timestamp):
        return self._value_e8


class TestComposePortfolio:
    def test_total_sums_all_three_components(self):
        bucket_values = [
            BucketValuePoint(timestamp=T0, available_e8=500, locked_e8=200),
        ]

        series = compose_portfolio(bucket_values, _FlatEarn(300))

        assert series == [
            PortfolioPoint(
                timestamp=T0, total_e8=1000, available_e8=500, locked_e8=200, earn_e8=300
            )
        ]

    def test_empty_earn_series_leaves_totals_to_the_buckets(self):
        bucket_values = [
            BucketValuePoint(timestamp=T0, available_e8=500, locked_e8=200),
            BucketValuePoint(timestamp=T1, available_e8=700, locked_e8=0),
        ]

        series = compose_portfolio(bucket_values, EarnSeriesValues({}))

        assert [p.total_e8 for p in series] == [700, 700]
        assert all(p.earn_e8 == 0 for p in series)

    def test_empty_bucket_series_yields_empty_portfolio(self):
        assert compose_portfolio([], EarnSeriesValues({})) == []


class TestEarnSeriesValues:
    def _series(self, monkeypatch, points):
        async def fake_series(user_address, timestamps):
            assert user_address == "0xuser"
            assert list(timestamps) == [T0, T1]
            return points

        monkeypatch.setattr(
            "src.services.portfolio.valuation.earn_value_series", fake_series
        )

    async def test_grid_values_aggregate_across_tokens(self, monkeypatch):
        self._series(
            monkeypatch,
            [
                EarnValuePoint(T0, USDC, 5_000_000, Decimal("5")),
                EarnValuePoint(T0, WETH, 10**17, Decimal("300")),
                EarnValuePoint(T1, USDC, 5_000_000, Decimal("5.5")),
            ],
        )

        earn = await EarnSeriesValues.load("0xuser", [T0, T1])

        assert earn.earn_value_e8(T0) == 305 * 10**8
        assert earn.earn_value_e8(T1) == 550_000_000

    async def test_unpriced_points_are_skipped_not_zeroed(self, monkeypatch, caplog):
        self._series(
            monkeypatch,
            [
                EarnValuePoint(T0, USDC, 5_000_000, Decimal("5")),
                EarnValuePoint(T0, WETH, 10**17, None),
            ],
        )

        earn = await EarnSeriesValues.load("0xuser", [T0, T1])

        assert earn.earn_value_e8(T0) == 500_000_000
        assert WETH in caplog.text

    async def test_zero_value_unpriced_points_do_not_warn(self, monkeypatch, caplog):
        self._series(monkeypatch, [EarnValuePoint(T0, WETH, 0, None)])

        earn = await EarnSeriesValues.load("0xuser", [T0, T1])

        assert earn.earn_value_e8(T0) == 0
        assert WETH not in caplog.text

    async def test_grid_timestamps_without_points_read_zero(self, monkeypatch):
        self._series(monkeypatch, [])

        earn = await EarnSeriesValues.load("0xuser", [T0, T1])

        assert earn.earn_value_e8(T0) == 0
        assert earn.earn_value_e8(T2) == 0

    async def test_fiat_values_round_half_up_to_e8(self, monkeypatch):
        self._series(
            monkeypatch,
            [EarnValuePoint(T0, USDC, 1, Decimal("0.000000005"))],
        )

        earn = await EarnSeriesValues.load("0xuser", [T0, T1])

        assert earn.earn_value_e8(T0) == 1
