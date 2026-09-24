from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from src.api._auth import jwt_identity
from src.models.operations import OperationsResponse, UnsettledOperationsResponse
from src.services.operations import list_operations, list_unsettled_operations

router = APIRouter(prefix="/v1/operations", tags=["Operations"])


@router.get("", response_model=OperationsResponse)
async def get_operations(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    before: Optional[str] = Query(default=None, description="next_cursor of the previous page"),
) -> OperationsResponse:
    """All of the caller's swap and earn operations, any status, newest first."""
    identity = await jwt_identity(request)
    try:
        operations, next_cursor = list_operations(identity.address, limit, before)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OperationsResponse(operations=operations, next_cursor=next_cursor)


@router.get("/unsettled", response_model=UnsettledOperationsResponse)
async def get_unsettled_operations(
    request: Request,
    limit: int = Query(default=100, ge=1, le=100),
) -> UnsettledOperationsResponse:
    identity = await jwt_identity(request)
    operations = list_unsettled_operations(identity.address, limit)
    return UnsettledOperationsResponse(operations=operations)
