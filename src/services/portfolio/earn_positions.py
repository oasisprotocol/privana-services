from dataclasses import dataclass

from src.core.db import get_db
from src.services.earn.vault_service import (
    EARN_OP_DEPOSIT,
    EARN_OP_WITHDRAW,
    EARN_STATUS_COMPLETED,
)


@dataclass(frozen=True)
class EarnFlow:
    timestamp: int
    operation: str
    pool_id: str
    token_id: str
    amount: int


@dataclass(frozen=True)
class PrincipalPoint:
    timestamp: int
    principal: int


def earn_flows(user_address: str) -> list[EarnFlow]:
    """Completed earn deposits/withdrawals for a user, oldest first.

    Timestamps are ``updated_at``: the row is stamped when the on-chain call
    settles, which is when the position actually changes — ``created_at`` only
    records when the request was signed. Amounts are asset amounts as passed
    to the contract; converting a flow to shares needs the pool rate at that
    instant and is the rate provider's job, not this table's.
    """
    rows = (
        get_db()
        .execute(
            "SELECT operation, pool_id, token_id, amount, updated_at "
            "FROM earn_transactions WHERE user_address = ? AND status = ? "
            "ORDER BY updated_at ASC",
            (user_address.lower(), EARN_STATUS_COMPLETED),
        )
        .fetchall()
    )
    return [
        EarnFlow(
            timestamp=row["updated_at"],
            operation=row["operation"],
            pool_id=row["pool_id"],
            token_id=row["token_id"],
            amount=int(row["amount"]),
        )
        for row in rows
    ]


def principal_series(flows: list[EarnFlow]) -> dict[str, list[PrincipalPoint]]:
    """Cumulative net deposited assets per pool as change-points.

    Deposits add, withdrawals subtract, other operations are ignored. Events
    sharing a timestamp collapse into the final state at that instant, same
    as the balance replay. Net principal can go negative when withdrawals
    include yield on top of the original deposit — that is real data (the
    user took out more than they put in), not an error.
    """
    series: dict[str, list[PrincipalPoint]] = {}
    totals: dict[str, int] = {}
    for flow in flows:
        if flow.operation == EARN_OP_DEPOSIT:
            delta = flow.amount
        elif flow.operation == EARN_OP_WITHDRAW:
            delta = -flow.amount
        else:
            continue
        totals[flow.pool_id] = totals.get(flow.pool_id, 0) + delta
        point = PrincipalPoint(timestamp=flow.timestamp, principal=totals[flow.pool_id])
        points = series.setdefault(flow.pool_id, [])
        if points and points[-1].timestamp == flow.timestamp:
            points[-1] = point
        else:
            points.append(point)
    return series
