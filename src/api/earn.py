import asyncio
import logging

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from src.clients.accounting import get_accounting_client
from src.models.earn import (
    BalanceListResponse,
    BalanceResponse,
    DepositQuoteResponse,
    DepositRequest,
    DepositResponse,
    PoolDetailResponse,
    PoolListResponse,
    PoolResponse,
    WithdrawRequest,
    WithdrawResponse,
)
from src.services.earn.registry import get_strategy_registry
from src.services.earn.vault_service import get_vault_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/earn", tags=["Earn"])


def _auth_error(detail: str = "Bearer token required") -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer_token(authorization: str) -> str:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _auth_error("Invalid bearer token")
    return token.strip()


async def _private_read_token(request: Request) -> str:
    authorization = request.headers.get("authorization")
    siwe_token = request.headers.get("x-siwe-token")
    if authorization and siwe_token:
        raise HTTPException(
            status_code=400,
            detail="Use either Authorization or X-SIWE-Token, not both",
        )
    if siwe_token:
        return siwe_token
    if not authorization:
        raise _auth_error()

    try:
        return await get_accounting_client().exchange_jwt_for_siwe_token(
            _bearer_token(authorization)
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise _auth_error("Invalid bearer token") from exc
        if exc.response.status_code == 403:
            raise HTTPException(
                status_code=403,
                detail="Bearer token is not allowed",
            ) from exc
        logger.warning(
            "Accounting JWT exchange failed with status %d",
            exc.response.status_code,
        )
        raise HTTPException(
            status_code=502,
            detail="Accounting token exchange failed",
        ) from exc
    except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.TimeoutException) as exc:
        logger.warning("Accounting JWT exchange request failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Accounting token exchange failed",
        ) from exc
    except (KeyError, ValueError, RuntimeError) as exc:
        logger.warning("Accounting JWT exchange returned an invalid response")
        raise HTTPException(
            status_code=502,
            detail="Accounting token exchange failed",
        ) from exc


@router.get("/pools", response_model=PoolListResponse)
async def list_pools() -> PoolListResponse:
    try:
        service = get_vault_service()
        pools = await asyncio.to_thread(service.list_pools)
        # AUM and APY reads are independent per pool, so fan them out together
        # to keep tail latency at max(slowest read) instead of sum-of-reads.
        results = await asyncio.gather(
            *[
                asyncio.gather(
                    service.effective_total_assets(p["pool_id"], p["total_assets"]),
                    service.strategy_apy_bps_safe(p["pool_id"]),
                )
                for p in pools
            ]
        )
        responses = [
            PoolResponse(
                pool_id=p["pool_id"],
                token_id=p["token_id"],
                strategy=get_strategy_registry().get(p["pool_id"]).name,
                total_assets=str(effective),
                apy_bps=apy_bps,
                status="active" if p["active"] else "paused",
                pool_address=p["pool_address"],
            )
            for p, (effective, apy_bps) in zip(pools, results)
        ]
        return PoolListResponse(pools=responses)
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
        effective, apy_bps = await asyncio.gather(
            service.effective_total_assets(pool_id, p["total_assets"]),
            service.strategy_apy_bps_safe(pool_id),
        )
        return PoolDetailResponse(
            pool_id=pool_id,
            token_id=p["token_id"],
            strategy=get_strategy_registry().get(pool_id).name,
            total_shares=str(p["total_shares"]),
            total_assets=str(effective),
            pool_address=p["pool_address"],
            apy_bps=apy_bps,
            status="active" if p["active"] else "paused",
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
            nonce=payload.nonce,
            signature=payload.signature,
        )
        return WithdrawResponse(
            withdraw_id=result.get("tx_hash", ""),
            **result,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/withdraw/nonce")
async def get_withdraw_nonce(request: Request) -> dict:
    token = await _private_read_token(request)
    try:
        service = get_vault_service()
        nonce = await asyncio.to_thread(
            service.get_withdraw_nonce_via_token, token
        )
        return {"nonce": nonce}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to fetch withdraw nonce")
        raise HTTPException(status_code=500, detail="Failed to fetch withdraw nonce") from exc


@router.get("/balance", response_model=BalanceListResponse)
async def get_balances(request: Request) -> BalanceListResponse:
    token = await _private_read_token(request)
    try:
        service = get_vault_service()
        balances = await service.get_all_balances(token)
        return BalanceListResponse(
            positions=[BalanceResponse(**b) for b in balances]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to get balances")
        raise HTTPException(status_code=500, detail="Failed to get balances") from exc
