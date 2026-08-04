from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional, TypeVar

from eth_account import Account
from privana import (
    DepositAddressRequest,
    DepositCheckRequest,
    PrivanaClient,
    SignWithdrawParams,
    WithdrawalRequest,
    WithdrawMessage,
    sign_withdraw_message,
)
from privana.client.errors import NetworkError
from privana.types.common import Network

from src.clients.defillama import DefiLlamaClient
from src.clients.midas import MidasClient
from src.clients.privana import (
    get_authenticated_privana_client,
    get_privana_client,
)
from src.core.config import load_settings
from src.services.earn.strategies.base import ApyPoint, BaseStrategy
from src.services.earn.strategies.defillama_history import defillama_apy_history

logger = logging.getLogger(__name__)

T = TypeVar("T")

_NETWORK_BY_CHAIN_ID: dict[int, Network] = {
    23295: "testnet",
    23294: "mainnet",
}

DEFAULT_POLL_INTERVAL_SEC = 3.0
DEFAULT_MAX_BRIDGE_POLL_ATTEMPTS = 200

_ACCEPTED_SUBMISSION_STATUSES = frozenset({"success", "pending", "accepted", "ok", "submitted"})

_USDC_DECIMALS = 6
_MTBILL_DECIMALS = 18
_DECIMAL_BALANCE = _MTBILL_DECIMALS - _USDC_DECIMALS


class MidasInstantUnavailableError(RuntimeError):
    """Raised when `redeemInstant` reverts. The likely causes are the daily
    instant limit being exhausted or the swapper having no liquidity at the
    moment. Surfaces to callers as a transient condition so the API layer
    can return a structured 409 ("retry later") rather than a 500.
    """


def _network_for_chain(chain_id: int) -> Network:
    network = _NETWORK_BY_CHAIN_ID.get(chain_id)
    if network is None:
        raise ValueError(
            f"MidasStrategy: unsupported accounting chain_id={chain_id}; "
            f"expected one of {sorted(_NETWORK_BY_CHAIN_ID)}"
        )
    return network


