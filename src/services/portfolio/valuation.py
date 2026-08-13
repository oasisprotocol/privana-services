from bisect import bisect_right
from dataclasses import dataclass

from src.services.price_history import SAMPLE_INTERVAL_SEC


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
