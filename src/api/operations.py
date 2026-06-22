from fastapi import APIRouter, HTTPException, Query, Request

from src.api._auth import auth_error, bearer_token, resolve_via_accounting
from src.clients.accounting import get_accounting_client
from src.models.operations import UnsettledOperationsResponse
from src.services.operations import list_unsettled_operations

router = APIRouter(prefix="/v1/operations", tags=["Operations"])


async def _jwt_user_address(request: Request) -> str:
    if request.headers.get("x-siwe-token"):
        raise HTTPException(
            status_code=400,
            detail="Use Authorization bearer token; X-SIWE-Token is not accepted",
        )

    authorization = request.headers.get("authorization")
    if not authorization:
        raise auth_error()

    token = bearer_token(authorization)
    return await resolve_via_accounting(
        lambda: get_accounting_client().get_jwt_user_address(token),
        failure_detail="Accounting token validation failed",
        log_label="Accounting JWT identity lookup",
    )


@router.get("/unsettled", response_model=UnsettledOperationsResponse)
async def get_unsettled_operations(
    request: Request,
    limit: int = Query(default=100, ge=1, le=100),
) -> UnsettledOperationsResponse:
    user_address = await _jwt_user_address(request)
    operations = list_unsettled_operations(user_address, limit)
    return UnsettledOperationsResponse(operations=operations)
