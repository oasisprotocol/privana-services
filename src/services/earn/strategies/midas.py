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
from privana.client.errors import AccountingApiError, NetworkError
from privana.types.common import Network

from src.clients.defillama import DefiLlamaClient
from src.clients.midas import MidasClient
from src.clients.privana import (
    get_earn_pool_privana_client,
    get_privana_client,
)
from src.core.config import load_settings
from src.services.earn import progress
from src.services.earn.strategies.base import ApyPoint, BaseStrategy, LiquidityUnavailable
from src.services.earn.strategies.defillama_history import defillama_apy_history

logger = logging.getLogger(__name__)

T = TypeVar("T")

_NETWORK_BY_CHAIN_ID: dict[int, Network] = {
    23295: "testnet",
    23294: "mainnet",
}

DEFAULT_POLL_INTERVAL_SEC = 3.0
DEFAULT_MAX_BRIDGE_POLL_ATTEMPTS = 200

# A confirmed redeem receipt does not guarantee the next balanceOf sees it:
# a load-balanced RPC can serve the read from a node still a block behind,
# reporting the pre-redeem balance. Re-read a few times before concluding
# the redeem produced nothing.
REDEEM_BALANCE_POLL_ATTEMPTS = 10
REDEEM_BALANCE_POLL_INTERVAL_SEC = 3.0

_ACCEPTED_SUBMISSION_STATUSES = frozenset({"success", "pending", "accepted", "ok", "submitted"})

_USDC_DECIMALS = 6
_MTBILL_DECIMALS = 18
_DECIMAL_BALANCE = _MTBILL_DECIMALS - _USDC_DECIMALS

# Midas vaults denominate every amountToken / minReceiveAmount argument in
# base-18 units regardless of the token's own decimals. Token-native amounts
# (USDC base units here) must be scaled up before crossing that boundary;
# ERC20 approvals stay in token-native units.
_BASE18_SCALE = 10 ** _DECIMAL_BALANCE


class MidasInstantUnavailableError(RuntimeError):
    """Raised when `redeemInstant` reverts. The likely causes are the daily
    instant limit being exhausted or the swapper having no liquidity at the
    moment. Surfaces to callers as a transient condition so the API layer
    can return a structured 409 ("retry later") rather than a 500.
    """


