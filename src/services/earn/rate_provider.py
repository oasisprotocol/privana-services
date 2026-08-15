from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Optional

from src.services.earn.strategies.base import ApyPoint
from src.services.pool_rate_history import PoolRatePoint, read_points

logger = logging.getLogger(__name__)

DAY_SEC = 86400
YEAR_SEC = 365 * DAY_SEC
BPS = Decimal(10_000)

# EarnManager share math carries an ERC4626 virtual offset: one share is worth
# (total_assets + 1) / (total_shares + VIRTUAL_SHARES). growth_factor divides two
# such rates so the offsets nearly cancel, but they still matter at low supply,
# so the per-share rate is reconstructed exactly rather than as total_assets/
# total_shares.
VIRTUAL_SHARES = 10**6
VIRTUAL_ASSETS = 1

ONE = Decimal(1)


class InsufficientRateHistory(Exception):
    """A sampled series does not cover the requested window.

    Raised by SampledRate when the pool_rate_history table has no point at or
    before from_ts (or to_ts): the tier-a rate is unknowable for that span, and
    the resolver falls back to the tier-b APY-derived provider rather than
    inventing a number.
    """


class EarnRateProvider(ABC):
    """Answers 'what did one unit of value in this pool become between two times'.

    ``growth_factor(pool, a, b)`` is ``value(b) / value(a)`` for funds left
    untouched in the pool over ``[a, b]``; 1.0 means flat. This is the single
    seam the earn-value history (S4) and the portfolio valuation (S5) both build
    on, so a position is valued exactly one way regardless of which chart renders
    it. Providers are stateless — they read a series and divide.
    """

    @abstractmethod
    async def growth_factor(self, pool_id: str, from_ts: int, to_ts: int) -> Decimal:
        ...


def _rate_at(points: list[PoolRatePoint], ts: int) -> Optional[Decimal]:
    """Per-share rate in effect at ``ts``: the value of the last sample taken at
    or before ``ts`` (a step function — a rate holds until the next sample).
    None when the series starts after ``ts`` and the rate is thus unknown.

    ``points`` is oldest-first (read_points guarantees ORDER BY timestamp ASC).
    """
    chosen: Optional[PoolRatePoint] = None
    for point in points:
        if point.timestamp > ts:
            break
        chosen = point
    if chosen is None:
        return None
    numerator = Decimal(chosen.total_assets) + VIRTUAL_ASSETS
    denominator = Decimal(chosen.total_shares) + VIRTUAL_SHARES
    return numerator / denominator


class SampledRate(EarnRateProvider):
    """Tier-a: growth from the sampled on-chain rate (pool_rate_history / S1).

    Exact where the sampler has run; blind before its first sample. Since the
    series is unbackfillable (Sapphire is non-archive), a window that predates it
    can never be filled, so we raise InsufficientRateHistory and let the resolver
    fall back rather than extrapolate a rate we never observed.
    """

    async def growth_factor(self, pool_id: str, from_ts: int, to_ts: int) -> Decimal:
        if to_ts <= from_ts:
            return ONE
        # A local indexed SQLite read is microseconds; no to_thread hop, matching
        # how the rest of the service treats get_db() reads.
        points = read_points(pool_id)
        rate_from = _rate_at(points, from_ts)
        rate_to = _rate_at(points, to_ts)
        if rate_from is None or rate_to is None or rate_from == 0:
            raise InsufficientRateHistory(
                f"pool_rate_history does not cover [{from_ts}, {to_ts}] for pool {pool_id}"
            )
        return rate_to / rate_from


