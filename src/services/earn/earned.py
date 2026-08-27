"""Accrued yield per earn position.

``earned_active`` is yield on money currently deployed: the position's value
now minus what the user paid for the shares they still hold. It is derived
from the settled cashflow ledger, never from the replayed chart series.

Cost is tracked in integer base units rather than as a weighted-average rate
per share. A deposit adds its full amount to the basis; a withdrawal removes
the basis proportional to the shares it burns, and the difference between
what the user took out and the basis removed is realised yield.

This is the weighted-average model carried out in integers rather than an
exact reproduction of it. Removing basis by floor division leaves the
remainder with the shares still held, which shifts sub-unit dust from
realised into cost and so reports active marginally low — never high — by
under one base unit per withdrawal. The trade is deliberate: no floating
point anywhere near money, and this identity stays exact regardless of
rounding, because the same rounded amount is subtracted from cost and added
to realised:

    active + realised == value_now - (deposited - withdrawn)

A figure that cannot be derived honestly is reported as None with a status
saying why. It is never a fabricated zero: the ledger being incomplete looks
exactly like a position that earned nothing.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from src.core.db import get_db

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_LEDGER_INCOMPLETE = "ledger_incomplete"
STATUS_PENDING_SETTLEMENT = "pending_settlement"
STATUS_UNSUPPORTED = "unsupported"

_OP_DEPOSIT = "deposit"
_STATUS_FAILED = "failed"
_STATUS_PENDING = "pending"


@dataclass(frozen=True)
class Earned:
    """Yield on the shares a user still holds, plus the basis behind it."""

    active: Optional[str]
    status: str
    cost_basis: Optional[str] = None
    realised: Optional[str] = None
    deposit_count: int = 0
    first_deposit_at: Optional[int] = None


def _pool_shares_accounted(pool_id: str) -> Optional[int]:
    """Sum of every recorded share movement in a pool, or None if any is missing.

    A per-user share match alone is not proof the history is complete: two
    errors can cancel, for instance a deposit relayed straight to the
    contract (``EarnManager.deposit`` is externally callable) paired with a
    withdrawal this service could not attribute. Comparing the pool's whole
    recorded movement against the chain's ``totalShares`` closes that gap.
    """
    row = get_db().execute(
        """SELECT COUNT(*) AS missing FROM earn_transactions
           WHERE LOWER(pool_id) = ? AND status != ? AND shares_delta IS NULL""",
        (pool_id.lower(), _STATUS_FAILED),
    ).fetchone()
    if row["missing"]:
        return None

    row = get_db().execute(
        """SELECT COALESCE(SUM(CAST(shares_delta AS INTEGER)), 0) AS total
           FROM earn_transactions
           WHERE LOWER(pool_id) = ? AND status != ?""",
        (pool_id.lower(), _STATUS_FAILED),
    ).fetchone()
    return int(row["total"])


def _read_cashflows(user_address: str, pool_id: str) -> list[dict]:
    """Settled and in-flight cashflows attributable to this user and pool.

    Deposits are keyed by the depositor. Withdrawals are keyed by the consent
    signer, because a withdraw row's user_address is the payout recipient and
    the shares burn from whoever signed the consent. Rows predating that
    column carry NULL and go unattributed here; the completeness check below
    catches the resulting gap rather than silently under-counting.
    """
    wallet = user_address.lower()
    rows = get_db().execute(
        """SELECT operation, amount, shares_delta, status, created_at, settled_at
           FROM earn_transactions
           WHERE LOWER(pool_id) = ? AND status != ?
           AND ((operation = ? AND user_address = ?)
                OR (operation != ? AND consent_signer = ?))
           ORDER BY created_at ASC, id ASC""",
        (pool_id.lower(), _STATUS_FAILED, _OP_DEPOSIT, wallet, _OP_DEPOSIT, wallet),
    ).fetchall()
    return [dict(row) for row in rows]


def earned_active(
    user_address: Optional[str],
    pool_id: str,
    shares: int,
    value_now: int,
    pool_total_shares: Optional[int] = None,
) -> Earned:
    """Yield on currently held shares.

    ``value_now`` must be the same position value the response reports, which
    is the contract's ``convertToAssets``. Deriving it here from the pool
    ratio would drop the contract's virtual share offset and produce an
    "earned" that does not reconcile with the balance shown beside it.
    """
    if not user_address:
        return Earned(active=None, status=STATUS_UNSUPPORTED)

    rows = _read_cashflows(user_address, pool_id)
    if any(row["status"] == _STATUS_PENDING for row in rows):
        return Earned(active=None, status=STATUS_PENDING_SETTLEMENT)
    if any(row["shares_delta"] is None for row in rows):
        return Earned(active=None, status=STATUS_LEDGER_INCOMPLETE)

    held = 0
    cost_basis = 0
    realised = 0
    deposit_count = 0
    first_deposit_at: Optional[int] = None

    for row in rows:
        delta = int(row["shares_delta"])
        amount = int(row["amount"])
        if row["operation"] == _OP_DEPOSIT:
            held += delta
            cost_basis += amount
            deposit_count += 1
            if first_deposit_at is None:
                # When the deposit settled, falling back to submission time
                # for rows written before settled_at existed.
                first_deposit_at = row["settled_at"] or row["created_at"]
            continue

        burned = -delta
        if burned <= 0 or held <= 0:
            # A burn that moved no shares, or one against a position the
            # ledger says is already empty, means the history is not what
            # actually happened on chain.
            return Earned(active=None, status=STATUS_LEDGER_INCOMPLETE)
        # Remove basis in proportion to the shares leaving, so the price the
        # remaining shares were bought at is untouched by an exit.
        basis_out = cost_basis * burned // held
        realised += amount - basis_out
        cost_basis -= basis_out
        held -= burned

    # The ledger must account for every share the contract says the user
    # holds. Any mismatch means a cashflow is missing, and a number derived
    # from a partial history is worse than no number.
    if held != shares:
        logger.info(
            "earned ledger incomplete pool=%s ledger_shares=%d chain_shares=%d",
            pool_id, held, shares,
        )
        return Earned(active=None, status=STATUS_LEDGER_INCOMPLETE)

    if pool_total_shares is not None:
        accounted = _pool_shares_accounted(pool_id)
        if accounted is None or accounted != pool_total_shares:
            logger.info(
                "earned pool ledger incomplete pool=%s accounted=%s chain=%d",
                pool_id, accounted, pool_total_shares,
            )
            return Earned(active=None, status=STATUS_LEDGER_INCOMPLETE)

    return Earned(
        active=str(value_now - cost_basis),
        status=STATUS_OK,
        cost_basis=str(cost_basis),
        realised=str(realised),
        deposit_count=deposit_count,
        first_deposit_at=first_deposit_at,
    )
