from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class SwapStatus(str, Enum):
    QUOTED = "quoted"
    PENDING_LOCK = "pending_lock"
    LOCKED = "locked"
    MONITORING = "monitoring"
    SETTLING = "settling"
    COMPLETED = "completed"
    SWAP_FAILED = "swap_failed"
    SETTLE_FAILED = "settle_failed"
    REFUNDING = "refunding"
    REFUNDED = "refunded"

    @property
    def is_active(self) -> bool:
        return self in {
            SwapStatus.PENDING_LOCK,
            SwapStatus.LOCKED,
            SwapStatus.MONITORING,
            SwapStatus.SETTLING,
            SwapStatus.REFUNDING,
        }

    @property
    def is_terminal(self) -> bool:
        return self in {
            SwapStatus.COMPLETED,
            SwapStatus.REFUNDED,
        }

    @property
    def is_failure(self) -> bool:
        return self in {
            SwapStatus.SWAP_FAILED,
            SwapStatus.SETTLE_FAILED,
        }


VALID_TRANSITIONS: dict[SwapStatus, set[SwapStatus]] = {
    SwapStatus.PENDING_LOCK: {SwapStatus.LOCKED, SwapStatus.SWAP_FAILED},
    SwapStatus.LOCKED: {SwapStatus.MONITORING, SwapStatus.SWAP_FAILED},
    SwapStatus.MONITORING: {SwapStatus.SETTLING, SwapStatus.SWAP_FAILED},
    SwapStatus.SETTLING: {SwapStatus.COMPLETED, SwapStatus.SETTLE_FAILED},
    SwapStatus.SWAP_FAILED: {SwapStatus.REFUNDING, SwapStatus.REFUNDED},
    SwapStatus.SETTLE_FAILED: {SwapStatus.REFUNDING, SwapStatus.REFUNDED},
    SwapStatus.REFUNDING: {SwapStatus.REFUNDED},
}

SUBMISSION_ACCEPTED = frozenset({"submitted", "confirmed", "pending"})


class QuoteRecord(BaseModel):
    id: str
    user_address: str
    from_token_id: str
    to_token_id: str
    from_chain_id: int
    to_chain_id: int
    from_amount: str
    to_amount_estimate: str
    to_amount_min: str
    lifi_response: str
    approval_address: Optional[str] = None
    expires_at: int
    created_at: int


class SwapRecord(BaseModel):
    id: str
    quote_id: str
    user_address: str
    from_token_id: str
    to_token_id: str
    from_chain_id: int
    to_chain_id: int
    from_amount: str
    to_amount_estimate: str
    to_amount_min: str
    to_amount_actual: Optional[str] = None
    status: SwapStatus
    lock_submission_id: Optional[str] = None
    lock_id: Optional[int] = None
    approval_tx_hash: Optional[str] = None
    swap_tx_hash: Optional[str] = None
    lifi_tool_used: Optional[str] = None
    error: Optional[str] = None
    created_at: int
    updated_at: int
