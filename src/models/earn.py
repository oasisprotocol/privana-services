from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class PoolStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"


class TransactionType(str, Enum):
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"


class PoolRecord(BaseModel):
    id: str
    token_id: str
    strategy: str
    total_shares: str
    total_assets: str
    pool_address: str
    apy_bps: int
    status: str
    created_at: int
    updated_at: int


class DepositRecord(BaseModel):
    id: str
    pool_id: str
    user_address: str
    shares: str
    total_deposited: str
    total_withdrawn: str
    created_at: int
    updated_at: int


class TransactionRecord(BaseModel):
    id: str
    pool_id: str
    user_address: str
    type: str
    amount: str
    shares: str
    exchange_rate: str
    tx_hash: Optional[str] = None
    status: str
    created_at: int
    updated_at: int


class PoolResponse(BaseModel):
    pool_id: str
    token_id: str
    strategy: str
    total_assets: str
    apy_bps: int
    status: str
    pool_address: str


class PoolDetailResponse(BaseModel):
    pool_id: str
    token_id: str
    strategy: str
    total_shares: str
    total_assets: str
    pool_address: str
    apy_bps: int
    status: str
    created_at: int


class PoolListResponse(BaseModel):
    pools: list[PoolResponse]


class ApyHistoryPoint(BaseModel):
    timestamp: int = Field(..., description="Unix seconds")
    apy_bps: int = Field(..., description="APY in basis points (500 = 5%), as in PoolResponse")


class ApyHistoryResponse(BaseModel):
    pool_id: str
    # Oldest first. Empty when the pool's strategy has no historical source, which
    # is a normal state — clients render no chart rather than treating it as an error.
    points: list[ApyHistoryPoint]


class DepositQuoteResponse(BaseModel):
    quote_id: str
    pool_id: str
    token_id: str
    amount: str
    shares_estimate: str
    exchange_rate: str
    pool_address: str
    transfer_nonce: int
    expires_at: int


class DepositRequest(BaseModel):
    pool_id: str = Field(..., description="Earn pool ID")
    user_address: str = Field(..., description="User wallet address")
    amount: str = Field(..., description="Amount in base units")
    nonce: int = Field(..., description="Transfer nonce for input token")
    signature: str = Field(..., description="EIP-712 transfer signature from user")


class DepositResponse(BaseModel):
    # Same value GET /v1/operations/unsettled reports as operation_id, not a tx hash.
    deposit_id: str
    pool_id: str
    amount: str
    shares_minted: Optional[str] = None
    exchange_rate: Optional[str] = None
    tx_hash: Optional[str] = None
    status: str
    error: Optional[str] = None


class WithdrawRequest(BaseModel):
    pool_id: str = Field(..., description="Earn pool ID")
    user_address: str = Field(..., description="User wallet address")
    amount: str = Field(..., description="Amount to withdraw in base units")
    nonce: int = Field(
        ...,
        description=(
            "User withdraw consent nonce, must equal "
            "EarnManager.withdrawNonces[user_address] at submission time."
        ),
    )
    signature: str = Field(
        ...,
        description=(
            "EIP-712 Withdraw(user, poolId, amount, nonce) signature in the "
            "EarnManager domain. Authorizes burning the user's pool shares."
        ),
    )


class WithdrawResponse(BaseModel):
    # Same value GET /v1/operations/unsettled reports as operation_id, not a tx hash.
    withdraw_id: str
    pool_id: str
    amount: str
    shares_burned: Optional[str] = None
    exchange_rate: Optional[str] = None
    tx_hash: Optional[str] = None
    status: str
    error: Optional[str] = None


class BalanceResponse(BaseModel):
    pool_id: str
    token_id: str
    shares: str
    underlying_amount: str
    exchange_rate: str
    # Yield-only 24h change (EA-Products #168). Null hides the badge; never a
    # fabricated zero.
    change_24h: Optional[str] = Field(
        default=None,
        description=(
            "Yield earned over the last ~24h, in token base units, as a signed "
            "decimal string. Null when it cannot be computed honestly, in which "
            "case the client shows no badge rather than a zero."
        ),
    )
    change_24h_pct: Optional[str] = Field(
        default=None,
        description=(
            "change_24h as a fraction of the position's value at the start of "
            'the window, as a signed decimal string. A fraction, not percent: '
            '"0.008" means 0.8%, so multiply by 100 to display.'
        ),
    )
    # Accrued yield on the shares still held. Null whenever
    # status is not "ok", so the UI shows a dash rather than a made-up zero.
    earned_active: Optional[str] = None
    earned_active_status: Literal[
        "ok", "ledger_incomplete", "pending_settlement", "unsupported"
    ] = "unsupported"
    cost_basis: Optional[str] = None
    deposit_count: int = 0
    first_deposit_at: Optional[int] = None

    @model_validator(mode="after")
    def _earned_value_matches_status(self) -> "BalanceResponse":
        # A figure alongside a non-ok status, or a missing one alongside "ok",
        # would tell the UI to render something the backend cannot stand
        # behind. Fail loudly here rather than shipping either.
        if (self.earned_active is None) != (self.earned_active_status != "ok"):
            raise ValueError(
                "earned_active must be set if and only if "
                "earned_active_status is 'ok'"
            )
        return self


class BalanceListResponse(BaseModel):
    positions: list[BalanceResponse]


class TransactionResponse(BaseModel):
    transaction_id: str
    pool_id: str
    type: str
    amount: str
    shares: str
    exchange_rate: str
    tx_hash: Optional[str] = None
    status: str
    created_at: int


class TransactionListResponse(BaseModel):
    transactions: list[TransactionResponse]
