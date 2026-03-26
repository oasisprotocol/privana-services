from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class QuoteRequest(BaseModel):
    from_token_id: str = Field(..., description="Source token bytes32 ID (hex)")
    to_token_id: str = Field(..., description="Destination token bytes32 ID (hex)")
    from_amount: str = Field(..., description="Amount in base units")
    user_address: str = Field(..., description="User wallet address")
    slippage: float = Field(default=0.03, ge=0.0, le=1.0, description="Slippage tolerance (0-1)")


class QuoteResponse(BaseModel):
    quote_id: str
    from_token_id: str
    to_token_id: str
    from_chain_id: int
    to_chain_id: int
    from_amount: str
    to_amount_gross: str
    to_amount_estimate: str
    to_amount_min: str
    fee_bps: int
    fee_amount: str
    tool_used: Optional[str] = None
    liquidity_provider: str
    transfer_nonce: int
    expires_at: int


class SwapRequest(BaseModel):
    quote_id: str = Field(..., description="Quote ID from GET /v1/quote")
    user_address: str = Field(..., description="User wallet address")
    input_nonce: int = Field(..., description="Transfer nonce for input token")
    input_signature: str = Field(..., description="EIP-712 transfer signature from user")


class SwapResponse(BaseModel):
    swap_id: str
    status: str
    message: str
    tx_hash: Optional[str] = None


class SwapStatusResponse(BaseModel):
    swap_id: str
    status: str
    from_token_id: str
    to_token_id: str
    from_amount: str
    to_amount_estimate: str
    to_amount_actual: Optional[str] = None
    swap_tx_hash: Optional[str] = None
    error: Optional[str] = None
    created_at: int
    updated_at: int


class TokenInfo(BaseModel):
    token_id: str
    token_type: int
    token_type_name: str
    chain_id: Optional[int] = None
    chain_name: Optional[str] = None
    token_address: Optional[str] = None
    symbol: Optional[str] = None


class TokenListResponse(BaseModel):
    tokens: list[TokenInfo]


class ChainInfo(BaseModel):
    chain_id: int
    name: str


class ChainListResponse(BaseModel):
    chains: list[ChainInfo]
