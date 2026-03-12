from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class TokenInfo(BaseModel):
    token_id: str
    token_type: int
    token_type_name: str
    data: str
    chain_id: Optional[int] = None
    chain_name: Optional[str] = None
    token_address: Optional[str] = None


class Balance(BaseModel):
    user_address: str
    token_id: str
    balance: str
    token_symbol: Optional[str] = None
    chain_id: Optional[str] = None


class LockInfo(BaseModel):
    lock_id: int
    user_address: str
    service_address: str
    token_id: str
    amount: int
    expiry: int
    is_expired: bool = False


class LockedFundsResponse(BaseModel):
    user_address: str
    service_address: Optional[str] = None
    locks: list[LockInfo]
    total_locked: int


class SubmissionResponse(BaseModel):
    submission_id: str
    status: str
    detail: Optional[str] = None
