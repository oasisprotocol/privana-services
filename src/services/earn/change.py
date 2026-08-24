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
from src.services.pool_rate_history import read_point_before

logger = logging.getLogger(__name__)

WINDOW_SEC = 24 * 3600
# The rate sampler runs ~4x/day; allow one missed sample before declaring the
# anchor too stale to trust.
MAX_SAMPLE_AGE_SEC = 30 * 3600

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
    """
    row = get_db().execute(
        """SELECT 1 FROM earn_transactions
           WHERE user_address = ? AND pool_id = ? AND status != 'failed'
           AND MAX(created_at, updated_at) >= ? LIMIT 1""",
        (user_address.lower(), pool_id, since),
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

    window_start = now - WINDOW_SEC
    if _has_cashflow_since(user_address, pool_id, window_start):
        return None

    point = read_point_before(
        pool_id, ts_max=window_start, ts_min=now - MAX_SAMPLE_AGE_SEC
    )
    if point is None:
        return None

    value_then = _position_value(shares, int(point.total_assets), int(point.total_shares))
    value_now = _position_value(shares, total_assets, total_shares)
    if value_then is None or value_now is None or value_then == 0:
        return None

    amount = value_now - value_then
    pct = (Decimal(amount) / Decimal(value_then)).quantize(_PCT_PRECISION)
    return Change(amount=str(amount), pct=str(pct))
