import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from src.api._auth import jwt_identity
from src.models.history import (
    PortfolioHistoryPoint,
    PortfolioHistoryResponse,
    usd_string,
)
from src.services.portfolio.history_service import MAX_HISTORY_DAYS, portfolio_history

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/portfolio", tags=["Portfolio"])

DAYS_QUERY = Query(
    default=None,
    ge=1,
    le=MAX_HISTORY_DAYS,
    description=(
        "Limit the series to the most recent N days. Omit it for everything "
        "since the user's first activity, which is what a client should send "
        "for an 'All' range."
    ),
)


@router.get("/history", response_model=PortfolioHistoryResponse)
async def get_portfolio_history(
    request: Request,
    days: Optional[int] = DAYS_QUERY,
) -> PortfolioHistoryResponse:
    """Total portfolio value over time, oldest first.

    Each point prices the balances held at that moment with the rate in force
    then, so the curve reflects both movements and price changes. Ranges up
    to 90 days are sampled ~4x/day, longer ones daily.
    """
    identity = await jwt_identity(request)
    try:
        series = await portfolio_history(identity, days)
    except httpx.HTTPError as exc:
        logger.warning("Accounting history read failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="Failed to read accounting history"
        ) from exc
    except Exception as exc:
        logger.exception("Failed to build portfolio history")
        raise HTTPException(
            status_code=500, detail="Failed to build portfolio history"
        ) from exc

    return PortfolioHistoryResponse(
        points=[
            PortfolioHistoryPoint(
                timestamp=point.timestamp,
                total_usd=usd_string(point.total_e8),
                available_usd=usd_string(point.available_e8),
                locked_usd=usd_string(point.locked_e8),
                earn_usd=usd_string(point.earn_e8),
            )
            for point in series
        ]
    )
