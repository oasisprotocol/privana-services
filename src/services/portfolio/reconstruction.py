import logging
from dataclasses import dataclass

from src.models.common import HistoryEntry

logger = logging.getLogger(__name__)

# Effect of each history kind on the (available, locked) buckets, as +/-1
# multipliers of the entry amount. Semantics agreed for the portfolio charts:
#
# - deposit and transferBalance* move spendable balance only.
# - createLock moves available into the lock; unlockLock moves it back.
# - modifyLock is a lock top-up funded from available (same shape as
#   createLock on an existing lock).
# - transferFromLockIn credits available: incoming funds are spendable
#   regardless of which bucket they left on the sender's side.
#
# withdraw and transferFromLockOut depend on who the counterparty is and are
# resolved per entry in _effect below.
_KIND_EFFECTS: dict[str, tuple[int, int]] = {
    "deposit": (1, 0),
    "transferBalanceIn": (1, 0),
    "transferBalanceOut": (-1, 0),
    "transferFromLockIn": (1, 0),
    "createLock": (-1, 1),
    "modifyLock": (-1, 1),
    "unlockLock": (1, -1),
}


def _effect(entry: HistoryEntry, user_address: str) -> tuple[int, int] | None:
    to_self = (entry.counterparty or "").lower() == user_address
    if entry.kind == "withdraw":
        # A lock payout also lands as kind=withdraw, distinguishable by the
        # counterparty: paying someone else comes out of the lock, while a
        # withdrawal to yourself (bridging out) debits available.
        return (-1, 0) if to_self else (0, -1)
    if entry.kind == "transferFromLockOut":
        # Paying yourself out of your own lock returns the funds to
        # available; paying anyone else consumes them from the lock.
        return (1, -1) if to_self else (0, -1)
    return _KIND_EFFECTS.get(entry.kind)


@dataclass(frozen=True)
class BucketPoint:
    timestamp: int
    available: int
    locked: int


def replay_history(
    entries: list[HistoryEntry], user_address: str
) -> dict[str, list[BucketPoint]]:
    """Replay a user's history into per-token available/locked change-points.

    Pure function: starts every token at (0, 0) and applies entries oldest
    first, emitting one point per distinct timestamp (all events sharing a
    timestamp collapse into the final state at that instant). user_address is
    the history owner; it disambiguates the entries whose bucket depends on
    whether the counterparty is the user themselves. Balances can go negative
    when the history is truncated — the accounting service only records from
    its own deployment forward — and that is preserved rather than clamped so
    downstream valuation can decide how to baseline.

    Entries with an unhandled kind or without token_id/amount are skipped;
    they carry no balance effect that we can attribute.
    """
    owner = user_address.lower()
    by_token: dict[str, list[tuple[int, int, tuple[int, int]]]] = {}
    for entry in entries:
        effect = _effect(entry, owner)
        if effect is None:
            logger.warning("Skipping history entry with unhandled kind=%s", entry.kind)
            continue
        if entry.token_id is None or entry.amount is None:
            logger.warning("Skipping %s entry without token_id/amount", entry.kind)
            continue
        by_token.setdefault(entry.token_id, []).append(
            (entry.timestamp, int(entry.amount), effect)
        )

    series: dict[str, list[BucketPoint]] = {}
    for token_id, token_entries in by_token.items():
        token_entries.sort(key=lambda item: item[0])
        points: list[BucketPoint] = []
        available = 0
        locked = 0
        for timestamp, amount, (avail_mult, locked_mult) in token_entries:
            available += avail_mult * amount
            locked += locked_mult * amount
            point = BucketPoint(timestamp=timestamp, available=available, locked=locked)
            if points and points[-1].timestamp == timestamp:
                points[-1] = point
            else:
                points.append(point)
        series[token_id] = points
    return series
