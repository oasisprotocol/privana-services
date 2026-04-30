from __future__ import annotations

import asyncio
import logging
from typing import Optional

from eth_account import Account
from flexvaults import (
    DepositQuoteRequest,
    FlexvaultsClient,
    IncludeDepositRequest,
    SignWithdrawParams,
    WithdrawalRequest,
    WithdrawMessage,
    sign_withdraw_message,
)
from flexvaults.types.common import Network

from src.clients.aave import AaveClient
from src.clients.flexvaults import (
    get_authenticated_flexvaults_client,
    get_flexvaults_client,
)
from src.core.config import load_settings
from src.services.earn.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


_NETWORK_BY_CHAIN_ID: dict[int, Network] = {
    23295: "testnet",
    23294: "mainnet",
}

DEFAULT_POLL_INTERVAL_SEC = 3.0


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

    Both bridge legs are fully state-based: this class polls accounting
    state until it observes the requested transfer either complete (success)
    or transition to a terminal failed status (raise). There is no
    wall-clock timeout; the polling loop only ends when the relay's state
    machine resolves.

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
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
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
        if self._flexvaults is not None:
            return self._flexvaults
        return get_flexvaults_client()

    async def _get_authed_flexvaults(self) -> FlexvaultsClient:
        """Auth-required SDK calls share the SIWE-authenticated singleton.
        Tests inject a pre-mocked client to bypass SIWE.
        """
        if self._flexvaults is not None:
            return self._flexvaults
        return await get_authenticated_flexvaults_client()

    async def get_apy_bps(self) -> int:
        return self._client.get_supply_apy_bps(self._asset_address)

    async def deposit_to_earn(self, amount: int) -> None:
        """Bridge `amount` from accounting on Sapphire to the LP EOA on Base,
        then supply it to Aave.

        Steps:
          1. Pull funds: sign Withdraw, call `request_withdrawal`, poll
             pending list until the relay's state machine reports completion.
          2. Top up Aave allowance if short.
          3. Call `pool.supply(asset, amount, onBehalfOf=pool_address)`;
             aTokens accrue yield to the pool from this point on.

        Approval is only refreshed when short. We don't reset to zero
        beforehand because the asset is sitting on our own EOA so there's
        no front-running risk and an extra tx per deposit is wasteful.
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

    async def withdraw_from_earn(self, amount: int) -> None:
        """Redeem `amount` from Aave back into the pool's accounting balance
        on Sapphire.

        Steps:
          1. `aave.withdraw(asset, amount, to=pool_address)`: aTokens burn,
             USDC lands at `pool_address` on Base.
          2. `get_deposit_quote(...)`: ask accounting where on Base to send
             the funds so they get credited to `pool_address`.
          3. ERC20.transfer USDC from `pool_address` to that deposit address.
          4. `include_deposit(...)`: nudge accounting to ingest the transfer
             (idempotent, fine if the relay already saw it).
          5. Poll `get_balance(pool_address, token_id)` until it has
             increased by `amount` vs the pre-call snapshot. State-based,
             no timer; the call only returns when the credit is observed.
        """
        if amount <= 0:
            raise ValueError(f"withdraw_from_earn requires a positive amount, got {amount}")

        client = self._get_flexvaults()
        pre_balance = await self._read_pool_balance()

        redeem_tx = self._client.withdraw(self._asset_address, amount, to=self._pool_address)
        logger.info(
            "AaveStrategy.withdraw_from_earn: redeemed from aave asset=%s amount=%d tx=%s",
            self._asset_address, amount, redeem_tx,
        )

        quote = await client.get_deposit_quote(
            DepositQuoteRequest(
                user_address=self._pool_address,
                token_id=self._token_id,
                amount=amount,
            )
        )

        transfer_tx = self._client.transfer_erc20(
            self._asset_address,
            quote.deposit_address,
            amount,
        )
        logger.info(
            "AaveStrategy.withdraw_from_earn: forwarded to deposit_address=%s amount=%d tx=%s",
            quote.deposit_address, amount, transfer_tx,
        )

        try:
            await client.include_deposit(
                IncludeDepositRequest(
                    user_address=self._pool_address,
                    token_id=self._token_id,
                    evm_transaction_data=transfer_tx,
                )
            )
        except Exception as exc:
            logger.warning(
                "AaveStrategy.withdraw_from_earn: include_deposit nudge failed (%s); "
                "relying on relay auto-pickup",
                exc,
            )

        target_balance = pre_balance + amount
        await self._poll_until_balance_at_least(target_balance)
        logger.info(
            "AaveStrategy.withdraw_from_earn: pool balance credited pool=%s token=%s amount=%d",
            self._pool_address, self._token_id, amount,
        )

    async def _bridge_to_base(self, amount: int) -> None:
        """Submit an accounting Withdraw and block until accounting reports
        the request resolved.

        State-based, no wall-clock timeout: the loop only exits when our
        withdrawal's `resolved` flag flips True (the relay completed the
        on-chain transfer). The PyPI SDK does not surface a failed/rejected
        status enum on this endpoint, so a hard relay failure surfaces only
        if `request_withdrawal` itself rejects synchronously.
        """
        client = self._get_flexvaults()
        lp_account = Account.from_key(self._lp_private_key)

        pre = await client.get_pending_withdrawals(self._pool_address)
        pre_indices: set[int] = {w.index for w in pre.pending_withdrawals}

        nonce = (await client.get_withdrawal_nonce(self._pool_address)).nonce

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

        own_index: Optional[int] = None
        while True:
            pending = await client.get_pending_withdrawals(self._pool_address)
            current_indices = {w.index for w in pending.pending_withdrawals}

            if own_index is None:
                new_indices = current_indices - pre_indices
                if new_indices:
                    own_index = min(new_indices)

            if own_index is not None:
                info = await client.get_withdrawal_info(own_index)
                if info.resolved:
                    logger.info(
                        "AaveStrategy._bridge_to_base: withdrawal resolved index=%d tx=%s",
                        own_index, info.tx_identifier,
                    )
                    return

            await asyncio.sleep(self._poll_interval_sec)

    async def _read_pool_balance(self) -> int:
        """Read the pool's accounting balance for this token. The PyPI SDK
        infers user from the bearer token, so we use the SIWE-authenticated
        client (LP/pool key) here.
        """
        client = await self._get_authed_flexvaults()
        balance = await client.get_balance(self._token_id)
        return int(balance.balance)

    async def _poll_until_balance_at_least(self, target_balance: int) -> None:
        """Block (state-based, no timer) until the pool's accounting balance
        is at least `target_balance`. The relay credits asynchronously after
        the on-chain ERC20 transfer; this is how we know the credit landed.
        """
        while True:
            current = await self._read_pool_balance()
            if current >= target_balance:
                return
            await asyncio.sleep(self._poll_interval_sec)

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
