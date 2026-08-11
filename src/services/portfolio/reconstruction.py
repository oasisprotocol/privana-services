import logging
from dataclasses import dataclass

from src.models.common import HistoryEntry

logger = logging.getLogger(__name__)

# Effect of each history kind on the (available, locked) buckets, as +/-1
# multipliers of the entry amount. Semantics agreed for the portfolio charts:
#
# - deposit / withdraw and transferBalance* move spendable balance only.
# - createLock moves available into the lock; unlockLock moves it back.
# - modifyLock is a lock top-up funded from available (same shape as
#   createLock on an existing lock).
# - transferFromLockOut debits the *locked* bucket: the counterparty is paid
#   out of the lock, available never sees the funds.
# - transferFromLockIn credits available: incoming funds are spendable
#   regardless of which bucket they left on the sender's side.
_KIND_EFFECTS: dict[str, tuple[int, int]] = {
    "deposit": (1, 0),
    "withdraw": (-1, 0),
    "transferBalanceIn": (1, 0),
    "transferBalanceOut": (-1, 0),
    "transferFromLockIn": (1, 0),
    "transferFromLockOut": (0, -1),
    "createLock": (-1, 1),
    "modifyLock": (-1, 1),
    "unlockLock": (1, -1),
}


@dataclass(frozen=True)
class BucketPoint:
    timestamp: int
    available: int
    locked: int


def replay_history(entries: list[HistoryEntry]) -> dict[str, list[BucketPoint]]:
    """Replay a user's history into per-token available/locked change-points.

    Pure function: starts every token at (0, 0) and applies entries oldest
    first, emitting one point per distinct timestamp (all events sharing a
    timestamp collapse into the final state at that instant). Balances can go
    negative when the history is truncated — the accounting service only
    records from its own deployment forward — and that is preserved rather
    than clamped so downstream valuation can decide how to baseline.

    Entries with an unhandled kind or without token_id/amount are skipped;
    they carry no balance effect that we can attribute.
    """
    by_token: dict[str, list[HistoryEntry]] = {}
    for entry in entries:
        effect = _KIND_EFFECTS.get(entry.kind)
        if effect is None:
            logger.warning("Skipping history entry with unhandled kind=%s", entry.kind)
            continue
        if entry.token_id is None or entry.amount is None:
            logger.warning("Skipping %s entry without token_id/amount", entry.kind)
            continue
        by_token.setdefault(entry.token_id, []).append(entry)

    series: dict[str, list[BucketPoint]] = {}
    for token_id, token_entries in by_token.items():
        token_entries.sort(key=lambda e: e.timestamp)
        points: list[BucketPoint] = []
        available = 0
        locked = 0
        for entry in token_entries:
            avail_mult, locked_mult = _KIND_EFFECTS[entry.kind]
            amount = int(entry.amount)
            available += avail_mult * amount
            locked += locked_mult * amount
            point = BucketPoint(timestamp=entry.timestamp, available=available, locked=locked)
            if points and points[-1].timestamp == entry.timestamp:
                points[-1] = point
            else:
                points.append(point)
        series[token_id] = points
    return series
