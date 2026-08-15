from dataclasses import dataclass

from src.services.earn.value_history import EarnCashflow


@dataclass(frozen=True)
class PrincipalPoint:
    timestamp: int
    principal: int


def principal_series(flows: list[EarnCashflow]) -> dict[str, list[PrincipalPoint]]:
    """Cumulative net deposited assets per pool as change-points.

    Consumes the settled cashflows from ``value_history`` (deposits including
    undeployed ones — shares are minted before that status is set — and
    completed withdrawals), so principal and value are derived from the same
    rows. Events sharing a timestamp collapse into the final state at that
    instant, same as the balance replay. Net principal can go negative when
    withdrawals include yield on top of the original deposit — that is real
    data (the user took out more than they put in), not an error.
    """
    series: dict[str, list[PrincipalPoint]] = {}
    totals: dict[str, int] = {}
    for flow in flows:
        totals[flow.pool_id] = totals.get(flow.pool_id, 0) + int(flow.signed_amount)
        point = PrincipalPoint(timestamp=flow.timestamp, principal=totals[flow.pool_id])
        points = series.setdefault(flow.pool_id, [])
        if points and points[-1].timestamp == flow.timestamp:
            points[-1] = point
        else:
            points.append(point)
    return series
