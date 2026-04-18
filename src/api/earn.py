import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.admin_auth import require_admin
from src.models.earn import (
    BalanceListResponse,
    BalanceResponse,
    DepositQuoteResponse,
    DepositRequest,
    DepositResponse,
    HarvestRequest,
    HarvestResponse,
    PoolDetailResponse,
    PoolListResponse,
    PoolResponse,
    WithdrawRequest,
    WithdrawResponse,
)
from src.services.earn.vault_service import get_vault_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/earn", tags=["Earn"])


@router.get("/pools", response_model=PoolListResponse)
async def list_pools() -> PoolListResponse:
    try:
        service = get_vault_service()
        pools = await asyncio.to_thread(service.list_pools)
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
        p = await asyncio.to_thread(service.get_pool, pool_id_bytes)
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


@router.get("/quote", response_model=DepositQuoteResponse)
async def get_deposit_quote(
    pool_id: str = Query(..., description="Earn pool ID (hex)"),
    amount: str = Query(..., description="Amount in base units"),
    user_address: str = Query(..., description="User wallet address"),
) -> DepositQuoteResponse:
    try:
        service = get_vault_service()
        quote = await service.get_deposit_quote(pool_id, amount, user_address)
        return DepositQuoteResponse(**quote)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Deposit quote failed")
        raise HTTPException(status_code=500, detail="Failed to get deposit quote") from exc


@router.post("/deposit", response_model=DepositResponse)
async def deposit(payload: DepositRequest) -> DepositResponse:
    try:
        service = get_vault_service()
        result = await service.deposit(
            pool_id_hex=payload.pool_id,
            user_address=payload.user_address,
            amount=payload.amount,
            nonce=payload.nonce,
            signature=payload.signature,
        )
        return DepositResponse(
            deposit_id=result.get("tx_hash", ""),
            **result,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/withdraw", response_model=WithdrawResponse)
async def withdraw(payload: WithdrawRequest) -> WithdrawResponse:
    try:
        service = get_vault_service()
        result = await service.withdraw(
            pool_id_hex=payload.pool_id,
            user_address=payload.user_address,
            amount=payload.amount,
        )
        return WithdrawResponse(
            withdraw_id=result.get("tx_hash", ""),
            **result,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/balance", response_model=BalanceListResponse)
async def get_balances(
    user_address: str = Query(..., description="User wallet address"),
) -> BalanceListResponse:
    try:
        service = get_vault_service()
        balances = await service.get_all_balances(user_address)
        return BalanceListResponse(
            positions=[BalanceResponse(**b) for b in balances]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to get balances")
        raise HTTPException(status_code=500, detail="Failed to get balances") from exc


@router.post(
    "/harvest",
    response_model=HarvestResponse,
    dependencies=[Depends(require_admin)],
)
async def harvest(payload: HarvestRequest) -> HarvestResponse:
    try:
        service = get_vault_service()
        result = await service.harvest(
            pool_id_hex=payload.pool_id,
            yield_amount=payload.yield_amount,
        )
        return HarvestResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
