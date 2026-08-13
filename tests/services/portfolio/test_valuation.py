from src.services.portfolio.valuation import StepSeries, sample_grid
from src.services.price_history import SAMPLE_INTERVAL_SEC


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
