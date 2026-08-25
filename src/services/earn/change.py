"""24h change for earn positions (EA-Products #168).

The badge measures yield only: value now minus value 24h ago, both computed
as ``shares × total_assets // total_shares`` so deposits and withdrawals never
read as gains. The historical rate comes from ``pool_rate_history``, which
samples real chain state — deliberately not the replayed cashflow series,
which can drift from the chain (EA-Products #170).

Whenever the figure cannot be computed honestly the result is None and the
UI hides the badge; a fabricated 0 is never returned. That covers: caller not
identified (bare SIWE token), a cashflow touching the window (exact handling
needs the per-cashflow share ledger from EA-Products #167), no rate sample
old enough, sampler outage older than the staleness cap, or a zero base.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from src.core.db import get_db

logger = logging.getLogger(__name__)

WINDOW_SEC = 24 * 3600
# The rate sampler runs ~4x/day and labels are grid-floored; allow roughly one
# missed sample beyond the widened window before declaring the anchor too
# stale to trust.
MAX_SAMPLE_AGE_SEC = 36 * 3600

_PCT_PRECISION = Decimal("0.000001")


@dataclass(frozen=True)
class Change:
    """Signed token-base-unit delta and its fraction of the window-start value."""

    amount: str
    pct: str


def _position_value(shares: int, total_assets: int, total_shares: int) -> Optional[int]:
    if total_shares <= 0:
        return None
    return shares * total_assets // total_shares


def _has_cashflow_since(user_address: str, pool_id: str, since: int) -> bool:
    """True when any non-failed cashflow for (user, pool) touches the window.

    Pending rows count: a pending withdrawal may already have landed on-chain
    (vault_service can crash between broadcast and the DB update), and a
    landed-but-uncounted cashflow is exactly the case that must null the badge.
    Withdrawals are matched on consent_signer too — user_address on a withdraw
    row is the payout recipient, and shares burn from whoever signed the
    consent.
    """
    # pool_id casing in the ledger follows whatever the deposit payload sent,
    # so the comparison must not be case-sensitive.
    wallet = user_address.lower()
    row = get_db().execute(
        """SELECT 1 FROM earn_transactions
           WHERE (user_address = ? OR consent_signer = ?)
           AND LOWER(pool_id) = ? AND status != 'failed'
           AND MAX(created_at, updated_at) >= ? LIMIT 1""",
        (wallet, wallet, pool_id.lower(), since),
    ).fetchone()
    return row is not None


def change_24h(
    user_address: Optional[str],
    pool_id: str,
    shares: int,
    total_assets: int,
    total_shares: int,
    now: int,
) -> Optional[Change]:
    if not user_address or shares <= 0:
        return None

    # Imported here: pool_rate_history's sampler pulls in vault_service, which
    # imports this module — a top-level import would be circular.
    from src.services.pool_rate_history import read_point_before

    # Bounds apply to the real reading time, so the anchor is genuinely at
    # least 24h old and at most MAX_SAMPLE_AGE_SEC old. The sampler runs
    # ~4x/day, so in practice the span lands close to 24h; the cap is what
    # keeps "24h" from quietly meaning "since the sampler last worked".
    point = read_point_before(
        pool_id,
        ts_max=now - WINDOW_SEC,
        ts_min=now - MAX_SAMPLE_AGE_SEC,
    )
    if point is None:
        return None

    # Guard the whole measured span, not just the last 24h: the anchor can be
    # older than the nominal window, and a cashflow anywhere between anchor
    # and now changes the share count mid-measurement. Guard from the grid
    # label rather than the reading time, since that is the earlier of the two
    # and widening the guard is the safe direction.
    if _has_cashflow_since(user_address, pool_id, point.timestamp):
        return None

    value_then = _position_value(shares, int(point.total_assets), int(point.total_shares))
    value_now = _position_value(shares, total_assets, total_shares)
    if value_then is None or value_now is None or value_then == 0:
        return None

    amount = value_now - value_then
    pct = (Decimal(amount) / Decimal(value_then)).quantize(_PCT_PRECISION)
    return Change(amount=str(amount), pct=str(pct))