class MidasStrategy(BaseStrategy):
    """Midas mTBILL strategy. Bridges pool USDC from the privana accounting
    layer on Sapphire to the LP EOA on Base, mints mTBILL via the Midas
    Issuance Vault, and redeems via the Instant Redemption Vault on the way
    out.

    For v1 this strategy uses ONLY the `redeemInstant` path. When the daily
    instant limit is exhausted, `redeem_instant` reverts and this strategy
    raises `MidasInstantUnavailableError`. The async `redeemRequest` path is
    intentionally not implemented because handling it would require an
    end-to-end async withdrawal flow (a request can sit pending for hours
    while a Midas operator approves it). Holding a shared withdraw lock that
    long would block every other pool's withdrawals.

    The headline APY is admin-managed via the ``MIDAS_APY_BPS`` setting.
    mTBILL yield is realised as price appreciation against USD, not as a
    token-balance accrual, so the live APY is not derivable from a single
    on-chain read. The configured value is used for ``/v1/earn/pools``
    display only and has no impact on routing or share math.

    Historical APY, when a DefiLlama pool is configured, comes from
    DefiLlama's mTBILL series (mirrors AaveStrategy). It is sourced
    independently of ``MIDAS_APY_BPS``, so the chart's latest point and the
    headline value may diverge; that is accepted — the chart is decoration,
    the headline is the offered rate. Absent a configured pool there is no
    history and ``get_apy_history`` returns an empty list.

    `convert_usdc_to_mtbill_amount` and `convert_mtbill_to_usdc_amount` are
    staticmethods so they can be unit-tested without constructing a
    strategy instance.
    """

    def __init__(
        self,
        client: MidasClient,
        asset_address: str,
        token_id: str,
        pool_address: Optional[str] = None,
        privana_client: Optional[PrivanaClient] = None,
        slippage_bps: Optional[int] = None,
        oracle_heartbeat_sec: Optional[int] = None,
        apy_bps: Optional[int] = None,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        max_bridge_poll_attempts: int = DEFAULT_MAX_BRIDGE_POLL_ATTEMPTS,
        defillama_pool_id: Optional[str] = None,
        defillama_client: Optional[DefiLlamaClient] = None,
    ) -> None:
        self._client = client
        self._asset_address = asset_address
        self._token_id = token_id
        # mTBILL exposes no APY on-chain at all: yield is price appreciation,
        # so a rate only exists once you sample the oracle over time. Absent a
        # configured DefiLlama pool we have no history, and get_apy_history
        # says so.
        self._defillama_pool_id = defillama_pool_id
        self._defillama = defillama_client

        settings = load_settings()
        self._pool_address = pool_address or settings.liquidity_provider_address
        self._lp_secret_key = settings.liquidity_provider_secret_key
        self._accounting_contract = settings.accounting_contract_address
        self._network = _network_for_chain(settings.accounting_chain_id)
        self._slippage_bps = (
            slippage_bps if slippage_bps is not None else settings.midas_default_slippage_bps
        )
        self._oracle_heartbeat_sec = (
            oracle_heartbeat_sec
            if oracle_heartbeat_sec is not None
            else settings.midas_oracle_heartbeat_sec
        )
        self._apy_bps = apy_bps if apy_bps is not None else settings.midas_apy_bps

        self._privana = privana_client
        self._poll_interval_sec = poll_interval_sec
        self._max_bridge_poll_attempts = max_bridge_poll_attempts

    @property
    def name(self) -> str:
        return "midas-mtbill"

    @property
    def asset_address(self) -> str:
        return self._asset_address

    @property
    def token_id(self) -> str:
        return self._token_id

    @property
    def pool_address(self) -> str:
        return self._pool_address

    def _get_privana(self) -> PrivanaClient:
        if self._privana is not None:
            return self._privana
        return get_privana_client()

    async def _get_authed_privana(self) -> PrivanaClient:
        if self._privana is not None:
            return self._privana
        return await get_authenticated_privana_client()

    async def get_apy_bps(self) -> int:
        return self._apy_bps

    async def get_apy_history(self, days: Optional[int] = None) -> list[ApyPoint]:
        return await defillama_apy_history(
            self._defillama_pool_id, self._defillama, days, log_label="MidasStrategy",
        )

    @staticmethod
    def convert_usdc_to_mtbill_amount(
        usdc_amount: int,
        oracle_price: int,
        oracle_decimals: int,
    ) -> int:
        """Convert USDC base units (6 decimals) to the equivalent mTBILL
        base units (18 decimals) at the given MTBILL/USD oracle price.

        Conceptual math:
            usdc_in_usd            = usdc_amount   / 10^6
            mtbill_in_usd_per_unit = oracle_price  / 10^oracle_decimals
            mtbill_in_usd          = usdc_in_usd / mtbill_in_usd_per_unit
            mtbill_base_units      = mtbill_in_usd * 10^18

        Reduced to integer ops, this is:
            mtbill_base_units = usdc_amount * 10^(oracle_decimals + 12) / oracle_price

        The +12 comes from balancing decimals on both sides of the equation:
            18 (mTBILL) - 6 (USDC) + oracle_decimals = oracle_decimals + 12

        Multiplication happens before division to preserve precision; Python
        int has arbitrary precision so there is no overflow risk at any
        realistic deposit size. ZeroDivisionError on a zero oracle price
        is left to propagate — that condition should already be caught by
        is_healthy() upstream.
        """
        scale = 10 ** (oracle_decimals + _DECIMAL_BALANCE)
        return (usdc_amount * scale) // oracle_price

    @staticmethod
    def convert_mtbill_to_usdc_amount(
        mtbill_amount: int,
        oracle_price: int,
        oracle_decimals: int,
    ) -> int:
        """Inverse of convert_usdc_to_mtbill_amount. Converts mTBILL base
        units (18 decimals) to USDC base units (6 decimals).

        Math:
            usdc_base_units = mtbill_amount * oracle_price
                              / 10^(oracle_decimals + 12)

        The +12 is the same decimals-balance term as the inverse:
            18 (mTBILL) - 6 (USDC) = 12
        """
        scale = 10 ** (oracle_decimals + _DECIMAL_BALANCE)
        return (mtbill_amount * oracle_price) // scale

    async def deposit_to_earn(self, amount: int) -> None:
        """Bridge `amount` USDC from accounting on Sapphire to the LP EOA on
        Base, then mint mTBILL via the Midas Issuance Vault.

        Steps:
          1. Bridge USDC via privana request_withdrawal (mirrors AaveStrategy).
          2. Approve the Issuance Vault if allowance is short.
          3. Price the deposit: read oracle, compute expected mTBILL out,
             apply slippage tolerance to derive min_receive_amount.
          4. depositInstant(USDC, amount, min_receive, referrerId=0). mTBILL
             is minted to the LP EOA on success; vault sweeps USDC to its
             configured tokensReceiver atomically.
        """
        if amount <= 0:
            raise ValueError(f"deposit_to_earn requires a positive amount, got {amount}")

        await self._bridge_to_base(amount)

        allowance = await asyncio.to_thread(
            self._client.get_allowance,
            self._asset_address,
            self._client.issuance_vault_address,
        )
        if allowance < amount:
            logger.info(
                "MidasStrategy.deposit_to_earn: topping up allowance asset=%s current=%d needed=%d",
                self._asset_address, allowance, amount,
            )
            await asyncio.to_thread(
                self._client.approve,
                self._asset_address,
                self._client.issuance_vault_address,
                amount,
            )

        price, decimals = await asyncio.to_thread(self._read_oracle_price)
        expected_mtbill = self.convert_usdc_to_mtbill_amount(amount, price, decimals)
        min_receive = expected_mtbill * (10_000 - self._slippage_bps) // 10_000

        tx_hash = await asyncio.to_thread(
            self._client.deposit_instant,
            self._asset_address,
            amount,
            min_receive,
        )
        logger.info(
            "MidasStrategy.deposit_to_earn: minted via Midas asset=%s amount=%d "
            "expected_mtbill=%d min_receive=%d tx=%s",
            self._asset_address, amount, expected_mtbill, min_receive, tx_hash,
        )

    async def withdraw_from_earn(self, amount: int) -> None:
        """Redeem the mTBILL equivalent of `amount` USDC via the Instant
        Redemption Vault, then forward USDC back to the accounting deposit
        address on Base.

        Steps:
          1. Snapshot the pool's accounting balance (for the credit poll).
          2. Read oracle and the redemption-side instantFee. Compute the
             mTBILL amount to redeem, including a fee-rate buffer so that
             post-fee USDC out >= target. Compute min_receive_usdc.
          3. redeemInstant(USDC, mtbill_in, min_receive_usdc). On revert
             (daily limit, swapper out of liquidity, paused) raise
             MidasInstantUnavailableError; the API layer surfaces this as
             a 409.
          4. ERC20.transfer USDC to the pool's per-account deposit address.
          5. Best-effort check_deposit nudge; relay auto-pickup is the
             ultimate source of truth.
          6. Poll get_balance until the credit is observed. State-based,
             no wall-clock timeout — matches the AaveStrategy contract.
        """
        if amount <= 0:
            raise ValueError(f"withdraw_from_earn requires a positive amount, got {amount}")

        client = await self._get_authed_privana()
        pre_balance = await self._read_pool_balance()

        price, decimals = await asyncio.to_thread(self._read_oracle_price)
        fee_bps = await asyncio.to_thread(self._client.get_redemption_instant_fee_bps)
        baseline_mtbill = self.convert_usdc_to_mtbill_amount(amount, price, decimals)
        mtbill_to_redeem = baseline_mtbill * (10_000 + fee_bps) // 10_000
        min_receive_usdc = amount * (10_000 - self._slippage_bps) // 10_000

        lp_usdc_before = await asyncio.to_thread(
            self._client.get_erc20_balance, self._asset_address,
        )

        try:
            redeem_tx = await asyncio.to_thread(
                self._client.redeem_instant,
                self._asset_address,
                mtbill_to_redeem,
                min_receive_usdc,
            )
        except RuntimeError as exc:
            raise MidasInstantUnavailableError(
                f"Midas redeemInstant unavailable (target_usdc={amount} "
                f"mtbill_in={mtbill_to_redeem}): {exc}"
            ) from exc

        lp_usdc_after = await asyncio.to_thread(
            self._client.get_erc20_balance, self._asset_address,
        )
        realized_usdc = lp_usdc_after - lp_usdc_before
        if realized_usdc <= 0:
            raise MidasInstantUnavailableError(
                f"Midas redeemInstant produced no USDC (target_usdc={amount} "
                f"mtbill_in={mtbill_to_redeem} before={lp_usdc_before} after={lp_usdc_after})"
            )

        logger.info(
            "MidasStrategy.withdraw_from_earn: redeemed via Midas mtbill_in=%d "
            "min_usdc=%d realized_usdc=%d tx=%s",
            mtbill_to_redeem, min_receive_usdc, realized_usdc, redeem_tx,
        )

        deposit = await client.get_deposit_address(DepositAddressRequest(chain_type="evm"))

        transfer_tx = await asyncio.to_thread(
            self._client.transfer_erc20,
            self._asset_address,
            deposit.deposit_address,
            realized_usdc,
        )
        logger.info(
            "MidasStrategy.withdraw_from_earn: forwarded to deposit_address=%s amount=%d tx=%s",
            deposit.deposit_address, realized_usdc, transfer_tx,
        )

        try:
            check = await client.check_deposit(
                DepositCheckRequest(
                    chain_id=self._client.w3.eth.chain_id,
                    tx_hash=transfer_tx,
                    amount=realized_usdc,
                )
            )
            if check.status == "error":
                logger.warning(
                    "MidasStrategy.withdraw_from_earn: check_deposit reported error: %s; "
                    "relying on relay auto-pickup",
                    check.detail,
                )
        except Exception as exc:
            logger.warning(
                "MidasStrategy.withdraw_from_earn: check_deposit nudge failed (%s); "
                "relying on relay auto-pickup",
                exc,
            )

        target_balance = pre_balance + realized_usdc
        await self._poll_until_balance_at_least(target_balance)
        logger.info(
            "MidasStrategy.withdraw_from_earn: pool balance credited pool=%s token=%s amount=%d",
            self._pool_address, self._token_id, realized_usdc,
        )

    async def total_assets(self) -> int:
        """Live AUM held by the pool address, in USDC base units. mTBILL
        balance times the oracle price. Returns 0 when the pool holds no
        mTBILL so callers fall back to the on-chain pool snapshot.
        """
        mtbill_bal = await asyncio.to_thread(
            self._client.get_mtbill_balance, self._pool_address,
        )
        if mtbill_bal == 0:
            return 0
        price, decimals = await asyncio.to_thread(self._read_oracle_price)
        return self.convert_mtbill_to_usdc_amount(mtbill_bal, price, decimals)

    async def is_healthy(self) -> bool:
        """Refuses routing when:
          1. The Issuance Vault is paused.
          2. The Redemption Vault is paused.
          3. The oracle has not been updated within 2x the configured
             heartbeat (Chronicle MTBILL/USD heartbeat is ~24h on Base).
          4. Any RPC failure occurs while probing the above.
        """
        try:
            if await asyncio.to_thread(self._client.is_issuance_paused):
                logger.warning("MidasStrategy.is_healthy: issuance vault paused")
                return False
            if await asyncio.to_thread(self._client.is_redemption_paused):
                logger.warning("MidasStrategy.is_healthy: redemption vault paused")
                return False
            _, updated_at = await asyncio.to_thread(self._client.get_oracle_round)
            now = int(time.time())
            if now - updated_at > 2 * self._oracle_heartbeat_sec:
                logger.warning(
                    "MidasStrategy.is_healthy: oracle stale updated_at=%d age=%ds",
                    updated_at, now - updated_at,
                )
                return False
            return True
        except Exception as exc:
            logger.warning("MidasStrategy.is_healthy: probe failed err=%s", exc)
            return False

    def _read_oracle_price(self) -> tuple[int, int]:
        """Two synchronous oracle reads bundled into one helper so a single
        ``to_thread`` covers both. Decimals doesn't change between rounds
        for a given oracle, so this isn't atomic-snapshot critical.
        """
        return self._client.get_oracle_answer(), self._client.get_oracle_decimals()

    async def _retry_on_network_error(
        self,
        op: str,
        factory: Callable[[], Awaitable[T]],
    ) -> T:
        """Mirrors AaveStrategy._retry_on_network_error. Idempotent SDK
        reads that may suffer transient TCP drops loop on NetworkError so
        the bridge polling loop honours its "state-based, no timeout"
        contract.
        """
        while True:
            try:
                return await factory()
            except NetworkError as exc:
                logger.warning(
                    "MidasStrategy.%s: transient network error, retrying after %.1fs (%s)",
                    op, self._poll_interval_sec, exc,
                )
                await asyncio.sleep(self._poll_interval_sec)

    async def _bridge_to_base(self, amount: int) -> None:
        """Submit an accounting Withdraw signed by the LP key and block
        until accounting reports the request resolved. Mirrors
        AaveStrategy._bridge_to_base verbatim; the underlying flow is
        protocol-agnostic. When both strategies are stable this can be
        lifted into a shared helper module.
        """
        client = self._get_privana()
        lp_account = Account.from_key(self._lp_secret_key)

        pre = await self._retry_on_network_error(
            "get_pending_withdrawals",
            lambda: client.get_pending_withdrawals(self._pool_address),
        )
        pre_indices: set[int] = {w.index for w in pre.pending_withdrawals}

        nonce_resp = await self._retry_on_network_error(
            "get_withdrawal_nonce",
            lambda: client.get_withdrawal_nonce(self._pool_address),
        )
        nonce = nonce_resp.nonce

        signature = sign_withdraw_message(
            SignWithdrawParams(
                account=lp_account,
                network=self._network,
                verifying_contract=self._accounting_contract,
                message=WithdrawMessage(
                    token_id=self._token_id,
                    amount=amount,
                    nonce=nonce,
                ),
            )
        )
        submission = await client.request_withdrawal(
            WithdrawalRequest(
                token_id=self._token_id,
                amount=amount,
                nonce=nonce,
                signature=signature,
            )
        )
        logger.info(
            "MidasStrategy._bridge_to_base: requested withdrawal pool=%s token=%s "
            "amount=%d nonce=%d status=%s detail=%s",
            self._pool_address, self._token_id, amount, nonce,
            submission.status, submission.detail,
        )
        if submission.status not in _ACCEPTED_SUBMISSION_STATUSES:
            raise RuntimeError(
                f"Withdrawal request rejected: status={submission.status} "
                f"detail={submission.detail}"
            )

        own_index: Optional[int] = None
        attempts = 0
        while True:
            attempts += 1
            if attempts > self._max_bridge_poll_attempts:
                raise RuntimeError(
                    f"MidasStrategy._bridge_to_base: withdrawal unresolved after "
                    f"{self._max_bridge_poll_attempts} polls (pool={self._pool_address} "
                    f"token={self._token_id} amount={amount}); aborting to release lock"
                )
            pending = await self._retry_on_network_error(
                "get_pending_withdrawals",
                lambda: client.get_pending_withdrawals(self._pool_address),
            )
            current_indices = {w.index for w in pending.pending_withdrawals}

            if own_index is None:
                new_indices = current_indices - pre_indices
                if new_indices:
                    own_index = min(new_indices)

            if own_index is not None:
                idx = own_index
                info = await self._retry_on_network_error(
                    "get_withdrawal_info",
                    lambda: client.get_withdrawal_info(idx),
                )
                if info.resolved:
                    logger.info(
                        "MidasStrategy._bridge_to_base: withdrawal resolved index=%d tx=%s",
                        own_index, info.tx_identifier,
                    )
                    return

            await asyncio.sleep(self._poll_interval_sec)

    async def _read_pool_balance(self) -> int:
        client = await self._get_authed_privana()
        balance = await self._retry_on_network_error(
            "get_balance",
            lambda: client.get_balance(self._token_id),
        )
        return int(balance.balance)

    async def _poll_until_balance_at_least(self, target_balance: int) -> None:
        while True:
            current = await self._read_pool_balance()
            if current >= target_balance:
                return
            await asyncio.sleep(self._poll_interval_sec)


__all__ = ["MidasStrategy", "MidasInstantUnavailableError"]
