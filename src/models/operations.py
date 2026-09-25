from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import BaseModel, field_validator


class OperationStage(BaseModel):
    # recording, bridging, deploying, reclaiming, returning, finality, paying_out
    stage: str
    at: int
    # finality carries {confirmations, required}
    detail: Optional[dict[str, Any]] = None


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
    # The nonce the user signed the request with: the one key a client holds
    # before it has learned the operation id (e.g. the submit response was lost).
    nonce: Optional[str] = None
    # Steps an earn operation has reached so far, oldest first. Empty for swaps.
    stages: list[OperationStage] = []

    @field_validator("stages", mode="before")
    @classmethod
    def _parse_stages(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return json.loads(value)
        return value


class UnsettledOperationsResponse(BaseModel):
    operations: list[UnsettledOperation]


class OperationsResponse(BaseModel):
    operations: list[UnsettledOperation]
    # Pass back as ``before`` to fetch the next (older) page; null on the last page.
    next_cursor: Optional[str] = None
