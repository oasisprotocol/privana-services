from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.services.earn.rate_provider import (
    DefiLlamaApyRate,
    EarnRateResolver,
    InsufficientRateHistory,
    SampledRate,
    _compound_apy,
)
from src.services.earn.strategies.base import ApyPoint
from src.services.pool_rate_history import PoolRatePoint, store_point

POOL_A = "0xaaaa000000000000000000000000000000000000000000000000000000000001"

JUN15 = 1781481600  # 2026-06-15 00:00:00 UTC
DAY = 86400
YEAR = 365 * DAY


class TestSampledRate:
    async def test_growth_is_ratio_of_per_share_rates(self):
        # Rate doubles: (100+1)/(0+1e6) -> (200+1)/(0+1e6). Shares held flat so
        # the offsets are identical on both ends and the factor is ~2.
        store_point(POOL_A, PoolRatePoint(JUN15, "100000000", "100000000"))
        store_point(POOL_A, PoolRatePoint(JUN15 + DAY, "200000000", "100000000"))

        factor = await SampledRate().growth_factor(POOL_A, JUN15, JUN15 + DAY)

        assert factor == pytest.approx(Decimal(2), rel=Decimal("1e-6"))

    async def test_uses_last_sample_at_or_before_each_end(self):
        # Step function: the rate at (JUN15 + 2*DAY) is the JUN15+DAY sample,
        # since no sample falls between them.
        store_point(POOL_A, PoolRatePoint(JUN15, "100000000", "100000000"))
        store_point(POOL_A, PoolRatePoint(JUN15 + DAY, "300000000", "100000000"))

        factor = await SampledRate().growth_factor(POOL_A, JUN15, JUN15 + 2 * DAY)

        assert factor == pytest.approx(Decimal(3), rel=Decimal("1e-6"))

    async def test_window_before_series_raises(self):
        store_point(POOL_A, PoolRatePoint(JUN15, "100000000", "100000000"))

        with pytest.raises(InsufficientRateHistory):
            await SampledRate().growth_factor(POOL_A, JUN15 - DAY, JUN15)

    async def test_no_samples_raises(self):
        with pytest.raises(InsufficientRateHistory):
            await SampledRate().growth_factor(POOL_A, JUN15, JUN15 + DAY)

    async def test_zero_length_window_is_one(self):
        # Returns before touching the series, so it holds even with no samples.
        assert await SampledRate().growth_factor(POOL_A, JUN15, JUN15) == Decimal(1)

    async def test_reversed_window_is_one(self):
        assert await SampledRate().growth_factor(POOL_A, JUN15 + DAY, JUN15) == Decimal(1)


class TestCompoundApy:
    def test_empty_history_is_flat(self):
        assert _compound_apy([], JUN15, JUN15 + YEAR) == Decimal(1)

    def test_constant_apy_compounds_over_a_year(self):
        # A flat 5% APY over exactly one year grows by ~1.05.
        history = [ApyPoint(timestamp=JUN15 - DAY, apy_bps=500)]

        factor = _compound_apy(history, JUN15, JUN15 + YEAR)

        assert float(factor) == pytest.approx(1.05, rel=1e-6)

    def test_half_year_is_half_the_log_growth(self):
        history = [ApyPoint(timestamp=JUN15 - DAY, apy_bps=500)]

        factor = _compound_apy(history, JUN15, JUN15 + YEAR // 2)

        assert float(factor) == pytest.approx(1.05**0.5, rel=1e-6)

    def test_rate_change_midway_uses_each_segment(self):
        # 10% for the first year, 0% for the second: total ~1.10 over two years.
        history = [
            ApyPoint(timestamp=JUN15, apy_bps=1000),
            ApyPoint(timestamp=JUN15 + YEAR, apy_bps=0),
        ]

        factor = _compound_apy(history, JUN15, JUN15 + 2 * YEAR)

        assert float(factor) == pytest.approx(1.10, rel=1e-6)

    def test_apy_before_series_extrapolates_from_first_point(self):
        # Window opens a year before the only point; its rate applies backward.
        history = [ApyPoint(timestamp=JUN15, apy_bps=500)]

        factor = _compound_apy(history, JUN15 - YEAR, JUN15)

        assert float(factor) == pytest.approx(1.05, rel=1e-6)


class TestResolver:
    async def test_defaults_to_tier_b(self):
        tier_a = AsyncMock()
        tier_b = AsyncMock()
        tier_b.growth_factor.return_value = Decimal("1.07")

        resolver = EarnRateResolver(tier_a=tier_a, tier_b=tier_b)
        result = await resolver.growth_factor(POOL_A, JUN15, JUN15 + DAY)

        assert result == Decimal("1.07")
        tier_a.growth_factor.assert_not_called()
        tier_b.growth_factor.assert_awaited_once()

    async def test_opted_in_pool_uses_tier_a(self):
        tier_a = AsyncMock()
        tier_a.growth_factor.return_value = Decimal("1.03")
        tier_b = AsyncMock()

        resolver = EarnRateResolver(tier_a=tier_a, tier_b=tier_b, sampled_pools={POOL_A})
        result = await resolver.growth_factor(POOL_A, JUN15, JUN15 + DAY)

        assert result == Decimal("1.03")
        tier_b.growth_factor.assert_not_called()

    async def test_opted_in_pool_falls_back_when_window_unsampled(self):
        tier_a = AsyncMock()
        tier_a.growth_factor.side_effect = InsufficientRateHistory("no coverage")
        tier_b = AsyncMock()
        tier_b.growth_factor.return_value = Decimal("1.07")

        resolver = EarnRateResolver(tier_a=tier_a, tier_b=tier_b, sampled_pools={POOL_A})
        result = await resolver.growth_factor(POOL_A, JUN15, JUN15 + DAY)

        assert result == Decimal("1.07")
        tier_a.growth_factor.assert_awaited_once()
        tier_b.growth_factor.assert_awaited_once()

    async def test_pool_id_matching_ignores_prefix_and_case(self):
        tier_a = AsyncMock()
        tier_a.growth_factor.return_value = Decimal("1.03")
        tier_b = AsyncMock()

        # Opted in with a bare, upper-cased id; queried with the 0x-prefixed form.
        resolver = EarnRateResolver(
            tier_a=tier_a, tier_b=tier_b, sampled_pools={POOL_A.removeprefix("0x").upper()}
        )
        await resolver.growth_factor(POOL_A, JUN15, JUN15 + DAY)

        tier_a.growth_factor.assert_awaited_once()

    async def test_tier_b_from_apy_history(self):
        # End to end through the real tier-b: a stubbed strategy history of a flat
        # 5% APY compounds to ~1.05 over a year.
        service = AsyncMock()
        service.strategy_apy_history_safe.return_value = [
            ApyPoint(timestamp=JUN15 - DAY, apy_bps=500)
        ]
        resolver = EarnRateResolver(tier_b=DefiLlamaApyRate(service=service))

        factor = await resolver.growth_factor(POOL_A, JUN15, JUN15 + YEAR)

        assert float(factor) == pytest.approx(1.05, rel=1e-6)
