import logging
from bisect import bisect_right
from dataclasses import dataclass
from typing import Protocol

from src.clients.coingecko import PricePoint
from src.services.portfolio.reconstruction import BucketPoint
from src.services.price_history import SAMPLE_INTERVAL_SEC

logger = logging.getLogger(__name__)


def sample_grid(start_ts: int, end_ts: int) -> list[int]:
    """Timestamps of the shared sampling grid covering [start_ts, end_ts].

    Snaps to the same interval the price and pool-rate samplers write on, so
    valuation always lands on timestamps where a sample can exist. The first
    point is the grid slot at or before start_ts; the last is the slot at or
    before end_ts.
    """
    if end_ts < start_ts:
        return []
    first = start_ts - (start_ts % SAMPLE_INTERVAL_SEC)
    last = end_ts - (end_ts % SAMPLE_INTERVAL_SEC)
    return list(range(first, last + 1, SAMPLE_INTERVAL_SEC))


@dataclass(frozen=True)
class StepSeries:
    """A right-continuous step function over (timestamp, value) change-points.

    Balances and prices both hold their last observed value between
    observations. Before the first point they differ: a balance does not
    exist before its first event, so it reads 0, while a price series is
    extrapolated flat from its first sample (extend_backward=True) — the same
    rule the earn value history applies, so both valuations price the time
    before the stored window identically.
    """

    timestamps: tuple[int, ...]
    values: tuple[int, ...]
    extend_backward: bool = False

    @classmethod
    def from_points(
        cls, points: list[tuple[int, int]], extend_backward: bool = False
    ) -> "StepSeries":
        ordered = sorted(points, key=lambda p: p[0])
        return cls(
            timestamps=tuple(p[0] for p in ordered),
            values=tuple(p[1] for p in ordered),
            extend_backward=extend_backward,
        )

    def value_at(self, ts: int) -> int:
        index = bisect_right(self.timestamps, ts)
        if index == 0:
            if self.extend_backward and self.values:
                return self.values[0]
            return 0
        return self.values[index - 1]


@dataclass(frozen=True)
class BucketValuePoint:
    timestamp: int
    available_e8: int
    locked_e8: int


def value_buckets(
    bucket_series: dict[str, list[BucketPoint]],
    price_series: dict[str, list[PricePoint]],
    token_decimals: dict[str, int],
    grid: list[int],
) -> list[BucketValuePoint]:
    """Value per-token bucket series in fiat across the sampling grid.

    At each grid timestamp every token contributes balance x price, with the
    balance normalised out of its raw units: value_e8 = amount * price_e8 //
    10^decimals. Both inputs are step functions, so between observations the
    last known balance and price hold; before the first stored price sample
    the series extends flat backwards, matching the earn value history.
    Tokens without a price series or decimals are skipped loudly rather than
    valued wrong — a hole in config should surface in logs, not in a silently
    lower total. Negative balances from truncated histories value negative by
    design.
    """
    valued_tokens = []
    for token_id, points in bucket_series.items():
        prices = price_series.get(token_id)
        decimals = token_decimals.get(token_id)
        if not prices or decimals is None:
            logger.warning(
                "Skipping token %s in valuation: missing %s",
                token_id,
                "price series" if not prices else "decimals",
            )
            continue
        valued_tokens.append(
            (
                StepSeries.from_points([(p.timestamp, p.available) for p in points]),
                StepSeries.from_points([(p.timestamp, p.locked) for p in points]),
                StepSeries.from_points(
                    [(p.timestamp, p.price_e8) for p in prices], extend_backward=True
                ),
                10**decimals,
            )
        )

    series = []
    for ts in grid:
        available_e8 = 0
        locked_e8 = 0
        for available, locked, price, scale in valued_tokens:
            price_e8 = price.value_at(ts)
            available_e8 += available.value_at(ts) * price_e8 // scale
            locked_e8 += locked.value_at(ts) * price_e8 // scale
        series.append(
            BucketValuePoint(timestamp=ts, available_e8=available_e8, locked_e8=locked_e8)
        )
    return series


class EarnValueProvider(Protocol):
    """Seam for the earn value series (plan step 4).

    The earn component is owned by the rate-provider work: shares(t) x
    rate(t) x price(t). Portfolio composition only needs a fiat value per
    grid timestamp, so the whole dependency is this one method.
    """

    def earn_value_e8(self, timestamp: int) -> int: ...


class ZeroEarnValue:
    """Stand-in until the earn value series lands; values every position at 0."""

    def earn_value_e8(self, timestamp: int) -> int:
        return 0


@dataclass(frozen=True)
class PortfolioPoint:
    timestamp: int
    total_e8: int
    available_e8: int
    locked_e8: int
    earn_e8: int


def compose_portfolio(
    bucket_values: list[BucketValuePoint],
    earn: EarnValueProvider,
) -> list[PortfolioPoint]:
    series = []
    for point in bucket_values:
        earn_e8 = earn.earn_value_e8(point.timestamp)
        series.append(
            PortfolioPoint(
                timestamp=point.timestamp,
                total_e8=point.available_e8 + point.locked_e8 + earn_e8,
                available_e8=point.available_e8,
                locked_e8=point.locked_e8,
                earn_e8=earn_e8,
            )
        )
    return series
