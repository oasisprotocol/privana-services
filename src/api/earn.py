import logging

from fastapi import APIRouter, HTTPException, Query

from src.models.earn import (
    PoolDetailResponse,
    PoolListResponse,
    PoolResponse,
)
from src.services.earn.vault_service import get_vault_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/earn", tags=["Earn"])


@router.get("/pools", response_model=PoolListResponse)
async def list_pools(
    status: str = Query(default=None, description="Filter by pool status"),
) -> PoolListResponse:
    try:
        service = get_vault_service()
        pools = service.list_pools(status=status)
        return PoolListResponse(
            pools=[
                PoolResponse(
                    pool_id=p.id,
                    token_id=p.token_id,
                    strategy=p.strategy,
                    total_assets=p.total_assets,
                    apy_bps=p.apy_bps,
                    status=p.status,
                )
                for p in pools
            ]
        )
    except Exception as exc:
        logger.exception("Failed to list earn pools")
        raise HTTPException(status_code=500, detail="Failed to list pools") from exc


@router.get("/pools/{pool_id}", response_model=PoolDetailResponse)
async def get_pool(pool_id: str) -> PoolDetailResponse:
    try:
        service = get_vault_service()
        p = service.get_pool(pool_id)
        return PoolDetailResponse(
            pool_id=p.id,
            token_id=p.token_id,
            strategy=p.strategy,
            total_shares=p.total_shares,
            total_assets=p.total_assets,
            pool_address=p.pool_address,
            apy_bps=p.apy_bps,
            status=p.status,
            last_harvest_at=p.last_harvest_at,
            created_at=p.created_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to get earn pool")
        raise HTTPException(status_code=500, detail="Failed to get pool") from exc
