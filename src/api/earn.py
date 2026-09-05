import asyncio
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from src.api._auth import auth_error, bearer_token, jwt_identity, resolve_via_accounting
from src.clients.accounting import get_accounting_client
from src.models.earn import (
    ApyHistoryPoint,
    ApyHistoryResponse,
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
from src.models.history import EarnHistoryPoint, EarnHistoryResponse, usd_string
from src.services.earn.registry import get_strategy_registry
from src.services.earn.vault_service import get_vault_service
from src.services.portfolio.history_service import MAX_HISTORY_DAYS, earn_history

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/earn", tags=["Earn"])

_POOL_ID_HEX_LEN = 64


def _pool_id_bytes(pool_id: str) -> bytes:
    """Parse a path/query pool id into its 32 raw bytes, rejecting malformed
    input with a clean 400 instead of leaking a parser error or letting a
    wrong-length value reach the contract layer as a 500.
    """
    stripped = pool_id.removeprefix("0x")
    try:
        raw = bytes.fromhex(stripped)
    except ValueError:
        raw = b""
    if len(stripped) != _POOL_ID_HEX_LEN or len(raw) != 32:
        raise HTTPException(
            status_code=400, detail="pool_id must be a 32-byte hex value"
        )
    return raw


async def _private_read_identity(request: Request) -> tuple[str, Optional[str]]:
    """SIWE read token plus the caller's address where it is knowable.

    The JWT exchange returns both from one cached accounting call; a bare
    X-SIWE-Token authorises the read but identifies nobody, so the address
    is None on that path.
    """
    authorization = request.headers.get("authorization")
    siwe_token = request.headers.get("x-siwe-token")
    if authorization and siwe_token:
        raise HTTPException(
            status_code=400,
            detail="Use either Authorization or X-SIWE-Token, not both",
        )
    if siwe_token:
        return siwe_token, None
    if not authorization:
        raise auth_error()

    token = bearer_token(authorization)
    identity = await resolve_via_accounting(
        lambda: get_accounting_client().get_jwt_identity(token),
        failure_detail="Accounting token exchange failed",
        log_label="Accounting JWT exchange",
    )
    return identity.siwe_token, identity.address


async def _private_read_token(request: Request) -> str:
    token, _ = await _private_read_identity(request)
    return token


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
    pool_id_bytes = _pool_id_bytes(pool_id)
    try:
        service = get_vault_service()
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


@router.get("/pools/{pool_id}/apy-history", response_model=ApyHistoryResponse)
async def get_pool_apy_history(
    pool_id: str,
    days: Optional[int] = Query(
        default=None,
        ge=1,
        description=(
            "Limit the series to the most recent N days. Omit it to get everything "
            "the source has, which is what a client should send for an 'All' range."
        ),
    ),
) -> ApyHistoryResponse:
    """APY over time for a pool, oldest first.

    Empty `points` is a normal answer, not an error: a pool whose strategy has no
    historical source simply has no chart to draw. The same goes for a failed read
    upstream — the history is decoration on a pool that works either way, so it
    degrades to empty rather than failing the request.
    """
    _pool_id_bytes(pool_id)
    service = get_vault_service()
    points = await service.strategy_apy_history_safe(pool_id, days)
    return ApyHistoryResponse(
        pool_id=pool_id,
        points=[ApyHistoryPoint(timestamp=p.timestamp, apy_bps=p.apy_bps) for p in points],
    )


@router.get("/quote", response_model=DepositQuoteResponse)
async def get_deposit_quote(
    pool_id: str = Query(..., description="Earn pool ID (hex)"),
    amount: str = Query(..., description="Amount in base units"),
    user_address: str = Query(..., description="User wallet address"),
) -> DepositQuoteResponse:
    _pool_id_bytes(pool_id)
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
    _pool_id_bytes(payload.pool_id)
    try:
        service = get_vault_service()
        result = await service.deposit(
            pool_id_hex=payload.pool_id,
            user_address=payload.user_address,
            amount=payload.amount,
            nonce=payload.nonce,
            signature=payload.signature,
        )
        # An on-chain revert is a settled outcome, reported as status="failed" on a
        # 200. Only a request we could not act on at all is an HTTP error.
        return DepositResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/withdraw", response_model=WithdrawResponse)
async def withdraw(payload: WithdrawRequest) -> WithdrawResponse:
    _pool_id_bytes(payload.pool_id)
    try:
        service = get_vault_service()
        result = await service.withdraw(
            pool_id_hex=payload.pool_id,
            user_address=payload.user_address,
            amount=payload.amount,
            nonce=payload.nonce,
            signature=payload.signature,
        )
        return WithdrawResponse(**result)
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


@router.get("/history", response_model=EarnHistoryResponse)
async def get_earn_history(
    request: Request,
    days: Optional[int] = Query(
        default=None,
        ge=1,
        le=MAX_HISTORY_DAYS,
        description=(
            "Limit the series to the most recent N days. Omit it for everything "
            "since the user's first deposit, which is what a client should send "
            "for an 'All' range."
        ),
    ),
) -> EarnHistoryResponse:
    """Value of the user's earn positions over time, oldest first.

    Reads the same series the portfolio chart uses for its earn slice, so the
    two charts always agree. Empty for a user who has never deposited.
    """
    identity = await jwt_identity(request)
    try:
        series = await earn_history(identity.address, days)
    except httpx.HTTPError as exc:
        logger.warning("Earn history upstream read failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="Failed to read earn history"
        ) from exc
    except Exception as exc:
        logger.exception("Failed to build earn history")
        raise HTTPException(
            status_code=500, detail="Failed to build earn history"
        ) from exc

    return EarnHistoryResponse(
        points=[
            EarnHistoryPoint(
                timestamp=point.timestamp, value_usd=usd_string(point.value_e8)
            )
            for point in series
        ]
    )


@router.get("/balance", response_model=BalanceListResponse)
async def get_balances(request: Request) -> BalanceListResponse:
    # The address, known only on the JWT path, unlocks the 24h change fields.
    token, user_address = await _private_read_identity(request)
    try:
        service = get_vault_service()
        balances = await service.get_all_balances(token, user_address=user_address)
        return BalanceListResponse(
            positions=[BalanceResponse(**b) for b in balances]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to get balances")
        raise HTTPException(status_code=500, detail="Failed to get balances") from exc
