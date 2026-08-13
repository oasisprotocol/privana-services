import logging
from bisect import bisect_right
from dataclasses import dataclass

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

    Balances and prices both behave this way between observations: the value
    at t is the most recent point at or before t, and 0 before the first
    point — a balance does not exist before its first event, and a token
    contributes no value before its first price sample.
    """

    timestamps: tuple[int, ...]
    values: tuple[int, ...]

    @classmethod
    def from_points(cls, points: list[tuple[int, int]]) -> "StepSeries":
        ordered = sorted(points, key=lambda p: p[0])
        return cls(
            timestamps=tuple(p[0] for p in ordered),
            values=tuple(p[1] for p in ordered),
        )

    def value_at(self, ts: int) -> int:
        index = bisect_right(self.timestamps, ts)
        if index == 0:
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
    last known balance and price hold. Tokens without a price series or
    decimals are skipped loudly rather than valued wrong — a hole in config
    should surface in logs, not in a silently lower total. Negative balances
    from truncated histories value negative by design.
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
                StepSeries.from_points([(p.timestamp, p.price_e8) for p in prices]),
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
