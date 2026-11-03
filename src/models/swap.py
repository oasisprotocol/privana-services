from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class SwapStatus(str, Enum):
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDING = "refunding"
    REFUNDED = "refunded"


class SwapVenue(str, Enum):
    INTERNAL = "internal"
    LIFI = "lifi"


class SwapStep(str, Enum):
    """Where a swap is: `<stage>:<substatus>` while it executes or refunds,
    and once it failed, whether a person has to return the user's funds."""

    INTERNAL_SWAP_PRICING = "internal_swap:pricing"
    INTERNAL_SWAP_SENT = "internal_swap:sent"
    INPUT_SENT = "input:sent"
    WITHDRAW_SENT = "withdraw:sent"
    EXECUTE_FUNDING = "execute:funding"
    EXECUTE_SENT = "execute:sent"
    EXECUTE_BRIDGING = "execute:bridging"
    DEPOSIT_SENT = "deposit:sent"
    DEPOSIT_CREDITING = "deposit:crediting"
    CREDIT_SENT = "credit:sent"
    REFUND_REDEPOSIT = "refund:redeposit"
    REFUND_CREDITING = "refund:crediting"
    REFUND_PAYOUT = "refund:payout"
    # Failed, and what the user's funds did: a parked swap may hold them
    # until a person returns them.
    PARKED = "parked"
    NO_FUNDS_MOVED = "no_funds_moved"


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
    slippage_bps: Optional[int] = None


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
