from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class SwapStatus(str, Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDING = "refunding"
    REFUNDED = "refunded"


class SwapVenue(str, Enum):
    INTERNAL = "internal"
    LIFI = "lifi"


class LifiSwapStep(str, Enum):
    INPUT_TRANSFER = "input_transfer"
    WITHDRAW = "withdraw"
    LIFI_EXECUTE = "lifi_execute"
    DEPOSIT = "deposit"
    CREDIT = "credit"


class QuoteRecord(BaseModel):
    id: str
    user_address: str
    from_token_id: str
    to_token_id: str
    from_chain_id: int
    to_chain_id: int
    from_amount: str
    to_amount_gross: str
    to_amount_estimate: str
    to_amount_min: str
    route_tool: Optional[str] = None
    liquidity_provider: str
    expires_at: int
    created_at: int
    venue: str = "internal"


class SwapRecord(BaseModel):
    id: str
    quote_id: str
    user_address: str
    from_token_id: str
    to_token_id: str
    from_amount: str
    to_amount_estimate: str
    to_amount_actual: Optional[str] = None
    status: str
    swap_tx_hash: Optional[str] = None
    error: Optional[str] = None
    created_at: int
    updated_at: int
    venue: str = "internal"
    step: Optional[str] = None
    withdrawal_index: Optional[int] = None
    lifi_tx_hash: Optional[str] = None
    deposit_tx_hash: Optional[str] = None
