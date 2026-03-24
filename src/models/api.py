from __future__ import annotations

from typing import Any, Optional

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
    approval_address: Optional[str] = None
    expires_at: int


class SwapRequest(BaseModel):
    quote_id: str = Field(..., description="Quote ID from GET /v1/quote")
    user_address: str = Field(..., description="User wallet address")
    lock_signature: str = Field(..., description="EIP-712 Lock signature from user")
    lock_expiry: int = Field(..., description="Lock expiry timestamp")


class SwapResponse(BaseModel):
    swap_id: str
    status: str
    message: str


class SwapStatusResponse(BaseModel):
    swap_id: str
    status: str
    from_token_id: str
    to_token_id: str
    from_chain_id: int
    to_chain_id: int
    from_amount: str
    to_amount_estimate: str
    to_amount_actual: Optional[str] = None
    approval_tx_hash: Optional[str] = None
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


class RelayExecuteRequest(BaseModel):
    chain_id: int
    to: str
    data: str
    value: int = 0
    gas_limit: int = 200_000


class RelayExecuteResponse(BaseModel):
    submission_id: str
    tx_hash: Optional[str] = None
    status: str


class RelaySettleSwapRequest(BaseModel):
    user_address: str
    lock_id: int
    output_token_id: str
    output_amount: int
    swap_tx_hash: Optional[str] = None


class LiFiQuoteParams(BaseModel):
    from_chain: int
    to_chain: int
    from_token: str
    to_token: str
    from_amount: str
    from_address: str
    slippage: float = 0.03
    integrator: str = "flexvaults"


class LiFiQuoteResponse(BaseModel):
    id: Optional[str] = None
    type: Optional[str] = None
    tool: Optional[str] = None
    action: Optional[dict[str, Any]] = None
    estimate: Optional[dict[str, Any]] = None
    transaction_request: Optional[dict[str, Any]] = Field(None, alias="transactionRequest")
    included_steps: Optional[list[dict[str, Any]]] = Field(None, alias="includedSteps")

    class Config:
        populate_by_name = True


class LiFiStatusResponse(BaseModel):
    status: Optional[str] = None
    substatus: Optional[str] = None
    receiving: Optional[dict[str, Any]] = None
    sending: Optional[dict[str, Any]] = None
    tool: Optional[str] = None
