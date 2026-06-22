from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class UnsettledOperation(BaseModel):
    operation_id: str
    operation_type: str
    status: str
    created_at: int
    updated_at: int
    tx_hash: Optional[str] = None
    error: Optional[str] = None

    quote_id: Optional[str] = None
    from_token_id: Optional[str] = None
    to_token_id: Optional[str] = None
    from_amount: Optional[str] = None
    to_amount_estimate: Optional[str] = None
    to_amount_actual: Optional[str] = None

    pool_id: Optional[str] = None
    token_id: Optional[str] = None
    amount: Optional[str] = None


class UnsettledOperationsResponse(BaseModel):
    operations: list[UnsettledOperation]
