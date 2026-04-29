from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from eth_account import Account
from flexvaults import (
    FlexvaultsClient,
    SignWithdrawParams,
    WithdrawalRequest,
    WithdrawMessage,
    sign_withdraw_message,
)
from flexvaults.types.common import Network

from src.clients.aave import AaveClient
from src.clients.flexvaults import get_flexvaults_client
from src.core.config import load_settings
from src.services.earn.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


_NETWORK_BY_CHAIN_ID: dict[int, Network] = {
    23295: "testnet",
    23294: "mainnet",
}

DEFAULT_WITHDRAWAL_POLL_INTERVAL_SEC = 3.0
DEFAULT_WITHDRAWAL_TIMEOUT_SEC = 90.0


def _network_for_chain(chain_id: int) -> Network:
    network = _NETWORK_BY_CHAIN_ID.get(chain_id)
    if network is None:
        raise ValueError(
            f"AaveStrategy: unsupported accounting chain_id={chain_id}; "
            f"expected one of {sorted(_NETWORK_BY_CHAIN_ID)}"
        )
    return network


class AaveStrategy(BaseStrategy):
    """Aave V3 strategy. Bridges pool funds from the flexvaults accounting
    layer on Sapphire to the LP EOA on Base, supplies them to Aave V3, and
    redeems on the way out.

    Cross-chain bridging uses the flexvaults SDK: `request_withdrawal`
    submits an EIP-712 signed Withdraw message to accounting, which then
    relays the funds to the same address on Base. Polling
    `get_pending_withdrawals` resolves the request before we proceed to
    the Aave-side `supply` call.

    Per-pool params (`asset_address`, `token_id`, optional `pool_address`)
    are passed at construction time. Cross-pool params (LP key, accounting
    contract, chain id) come from settings so each pool doesn't restate
    them.
    """

    def __init__(
        self,
        client: AaveClient,
        asset_address: str,
        token_id: str,
        pool_address: Optional[str] = None,
        flexvaults_client: Optional[FlexvaultsClient] = None,
        poll_interval_sec: float = DEFAULT_WITHDRAWAL_POLL_INTERVAL_SEC,
        withdrawal_timeout_sec: float = DEFAULT_WITHDRAWAL_TIMEOUT_SEC,
    ) -> None:
        self._client = client
        self._asset_address = asset_address
        self._token_id = token_id

        settings = load_settings()
        self._pool_address = pool_address or settings.liquidity_provider_address
        self._lp_private_key = settings.liquidity_provider_private_key
        self._accounting_contract = settings.accounting_contract_address
        self._network = _network_for_chain(settings.accounting_chain_id)

        self._flexvaults = flexvaults_client
        self._poll_interval_sec = poll_interval_sec
        self._withdrawal_timeout_sec = withdrawal_timeout_sec

    @property
    def name(self) -> str:
        return "aave-v3"

    @property
    def asset_address(self) -> str:
        return self._asset_address

    @property
    def token_id(self) -> str:
        return self._token_id

    @property
    def pool_address(self) -> str:
        return self._pool_address

    def _get_flexvaults(self) -> FlexvaultsClient:
        if self._flexvaults is None:
            self._flexvaults = get_flexvaults_client()
        return self._flexvaults

    async def get_apy_bps(self) -> int:
        return self._client.get_supply_apy_bps(self._asset_address)

    async def deposit_to_earn(self, amount: int) -> None:
        """Bridge `amount` from accounting on Sapphire to the LP EOA on Base,
        then supply it to Aave.

        Steps:
          1. Pull funds: sign Withdraw, call `request_withdrawal`, poll
             pending list until the relay completes the on-chain transfer.
          2. Top up Aave allowance if short.
          3. Call `pool.supply(asset, amount, onBehalfOf=LP)`; aTokens accrue
             yield to the LP from this point on.

        Approval is only refreshed when short. We don't reset to zero
        beforehand because (a) the asset is sitting on our own EOA so
        there's no front-running risk and (b) an extra tx per deposit is
        wasteful.
        """
        if amount <= 0:
            raise ValueError(f"deposit_to_earn requires a positive amount, got {amount}")

        await self._bridge_to_base(amount)

        allowance = self._client.get_allowance(self._asset_address)
        if allowance < amount:
            logger.info(
                "AaveStrategy.deposit_to_earn: topping up allowance asset=%s current=%d needed=%d",
                self._asset_address, allowance, amount,
            )
            self._client.approve_pool(self._asset_address, amount)

        tx_hash = self._client.supply(self._asset_address, amount)
        logger.info(
            "AaveStrategy.deposit_to_earn: supplied asset=%s amount=%d tx=%s",
            self._asset_address, amount, tx_hash,
        )

    async def _bridge_to_base(self, amount: int) -> None:
        """Trigger an accounting withdrawal that delivers `amount` of the
        underlying asset to `pool_address` on Base, and block until the relay
        confirms. Raises on timeout, signature rejection, or relay failure.
        """
        client = self._get_flexvaults()
        lp_account = Account.from_key(self._lp_private_key)

        pre = await client.get_pending_withdrawals(self._pool_address)
        pre_indices: set[int] = {w.index for w in pre.withdrawals}

        nonce_resp = await client.get_transfer_nonce(self._pool_address)
        nonce = nonce_resp.nonce

        signature = sign_withdraw_message(
            SignWithdrawParams(
                account=lp_account,
                network=self._network,
                verifying_contract=self._accounting_contract,
                message=WithdrawMessage(
                    user_address=self._pool_address,
                    token_id=self._token_id,
                    amount=amount,
                    nonce=nonce,
                ),
            )
        )
        await client.request_withdrawal(
            WithdrawalRequest(
                user_address=self._pool_address,
                token_id=self._token_id,
                amount=amount,
                nonce=nonce,
                signature=signature,
            )
        )
        logger.info(
            "AaveStrategy._bridge_to_base: requested withdrawal pool=%s token=%s amount=%d nonce=%d",
            self._pool_address, self._token_id, amount, nonce,
        )

        deadline = time.monotonic() + self._withdrawal_timeout_sec
        own_index: Optional[int] = None

        while time.monotonic() < deadline:
            pending = await client.get_pending_withdrawals(self._pool_address)
            current_indices = {w.index for w in pending.withdrawals}

            if own_index is None:
                new_indices = current_indices - pre_indices
                if new_indices:
                    own_index = min(new_indices)

            if own_index is not None and own_index not in current_indices:
                info = await client.get_withdrawal_info(own_index)
                if info.status == "completed":
                    logger.info(
                        "AaveStrategy._bridge_to_base: withdrawal completed index=%d tx=%s",
                        own_index, info.transaction_hash,
                    )
                    return
                raise RuntimeError(
                    f"AaveStrategy._bridge_to_base: withdrawal {own_index} resolved with "
                    f"status={info.status}"
                )

            await asyncio.sleep(self._poll_interval_sec)

        raise TimeoutError(
            f"AaveStrategy._bridge_to_base: withdrawal did not complete within "
            f"{self._withdrawal_timeout_sec}s (pool={self._pool_address}, amount={amount})"
        )

    async def withdraw_from_earn(self, amount: int) -> None:
        """Redeem `amount` of the underlying asset from Aave. Funds return to
        the LP EOA on Base.

        Re-depositing them into the flexvaults accounting layer on Sapphire
        is added in a follow-up commit; this method currently only handles
        the Aave side.
        """
        if amount <= 0:
            raise ValueError(f"withdraw_from_earn requires a positive amount, got {amount}")

        tx_hash = self._client.withdraw(self._asset_address, amount)
        logger.info(
            "AaveStrategy.withdraw_from_earn: redeemed asset=%s amount=%d tx=%s",
            self._asset_address, amount, tx_hash,
        )

    async def pending_yield(self) -> int:
        return 0

    async def total_assets(self) -> int:
        """aToken balance held by the pool address for this asset, which
        equals principal plus accrued Aave yield.
        """
        return self._client.get_aToken_balance(
            self._asset_address, self._pool_address,
        )

    async def is_healthy(self) -> bool:
        """Treat a successful supply-rate read as a cheap liveness proxy. If
        getReserveData throws, the pool is unreachable or the asset isn't
        listed; either way, don't route deposits here.
        """
        try:
            self._client.get_supply_apy_bps(self._asset_address)
            return True
        except Exception as exc:
            logger.warning(
                "AaveStrategy.is_healthy: probe failed asset=%s err=%s",
                self._asset_address, exc,
            )
            return False