class DefiLlamaApyRate(EarnRateProvider):
    """Tier-b: growth compounded from the strategy's DefiLlama APY history.

    Backfillable (DefiLlama carries a long daily series), so this is the v1
    default for every pool while the sampled series is still empty. The APY is
    piecewise-constant between points and extrapolated flat past both ends —
    rates are sticky, and a chart needs an answer for the whole window, not a
    gap. An empty history yields 1.0 (flat / principal-only): the honest answer
    when there is no rate source at all.
    """

    def __init__(self, service=None) -> None:
        self._service = service

    def _get_service(self):
        if self._service is None:
            from src.services.earn.vault_service import get_vault_service

            self._service = get_vault_service()
        return self._service

    async def growth_factor(self, pool_id: str, from_ts: int, to_ts: int) -> Decimal:
        if to_ts <= from_ts:
            return ONE
        history = await self._get_service().strategy_apy_history_safe(pool_id)
        return _compound_apy(history, from_ts, to_ts)


def _compound_apy(history: list[ApyPoint], from_ts: int, to_ts: int) -> Decimal:
    """Compound a piecewise-constant APY curve over ``[from_ts, to_ts]``.

    Each segment contributes ``(1 + apy) ** (dt / year)``; the product is
    accumulated in log-space so a long window with many points stays numerically
    stable. The APY in force on a segment is the last point at or before the
    segment start, extrapolated from the first point for time before the series.
    """
    if to_ts <= from_ts or not history:
        return ONE

    ordered = sorted(history, key=lambda p: p.timestamp)
    boundaries = [from_ts]
    for point in ordered:
        if from_ts < point.timestamp < to_ts:
            boundaries.append(point.timestamp)
    boundaries.append(to_ts)

    log_growth = 0.0
    for start, end in zip(boundaries, boundaries[1:]):
        apy_bps = _apy_at(ordered, start)
        rate = 1.0 + float(apy_bps) / float(BPS)
        if rate <= 0.0:
            continue
        log_growth += math.log(rate) * ((end - start) / YEAR_SEC)

    return Decimal(str(math.exp(log_growth)))


def _apy_at(ordered: list[ApyPoint], ts: int) -> int:
    """APY (bps) in force at ``ts``: the last point at or before it, or the first
    point when ``ts`` predates the series (flat backward extrapolation)."""
    chosen = ordered[0]
    for point in ordered:
        if point.timestamp > ts:
            break
        chosen = point
    return chosen.apy_bps


class EarnRateResolver:
    """Routes each pool to a rate provider.

    v1: every pool uses tier-b (DefiLlama), because pool_rate_history starts
    empty and fills only forward. A pool is opted into tier-a by adding its id to
    ``sampled_pools`` once its sampled series has depth — a one-line change here,
    with no downstream rewrite, because both tiers satisfy the same
    ``growth_factor`` contract. Within an opted-in pool, a window the sampler
    doesn't yet cover still falls back to tier-b, so opting in early is safe.
    """

    def __init__(
        self,
        tier_a: Optional[EarnRateProvider] = None,
        tier_b: Optional[EarnRateProvider] = None,
        sampled_pools: Optional[set[str]] = None,
    ) -> None:
        self._tier_a = tier_a or SampledRate()
        self._tier_b = tier_b or DefiLlamaApyRate()
        self._sampled_pools = {self._normalize(p) for p in (sampled_pools or set())}

    async def growth_factor(self, pool_id: str, from_ts: int, to_ts: int) -> Decimal:
        if self._normalize(pool_id) in self._sampled_pools:
            try:
                return await self._tier_a.growth_factor(pool_id, from_ts, to_ts)
            except InsufficientRateHistory:
                logger.debug(
                    "EarnRateResolver: pool=%s window [%d, %d] not sampled yet; using tier-b",
                    pool_id, from_ts, to_ts,
                )
        return await self._tier_b.growth_factor(pool_id, from_ts, to_ts)

    @staticmethod
    def _normalize(pool_id: str) -> str:
        return pool_id.removeprefix("0x").lower()


_resolver_instance: Optional[EarnRateResolver] = None


def get_earn_rate_resolver() -> EarnRateResolver:
    global _resolver_instance
    if _resolver_instance is None:
        _resolver_instance = EarnRateResolver()
    return _resolver_instance


def reset_earn_rate_resolver() -> None:
    """Test hook. Clears the module-level singleton so each test gets a fresh
    resolver."""
    global _resolver_instance
    _resolver_instance = None
