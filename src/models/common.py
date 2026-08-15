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
    symbol: Optional[str] = None
    name: Optional[str] = None
    decimals: Optional[int] = None


class Balance(BaseModel):
    user_address: str
    token_id: str
    balance: str
    token_symbol: Optional[str] = None
    chain_id: Optional[str] = None


class HistoryEntry(BaseModel):
    kind: str
    timestamp: int
    token_id: Optional[str] = None
    amount: Optional[str] = None
    counterparty: Optional[str] = None
    deposit_id: Optional[str] = None
    chain_id: Optional[int] = None
