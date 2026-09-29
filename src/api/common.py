import logging

from fastapi import APIRouter, HTTPException

from src.models.api import (
    ChainInfo,
    ChainListResponse,
    TokenInfo,
    TokenListResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Common"])


@router.get("/tokens", response_model=TokenListResponse)
async def list_tokens() -> TokenListResponse:
    from src.clients.accounting import get_accounting_client
    from src.core.tokens import get_supported_token_ids

    try:
        client = get_accounting_client()
        supported_token_ids = set(get_supported_token_ids())
        all_tokens = await client.list_all_tokens()
        tokens = [
            TokenInfo(
                token_id=info.token_id,
                token_type=info.token_type,
                token_type_name=info.token_type_name,
                chain_id=info.chain_id,
                chain_name=info.chain_name,
                token_address=info.token_address,
                token_symbol=info.symbol,
                token_name=info.name,
                token_decimals=info.decimals,
            )
            for info in all_tokens
            if info.token_id.lower() in supported_token_ids
        ]
        return TokenListResponse(tokens=tokens)
    except Exception as exc:
        logger.exception("Failed to list tokens")
        raise HTTPException(status_code=500, detail="Failed to list tokens") from exc


@router.get("/chains", response_model=ChainListResponse)
async def list_chains() -> ChainListResponse:
    chains = _get_supported_chains()
    return ChainListResponse(chains=chains)


def _get_supported_chains() -> list[ChainInfo]:
    from src.core.tokens import get_supported_chains
    return [ChainInfo(**c) for c in get_supported_chains()]
