import logging

from fastapi import APIRouter, HTTPException, Query

from src.models.api import (
    QuoteResponse,
    SwapRequest,
    SwapResponse,
    SwapStatusResponse,
)
from src.services.swap.quote_service import get_quote_service
from src.services.swap.executor import get_swap_executor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Swap"])


@router.get("/quote", response_model=QuoteResponse)
async def get_quote(
    from_token_id: str = Query(..., description="Source token bytes32 ID"),
    to_token_id: str = Query(..., description="Destination token bytes32 ID"),
    from_amount: str = Query(..., description="Amount in base units"),
    user_address: str = Query(..., description="User wallet address"),
    slippage: float = Query(default=0.03, ge=0.0, le=1.0),
) -> QuoteResponse:
    try:
        service = get_quote_service()
        return await service.get_quote(
            from_token_id=from_token_id,
            to_token_id=to_token_id,
            from_amount=from_amount,
            user_address=user_address,
            slippage=slippage,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Quote request failed")
        raise HTTPException(status_code=500, detail="Failed to get quote") from exc


@router.post("/swap", response_model=SwapResponse)
async def execute_swap(payload: SwapRequest) -> SwapResponse:
    try:
        executor = get_swap_executor()
        swap = await executor.execute_swap(
            quote_id=payload.quote_id,
            user_address=payload.user_address,
            input_nonce=payload.input_nonce,
            input_signature=payload.input_signature,
        )
        return SwapResponse(
            swap_id=swap.id,
            status=swap.status,
            message="Swap completed" if swap.status == "completed" else "Swap failed",
            tx_hash=swap.swap_tx_hash,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Swap execution failed")
        raise HTTPException(status_code=500, detail="Failed to execute swap") from exc


@router.get("/swap/{swap_id}/status", response_model=SwapStatusResponse)
async def get_swap_status(swap_id: str) -> SwapStatusResponse:
    try:
        executor = get_swap_executor()
        swap = executor._get_swap(swap_id)
        return SwapStatusResponse(
            swap_id=swap.id,
            status=swap.status,
            from_token_id=swap.from_token_id,
            to_token_id=swap.to_token_id,
            from_amount=swap.from_amount,
            to_amount_estimate=swap.to_amount_estimate,
            to_amount_actual=swap.to_amount_actual,
            swap_tx_hash=swap.swap_tx_hash,
            error=swap.error,
            created_at=swap.created_at,
            updated_at=swap.updated_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to get swap status")
        raise HTTPException(status_code=500, detail="Failed to get swap status") from exc
