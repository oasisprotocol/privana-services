from src.clients.coingecko import PricePoint
from src.services.portfolio.reconstruction import BucketPoint
from src.services.portfolio.valuation import (
    BucketValuePoint,
    StepSeries,
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

    def test_balance_before_first_price_sample_values_zero(self):
        buckets = {USDC: [BucketPoint(timestamp=T0, available=5_000_000, locked=0)]}
        prices = {USDC: [PricePoint(timestamp=T1, price_e8=100_000_000)]}

        series = value_buckets(buckets, prices, {USDC: 6}, [T0, T1])

        assert series[0].available_e8 == 0
        assert series[1].available_e8 == 500_000_000

    def test_empty_grid_yields_empty_series(self):
        assert value_buckets({}, {}, {}, []) == []
