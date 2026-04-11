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
async def list_pools() -> PoolListResponse:
    try:
        service = get_vault_service()
        pools = service.list_pools()
        return PoolListResponse(
            pools=[
                PoolResponse(
                    pool_id=p["pool_id"],
                    token_id=p["token_id"],
                    strategy="aave-v3",
                    total_assets=str(p["total_assets"]),
                    apy_bps=0,
                    status="active" if p["active"] else "paused",
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
        pool_id_bytes = bytes.fromhex(pool_id.removeprefix("0x"))
        p = service.get_pool(pool_id_bytes)
        if p["pool_address"] == "0x0000000000000000000000000000000000000000":
            raise ValueError("Pool not found")
        return PoolDetailResponse(
            pool_id=pool_id,
            token_id=p["token_id"],
            strategy="aave-v3",
            total_shares=str(p["total_shares"]),
            total_assets=str(p["total_assets"]),
            pool_address=p["pool_address"],
            apy_bps=0,
            status="active" if p["active"] else "paused",
            last_harvest_at=None,
            created_at=0,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to get earn pool")
        raise HTTPException(status_code=500, detail="Failed to get pool") from exc
