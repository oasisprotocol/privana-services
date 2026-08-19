from fastapi import APIRouter, Query, Request

from src.api._auth import jwt_identity
from src.models.operations import UnsettledOperationsResponse
from src.services.operations import list_unsettled_operations

router = APIRouter(prefix="/v1/operations", tags=["Operations"])


@router.get("/unsettled", response_model=UnsettledOperationsResponse)
async def get_unsettled_operations(
    request: Request,
    limit: int = Query(default=100, ge=1, le=100),
) -> UnsettledOperationsResponse:
    identity = await jwt_identity(request)
    operations = list_unsettled_operations(identity.address, limit)
    return UnsettledOperationsResponse(operations=operations)