class MidasLiquidityUnavailable(MidasInstantUnavailableError, LiquidityUnavailable):
    """The instant path cannot take this redeem right now: the daily limit is
    spent or the vault reverted the redeem. Raised only before any funds move."""


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
    layer on Sapphire to the earn account on Base, mints mTBILL via the Midas
    Issuance Vault, and redeems via the Instant Redemption Vault on the way
    out.

    For v1 this strategy uses ONLY the `redeemInstant` path. When the daily
    instant limit is exhausted, `redeem_instant` reverts and this strategy
    raises `MidasInstantUnavailableError`. The async `redeemRequest` path is
    intentionally not implemented because handling it would require an
    end-to-end async withdrawal flow (a request can sit pending for hours
    while a Midas operator approves it). Holding a shared withdraw lock that
    long would block every other pool's withdrawals.

    The headline APY prefers the live DefiLlama rate — the latest point of
    the same mTBILL series behind ``get_apy_history`` — and falls back to the
    admin-set ``MIDAS_APY_BPS`` setting when no DefiLlama pool is configured
    or the fetch fails. mTBILL yield is realised as price appreciation against
    USD, not as a token-balance accrual, so the rate is not derivable from a
    single on-chain read; DefiLlama does the sample-and-annualise for us. The
    value is used for ``/v1/earn/pools`` display only and has no impact on
    routing or share math.

    Historical APY comes from DefiLlama's mTBILL series when a pool is
    configured (mirrors AaveStrategy). Because the headline is that series'
    latest point, chart and headline agree by construction. Absent a
    configured pool there is no history: ``get_apy_history`` returns an empty
    list and the headline falls back to ``MIDAS_APY_BPS``.

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
        self._pool_address = pool_address or settings.earn_pool_address
        self._ep_secret_key = settings.earn_pool_secret_key
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
        return await get_earn_pool_privana_client()

    async def get_apy_bps(self) -> int:
        # Prefer the live DefiLlama rate (the series' latest point); fall back
        # to the admin-set constant when no pool is configured or the fetch
        # fails. Shares the DefiLlama 1h cache with get_apy_history, so calling
        # it here is cheap.
        history = await self.get_apy_history()
        if history:
            return history[-1].apy_bps
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
        round_up: bool = False,
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
        if round_up:
            # Sizing a redeem rounds up: landing a base unit short means the
            # payout that follows cannot be covered.
            return -(-usdc_amount * scale // oracle_price)
        return (usdc_amount * scale) // oracle_price

    @staticmethod
    def redemption_payout(
        mtbill_amount: int, oracle_price: int, oracle_decimals: int, fee_bps: int
    ) -> int:
        """USDC base units the redemption vault pays for ``mtbill_amount``.

        Mirrors the contract: the instant fee is taken in mTBILL, and the
        remainder is converted at the oracle price with the result floored.
        Reproduces the mainnet vault to the base unit.
        """
        net = mtbill_amount - mtbill_amount * fee_bps // 10_000
        return (net * oracle_price) // (10 ** (oracle_decimals + _DECIMAL_BALANCE))

    @classmethod
    def size_redeem(
        cls, usdc_target: int, oracle_price: int, oracle_decimals: int, fee_bps: int
    ) -> int:
        """Smallest mTBILL amount whose vault payout covers ``usdc_target``.

        Starts from the fee-grossed estimate and steps up by one USDC base
        unit of mTBILL until the vault's own payout clears the target, so the
        redeem never asks for a base unit more than it needs.
        """
        gross = usdc_target * 10_000 // (10_000 - fee_bps)
        mtbill = cls.convert_usdc_to_mtbill_amount(
            gross, oracle_price, oracle_decimals, round_up=True
        )
        unit = cls.convert_usdc_to_mtbill_amount(1, oracle_price, oracle_decimals, round_up=True)
        while cls.redemption_payout(mtbill, oracle_price, oracle_decimals, fee_bps) < usdc_target:
            mtbill += unit
        return mtbill

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
        """Bridge `amount` USDC from accounting on Sapphire to the earn account on
        Base, then mint mTBILL via the Midas Issuance Vault.

        Steps:
          1. Bridge USDC via privana request_withdrawal (mirrors AaveStrategy).
          2. Approve the Issuance Vault if allowance is short.
          3. Price the deposit: read oracle, compute expected mTBILL out,
             apply slippage tolerance to derive min_receive_amount.
          4. depositInstant(USDC, amount in base-18, min_receive,
             referrerId=0). mTBILL is minted to the earn account on success; vault
             sweeps USDC to its configured tokensReceiver atomically.
        """
        if amount <= 0:
            raise ValueError(f"deposit_to_earn requires a positive amount, got {amount}")

        await self._await_bridges_in_flight()
        on_hand = await asyncio.to_thread(
            self._client.get_erc20_balance, self._asset_address,
        )
        if on_hand >= amount:
            logger.info(
                "MidasStrategy.deposit_to_earn: %d already on the earn account (balance=%d); "
                "skipping the bridge",
                amount, on_hand,
            )
        else:
            progress.update(progress.BRIDGING)
            await self._bridge_to_base(amount - on_hand)
            on_hand = await asyncio.to_thread(
                self._client.get_erc20_balance, self._asset_address,
            )
        # Everything on the account is pool money, so mint all of it: a bridge
        # a previous deposit gave up on would otherwise sit here earning nothing.
        deploy = max(amount, on_hand)
        progress.update(progress.DEPLOYING)

        allowance = await asyncio.to_thread(
            self._client.get_allowance,
            self._asset_address,
            self._client.issuance_vault_address,
        )
        if allowance < deploy:
            logger.info(
                "MidasStrategy.deposit_to_earn: topping up allowance asset=%s current=%d needed=%d",
                self._asset_address, allowance, deploy,
            )
            await asyncio.to_thread(
                self._client.approve,
                self._asset_address,
                self._client.issuance_vault_address,
                deploy,
            )

        price, decimals = await asyncio.to_thread(self._read_oracle_price)
        expected_mtbill = self.convert_usdc_to_mtbill_amount(deploy, price, decimals)
        min_receive = expected_mtbill * (10_000 - self._slippage_bps) // 10_000

        tx_hash = await asyncio.to_thread(
            self._client.deposit_instant,
            self._asset_address,
            deploy * _BASE18_SCALE,
            min_receive,
        )
        logger.info(
            "MidasStrategy.deposit_to_earn: minted via Midas asset=%s amount=%d "
            "expected_mtbill=%d min_receive=%d tx=%s",
            self._asset_address, deploy, expected_mtbill, min_receive, tx_hash,
        )

    async def withdraw_from_earn(self, amount: int) -> None:
        """Redeem the mTBILL equivalent of `amount` USDC via the Instant
        Redemption Vault, then forward USDC back to the accounting deposit
        address on Base.

        Steps:
          1. Snapshot the pool's accounting balance (for the credit poll).
          2. Read oracle and the redemption-side fee. Compute the
             mTBILL amount to redeem, including a fee-rate buffer so that
             post-fee USDC out >= target. Compute min_receive_usdc in
             base-18. Top up the vault's mTBILL allowance if short.
          3. redeemInstant(USDC, mtbill_in, min_receive_usdc). On revert
             (daily limit, swapper out of liquidity, paused) raise
             MidasInstantUnavailableError; the API layer surfaces this as
             a 409.
          4. ERC20.transfer USDC to the pool's per-account deposit address.
          5. Poll get_balance until the credit is observed, re-sending the
             check_deposit nudge until accounting accepts it (the first
             attempts fail Base finality). State-based, no wall-clock
             timeout — matches the AaveStrategy contract.
        """
        if amount <= 0:
            raise ValueError(f"withdraw_from_earn requires a positive amount, got {amount}")

        progress.update(progress.RECLAIMING)
        pre_balance = await self._read_pool_balance()

        # Anything already sitting raw on the account is pool money that was
        # never minted; spend it before touching the position.
        lp_usdc_before = await asyncio.to_thread(
            self._client.get_erc20_balance, self._asset_address,
        )
        from_raw = min(lp_usdc_before, amount)
        realized_usdc = 0
        if amount > from_raw:
            realized_usdc = await self._redeem(amount - from_raw, lp_usdc_before)
        realized_usdc += from_raw

        # Acquired right before each authed call, not once for the flow: the
        # getter refreshes the bearer token near expiry, and the redeem legs
        # above can outlive a token that was fresh at the start.
        async def _fetch_deposit_address():
            client = await self._get_authed_privana()
            return await client.get_deposit_address(
                DepositAddressRequest(chain_type="evm")
            )

        deposit = await self._retry_on_network_error(
            "get_deposit_address", _fetch_deposit_address
        )

        progress.update(progress.RETURNING)
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

        target_balance = pre_balance + realized_usdc
        await self._poll_until_credited(target_balance, transfer_tx, realized_usdc)
        logger.info(
            "MidasStrategy.withdraw_from_earn: pool balance credited pool=%s token=%s amount=%d",
            self._pool_address, self._token_id, realized_usdc,
        )

    async def _redeem(self, amount: int, lp_usdc_before: int) -> int:
        """Redeem enough mTBILL for `amount` USDC through the Instant
        Redemption Vault and return the USDC that actually arrived.
        """
        price, decimals = await asyncio.to_thread(self._read_oracle_price)
        fee_bps = await asyncio.to_thread(
            self._client.get_redemption_fee_bps, self._asset_address,
        )
        if fee_bps >= 10_000:
            raise MidasInstantUnavailableError(
                f"Midas redemption fee is {fee_bps} bps; refusing to redeem"
            )
        # Sized against the vault's own arithmetic rather than an approximation
        # of it. The redemption vault takes its fee in mTBILL first and then
        # converts what is left, flooring once more on the way out. Sizing in
        # USDC and grossing up for the fee lands one base unit under that on
        # mainnet, and the transfer to the user then cannot be covered.
        mtbill_to_redeem = self.size_redeem(amount, price, decimals, fee_bps)
        # A shortfall left after spending raw funds can be smaller than the
        # vault's minimum redeem. Redeem the minimum then; the surplus USDC
        # is forwarded with the rest and sits idle until redeployed.
        min_mtbill = await asyncio.to_thread(self._client.get_redemption_min_amount)
        mtbill_to_redeem = max(mtbill_to_redeem, min_mtbill)
        # The pool has to hand `amount` on to the user straight after this, so
        # anything less is unusable. Floor the redeem at the target rather than
        # at a slippage band below it: reverting here is recoverable, whereas a
        # short fill leaves the payout to revert with the funds already out of
        # the protocol.
        min_receive_usdc = amount * _BASE18_SCALE

        mtbill_allowance = await asyncio.to_thread(
            self._client.get_allowance,
            self._client.mtbill_address,
            self._client.redemption_vault_address,
        )
        if mtbill_allowance < mtbill_to_redeem:
            logger.info(
                "MidasStrategy.withdraw_from_earn: topping up mTBILL allowance "
                "current=%d needed=%d",
                mtbill_allowance, mtbill_to_redeem,
            )
            await asyncio.to_thread(
                self._client.approve,
                self._client.mtbill_address,
                self._client.redemption_vault_address,
                mtbill_to_redeem,
            )

        try:
            redeem_tx = await asyncio.to_thread(
                self._client.redeem_instant,
                self._asset_address,
                mtbill_to_redeem,
                min_receive_usdc,
            )
        except RuntimeError as exc:
            # The redeem reverted, so nothing moved: the withdrawal can wait
            # for liquidity instead of failing.
            raise MidasLiquidityUnavailable(
                f"Midas redeemInstant unavailable (target_usdc={amount} "
                f"mtbill_in={mtbill_to_redeem}): {exc}"
            ) from exc

        lp_usdc_after = lp_usdc_before
        for attempt in range(1, REDEEM_BALANCE_POLL_ATTEMPTS + 1):
            lp_usdc_after = await asyncio.to_thread(
                self._client.get_erc20_balance, self._asset_address,
            )
            if lp_usdc_after > lp_usdc_before:
                break
            if attempt < REDEEM_BALANCE_POLL_ATTEMPTS:
                logger.warning(
                    "MidasStrategy.withdraw_from_earn: redeem tx=%s confirmed but the "
                    "USDC balance still reads %d (attempt %d/%d); re-reading",
                    redeem_tx, lp_usdc_after, attempt, REDEEM_BALANCE_POLL_ATTEMPTS,
                )
                await asyncio.sleep(REDEEM_BALANCE_POLL_INTERVAL_SEC)

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
        return realized_usdc

    async def min_deploy_amount(self) -> int:
        """The issuance vault's own minimum, converted from Midas base-18 to
        token units and rounded up so the converted figure never lands just
        under the vault's floor.
        """
        base18 = await asyncio.to_thread(self._client.get_issuance_min_amount)
        return -(-base18 // _BASE18_SCALE)

    async def total_assets(self) -> int:
        """Live AUM held by the pool address, in USDC base units: mTBILL
        balance times the oracle price, plus any USDC sitting on the address
        itself. That raw balance is pool money mid-flight, bridged in but not
        yet minted; leaving it out would show the pool as short. Returns 0
        when the address holds nothing so callers fall back to the on-chain
        pool snapshot.
        """
        # Both sides at one block, so a mint or redeem landing between the
        # reads cannot show up on both of them.
        block = await asyncio.to_thread(lambda: self._client.w3.eth.block_number)
        mtbill_bal = await asyncio.to_thread(
            self._client.get_mtbill_balance, self._pool_address, block,
        )
        raw_usdc = await asyncio.to_thread(
            self._client.get_erc20_balance, self._asset_address, None, block,
        )
        if mtbill_bal == 0:
            return raw_usdc
        price, decimals = await asyncio.to_thread(self._read_oracle_price)
        return raw_usdc + self.convert_mtbill_to_usdc_amount(mtbill_bal, price, decimals)

    async def in_flight_assets(self) -> int:
        return sum(int(w.amount) for w in await self._pending_bridges())

    async def _pending_bridges(self) -> list:
        client = self._get_privana()
        pending = await self._retry_on_network_error(
            "get_pending_withdrawals",
            lambda: client.get_pending_withdrawals(self._pool_address),
        )
        token = self._token_id.lower()
        return [w for w in pending.pending_withdrawals if w.token_id.lower() == token]

    async def _await_bridges_in_flight(self) -> None:
        """An earlier bridge still on its way would land on the same account
        and be mistaken for ours, so wait for the account to settle first.
        """
        for attempt in range(1, self._max_bridge_poll_attempts + 1):
            pending = await self._pending_bridges()
            if not pending:
                return
            logger.info(
                "MidasStrategy: %d earlier bridge(s) still in flight (attempt %d/%d); waiting",
                len(pending), attempt, self._max_bridge_poll_attempts,
            )
            await asyncio.sleep(self._poll_interval_sec)
        raise RuntimeError(
            f"MidasStrategy: earlier bridge still in flight after "
            f"{self._max_bridge_poll_attempts} polls; aborting to release lock"
        )

    async def withdraw_ready(self, amount: int) -> bool:
        """Whether the instant path can take this withdrawal today. Raw USDC
        already on the earn account is spent first and needs no redeem. A
        failed read answers yes: the redeem itself is the final check, and
        it holds the request the same way if it reverts."""
        try:
            on_hand = await asyncio.to_thread(
                self._client.get_erc20_balance, self._asset_address,
            )
            shortfall = amount - on_hand
            if shortfall <= 0:
                return True
            if await asyncio.to_thread(self._client.is_redemption_paused):
                return False
            price, decimals = await asyncio.to_thread(self._read_oracle_price)
            fee_bps = await asyncio.to_thread(
                self._client.get_redemption_fee_bps, self._asset_address,
            )
            if fee_bps >= 10_000:
                return False
            needed = self.size_redeem(shortfall, price, decimals, fee_bps)
            minimum = await asyncio.to_thread(self._client.get_redemption_min_amount)
            needed = max(needed, minimum)
            remaining = await asyncio.to_thread(self._client.get_instant_redeem_remaining)
        except Exception:
            logger.warning("MidasStrategy.withdraw_ready: capacity read failed", exc_info=True)
            return True
        if remaining < needed:
            logger.info(
                "MidasStrategy.withdraw_ready: instant capacity %d below the %d needed",
                remaining, needed,
            )
            return False
        return True

    async def stranded_assets(self) -> int:
        return await asyncio.to_thread(
            self._client.get_erc20_balance, self._asset_address,
        )

    async def idle_assets(self) -> int:
        """The pool's accounting balance: deposits whose issuance never
        completed, and redemptions not yet redeployed. Both carry minted
        shares, so they belong in AUM even though the mTBILL position does
        not reflect them."""
        return await self._read_pool_balance()

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
            except AccountingApiError as exc:
                # A 5xx is the accounting API failing, not the read being
                # wrong; aborting here has stranded funds mid-bridge after
                # the on-chain legs already ran. 4xx stays fatal: the server
                # understood the read and refused it.
                if exc.status_code < 500:
                    raise
                logger.warning(
                    "MidasStrategy.%s: accounting 5xx, retrying after %.1fs (%s)",
                    op, self._poll_interval_sec, exc,
                )
                await asyncio.sleep(self._poll_interval_sec)

    async def _bridge_to_base(self, amount: int) -> None:
        """Submit an accounting Withdraw signed by the earn pool key and block
        until the funds land on the earn account.

        Landing is judged by the asset balance on the EOA, not by
        accounting's pending list. A withdrawal the relay resolves before
        this loop first observes it pending never shows up in that list,
        and waiting on the list then runs until the poll cap with the funds
        already here. The poll reads the chain only, so no accounting outage
        can stretch the cap.
        """
        client = self._get_privana()
        ep_account = Account.from_key(self._ep_secret_key)
        balance_before = await asyncio.to_thread(
            self._client.get_erc20_balance, self._asset_address,
        )
        nonce_resp = await self._retry_on_network_error(
            "get_withdrawal_nonce",
            lambda: client.get_withdrawal_nonce(self._pool_address),
        )
        nonce = nonce_resp.nonce
        signature = sign_withdraw_message(
            SignWithdrawParams(
                account=ep_account,
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
        attempts = 0
        while True:
            attempts += 1
            try:
                balance = await asyncio.to_thread(
                self._client.get_erc20_balance, self._asset_address,
            )
            except Exception as exc:
                logger.warning(
                    "MidasStrategy._bridge_to_base: balance read failed (attempt %d/%d); retrying: %s",
                    attempts, self._max_bridge_poll_attempts, exc,
                )
                balance = -1
            if balance >= balance_before + amount:
                logger.info(
                    "MidasStrategy._bridge_to_base: withdrawal landed nonce=%d amount=%d balance=%d",
                    nonce, amount, balance,
                )
                return
            if attempts >= self._max_bridge_poll_attempts:
                raise RuntimeError(
                    f"MidasStrategy._bridge_to_base: withdrawal not landed after "
                    f"{self._max_bridge_poll_attempts} polls (pool={self._pool_address} "
                    f"token={self._token_id} amount={amount}); aborting to release lock"
                )
            await asyncio.sleep(self._poll_interval_sec)

    async def _read_pool_balance(self) -> int:
        async def _get_balance():
            client = await self._get_authed_privana()
            return await client.get_balance(self._token_id)

        balance = await self._retry_on_network_error("get_balance", _get_balance)
        return int(balance.balance)

    async def _nudge_check_deposit(self, tx_hash: str, amount: int) -> bool:
        """One check_deposit attempt for the forwarded transfer, True once
        accounting accepts the report (credited or pending). Mirrors
        AaveStrategy._nudge_check_deposit: the relay has no watcher on pool
        deposit addresses, so the caller retries until accepted.
        """
        try:
            client = await self._get_authed_privana()
            check = await client.check_deposit(
                DepositCheckRequest(
                    chain_id=self._client.w3.eth.chain_id,
                    tx_hash=tx_hash,
                    amount=amount,
                )
            )
        except Exception as exc:
            progress.update_finality(exc)
            logger.warning(
                "MidasStrategy.withdraw_from_earn: check_deposit not accepted yet "
                "(%s); will retry",
                exc,
            )
            return False
        if check.status == "error":
            logger.warning(
                "MidasStrategy.withdraw_from_earn: check_deposit reported error: %s; "
                "will retry",
                check.detail,
            )
            return False
        return True

    async def _poll_until_credited(
        self, target_balance: int, tx_hash: str, amount: int
    ) -> None:
        nudged = False
        while True:
            if not nudged:
                nudged = await self._nudge_check_deposit(tx_hash, amount)
            current = await self._read_pool_balance()
            if current >= target_balance:
                return
            await asyncio.sleep(self._poll_interval_sec)


__all__ = ["MidasStrategy", "MidasInstantUnavailableError"]
