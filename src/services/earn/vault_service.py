import asyncio
import logging
import time
import uuid
from decimal import Decimal
from typing import Optional

from web3 import Web3

from src.clients.accounting import get_accounting_client
from src.clients.sapphire import get_sapphire_client
from src.core.abi import load_abi
from src.core.config import load_settings
from src.core.db import db_write, get_db
from src.core.eip712 import recover_withdraw_signer, sign_transfer
from src.core.validation import (
    sanitize_error,
    validate_address,
    validate_amount,
    validate_signature,
)
from src.services.earn.change import change_24h
from src.services.earn.registry import StrategyRegistry, get_strategy_registry
from src.services.earn.strategies.base import ApyPoint

logger = logging.getLogger(__name__)

EARN_MANAGER_ABI = load_abi("EarnManager")

EARN_OP_DEPOSIT = "deposit"
EARN_OP_WITHDRAW = "withdraw"
EARN_STATUS_PENDING = "pending"
EARN_STATUS_COMPLETED = "completed"
EARN_STATUS_FAILED = "failed"
# Shares were minted on-chain but the funds never reached the yield strategy.
# Distinct from "failed" because the user's deposit is real and irreversible,
# and distinct from "completed" because the balance earns nothing until an
# operator redeploys it.
EARN_STATUS_UNDEPLOYED = "undeployed"

SYNC_MAX_DROP_BPS = 100


def _exchange_rate(total_assets: int, total_shares: int) -> str:
    if total_shares == 0:
        return "1.0"
    return str(Decimal(total_assets) / Decimal(total_shares))


class VaultService:
    def __init__(self, registry: Optional[StrategyRegistry] = None) -> None:
        self.settings = load_settings()
        self.sapphire = get_sapphire_client()
        self.accounting = get_accounting_client()
        self._lp_tx_lock = asyncio.Lock()
        self._registry = registry if registry is not None else get_strategy_registry()
        self.contract_address = Web3.to_checksum_address(
            self.settings.earn_manager_contract_address
        )
        self.contract = self.sapphire.w3.eth.contract(
            address=self.contract_address,
            abi=EARN_MANAGER_ABI,
        )

    async def _route_to_strategy(self, pool_id_hex: str, amount: int) -> None:
        """After a successful EarnManager.deposit, push the same amount into
        the pool's configured yield strategy. Blocks until the strategy
        confirms the funds reached the external protocol; raises on failure
        so the deposit endpoint surfaces the error rather than reporting a
        successful deposit for funds still sitting in pool balance.
        """
        strategy = self._registry.get(pool_id_hex)
        if strategy.name == "manual":
            return
        await strategy.deposit_to_earn(amount)

    async def _reclaim_from_strategy(self, pool_id_hex: str, amount: int) -> None:
        """Before a user withdraw, pull `amount` back from the strategy so
        the pool has liquidity to pay out. Blocks until the credit is
        observed in pool's accounting balance; raises on failure so the
        EarnManager.withdraw step is never executed when pool can't cover
        the payout.
        """
        strategy = self._registry.get(pool_id_hex)
        if strategy.name == "manual":
            return
        await strategy.withdraw_from_earn(amount)

    async def _rollback_reclaim(self, pool_id_hex: str, amount: int, tx_id: str) -> None:
        """Re-supply funds that ``_reclaim_from_strategy`` pulled back when the
        subsequent on-chain ``EarnManager.withdraw`` reverted. Without this the
        reclaimed liquidity sits idle in pool balance with shares unburned.
        Best-effort: a failed rollback is logged at CRITICAL for manual
        reconciliation rather than masked.
        """
        try:
            await self._route_to_strategy(pool_id_hex, amount)
            logger.info(
                "Earn withdraw %s: reclaimed funds re-supplied to strategy after revert",
                tx_id,
            )
        except Exception:
            logger.critical(
                "Earn withdraw %s: on-chain burn reverted AND re-supply rollback failed; "
                "amount=%d reclaimed into pool balance is stranded and needs manual redeploy",
                tx_id, amount,
            )

    def get_pool(self, pool_id: bytes) -> dict:
        pool = self.contract.functions.pools(pool_id).call()
        return {
            "token_id": "0x" + pool[0].hex(),
            "pool_address": pool[1],
            "total_shares": pool[2],
            "total_assets": pool[3],
            "active": pool[4],
        }

    def list_pools(self) -> list[dict]:
        count = self.contract.functions.getPoolCount().call()
        pools = []
        for i in range(count):
            pool_id = self.contract.functions.poolIds(i).call()
            pool = self.get_pool(pool_id)
            pool["pool_id"] = "0x" + pool_id.hex()
            pools.append(pool)
        return pools

    def get_user_shares_via_token(self, pool_id: bytes, token_hex: str) -> int:
        """Read a user's pool share balance via the SIWE auth-gated view.

        The contract recovers the caller from ``token`` (issued by accounting's
        ROFL service); the backend has no ambient privilege here, only the
        token-bearer's. Anyone holding a valid token reads exactly that user's
        balance and no one else's.
        """
        token_bytes = bytes.fromhex(token_hex.removeprefix("0x"))
        return self.contract.functions.getUserShares(pool_id, token_bytes).call()

    def get_withdraw_nonce_via_token(self, token_hex: str) -> int:
        """Read the caller's withdraw nonce via the SIWE auth-gated view.

        Frontend obtains this before signing a ``Withdraw`` consent so the
        supplied nonce matches storage at submission time.
        """
        token_bytes = bytes.fromhex(token_hex.removeprefix("0x"))
        return self.contract.functions.getWithdrawNonce(token_bytes).call()

    def convert_to_shares(self, pool_id: bytes, assets: int) -> int:
        return self.contract.functions.convertToShares(pool_id, assets).call()

    def convert_to_assets(self, pool_id: bytes, shares: int) -> int:
        return self.contract.functions.convertToAssets(pool_id, shares).call()

    def get_user_balance_via_token(self, pool_id: bytes, token_hex: str) -> dict:
        shares = self.get_user_shares_via_token(pool_id, token_hex)
        underlying = self.convert_to_assets(pool_id, shares) if shares > 0 else 0
        pool = self.get_pool(pool_id)
        return {
            "pool_id": "0x" + pool_id.hex(),
            "token_id": pool["token_id"],
            "shares": str(shares),
            "underlying_amount": str(underlying),
            "exchange_rate": _exchange_rate(pool["total_assets"], pool["total_shares"]),
        }


    async def get_deposit_quote(
        self,
        pool_id_hex: str,
        amount: str,
        user_address: str,
    ) -> dict:
        """Build a deposit quote with the four independent reads dispatched
        in parallel: getPool + convertToShares on Sapphire, the strategy's
        live total_assets on Base, and the accounting transfer nonce over
        HTTP. Sequential, each leg costs an RPC roundtrip on a slow public
        endpoint; running them concurrently makes the slowest leg the
        floor instead of the sum.
        """
        validate_address(user_address, "user_address")
        validate_amount(amount, "amount")

        pool_id = bytes.fromhex(pool_id_hex.removeprefix("0x"))
        amount_int = int(amount)

        pool, shares_estimate, strategy_aum, transfer_nonce = await asyncio.gather(
            asyncio.to_thread(self.get_pool, pool_id),
            asyncio.to_thread(self.convert_to_shares, pool_id, amount_int),
            self._strategy_total_assets_safe(pool_id_hex),
            self.accounting.get_transfer_nonce(user_address),
        )

        if pool["pool_address"] == "0x0000000000000000000000000000000000000000":
            raise ValueError("Pool not found")
        if not pool["active"]:
            raise ValueError("Pool is not active")

        effective_assets = (
            strategy_aum if strategy_aum is not None and strategy_aum > 0 else pool["total_assets"]
        )
        exchange_rate = _exchange_rate(effective_assets, pool["total_shares"])

        now = int(time.time())
        return {
            "quote_id": str(uuid.uuid4()),
            "pool_id": pool_id_hex,
            "token_id": pool["token_id"],
            "amount": amount,
            "shares_estimate": str(shares_estimate),
            "exchange_rate": exchange_rate,
            "pool_address": pool["pool_address"],
            "transfer_nonce": transfer_nonce,
            "expires_at": now + self.settings.quote_ttl,
        }

    async def _strategy_total_assets_safe(self, pool_id_hex: str) -> Optional[int]:
        """Best-effort strategy AUM read for parallel-fetch paths. Returns
        None when there's no external strategy or the read fails, letting
        the caller fall back to the on-chain pool snapshot.
        """
        strategy = self._registry.get(pool_id_hex)
        if strategy.name == "manual":
            return None
        try:
            return await strategy.total_assets()
        except Exception:
            logger.exception(
                "_strategy_total_assets_safe failed pool=%s strategy=%s",
                pool_id_hex, strategy.name,
            )
            return None

    async def strategy_apy_history_safe(
        self, pool_id_hex: str, days: Optional[int] = None
    ) -> list[ApyPoint]:
        """Best-effort APY history for the configured strategy, oldest first.

        Empty is a normal answer, not an error: most strategies have no historical
        source. Degrades to empty on failure too, so a flaky external read renders
        no chart rather than 500ing the endpoint.
        """
        strategy = self._registry.get(pool_id_hex)
        try:
            return await strategy.get_apy_history(days)
        except Exception:
            logger.exception(
                "strategy_apy_history_safe failed pool=%s strategy=%s",
                pool_id_hex, strategy.name,
            )
            return []

    async def strategy_apy_bps_safe(self, pool_id_hex: str) -> int:
        """Best-effort APY read for the configured strategy.

        Returns the strategy's current APY in basis points. Falls back to 0
        on any failure (Aave RPC down, asset not listed, etc.) so a flaky
        external read never 500s ``/v1/earn/pools``. Same protective shape as
        ``_strategy_total_assets_safe``: log and degrade rather than crash.
        """
        strategy = self._registry.get(pool_id_hex)
        try:
            return await strategy.get_apy_bps()
        except Exception:
            logger.exception(
                "strategy_apy_bps_safe failed pool=%s strategy=%s",
                pool_id_hex, strategy.name,
            )
            return 0

    async def deposit(
        self,
        pool_id_hex: str,
        user_address: str,
        amount: str,
        nonce: int,
        signature: str,
    ) -> dict:
        """Deposit user funds into an earn pool and mint shares.

        Signature flow: the user signs an EIP-712 ``Transfer(user -> pool, tokenId,
        amount, nonce)`` off-chain against the Accounting domain. This service
        forwards that signature to ``EarnManager.deposit``, which atomically
        transfers the funds on the accounting ledger and mints pool shares to the
        user. The service itself never signs — authority to debit the user lives
        with the user alone.
        """
        validate_address(user_address, "user_address")
        validate_amount(amount, "amount")
        validate_signature(signature, "signature")

        pool_id = bytes.fromhex(pool_id_hex.removeprefix("0x"))
        pool = self.get_pool(pool_id)
        if pool["pool_address"] == "0x0000000000000000000000000000000000000000":
            raise ValueError("Pool not found")
        if not pool["active"]:
            raise ValueError("Pool is not active")

        sig_bytes = bytes.fromhex(signature.removeprefix("0x"))

        async with self._lp_tx_lock:
            # Sync under the lock: it reads the strategy's live AUM and writes
            # it as the contract's share-math denominator, so it must not run
            # while another op has assets in flight. Outside the lock a deposit
            # could sync a transient balance mid-reclaim and mint against a
            # false denominator.
            #
            # Fail closed: minting divides by this denominator, so a deposit
            # that cannot confirm it is refused rather than priced against a
            # stale or manipulated value. Withdraw, which burns rather than
            # mints, stays best-effort.
            if await self.sync_total_assets(pool_id_hex) is None:
                raise ValueError(
                    "Pool valuation could not be confirmed; deposit refused. "
                    "Retry shortly."
                )

            tx_id = self._record_transaction(
                operation=EARN_OP_DEPOSIT,
                pool_id_hex=pool_id_hex,
                user_address=user_address,
                token_id=pool["token_id"],
                amount=amount,
                signer_address=user_address,
                nonce=nonce,
                signature=signature,
            )

            try:
                tx_hash = await asyncio.to_thread(
                    self.sapphire.execute_contract_call,
                    contract_address=self.contract_address,
                    abi=EARN_MANAGER_ABI,
                    function_name="deposit",
                    args=[
                        pool_id,
                        Web3.to_checksum_address(user_address),
                        int(amount),
                        nonce,
                        sig_bytes,
                    ],
                )
            except Exception as exc:
                logger.exception("Earn deposit %s failed", tx_id)
                error = sanitize_error(str(exc))
                self._update_transaction(tx_id, status=EARN_STATUS_FAILED, error=error)
                return {
                    "deposit_id": tx_id,
                    "pool_id": pool_id_hex,
                    "amount": amount,
                    "shares_minted": None,
                    "exchange_rate": None,
                    "tx_hash": None,
                    "status": "failed",
                    "error": error,
                }

            self._update_transaction(tx_id, status=EARN_STATUS_COMPLETED, tx_hash=tx_hash)

            deploy_error = None
            try:
                await self._route_to_strategy(pool_id_hex, int(amount))
            except Exception as exc:
                logger.exception(
                    "Earn deposit %s minted shares but strategy routing failed; "
                    "funds are in pool balance pending redeploy",
                    tx_id,
                )
                deploy_error = sanitize_error(str(exc))
                self._update_transaction(
                    tx_id, status=EARN_STATUS_UNDEPLOYED, error=deploy_error
                )

        deploy_status = EARN_STATUS_UNDEPLOYED if deploy_error else EARN_STATUS_COMPLETED

        try:
            pool_after = self.get_pool(pool_id)
            effective_assets = await self.effective_total_assets(pool_id_hex, pool_after["total_assets"])
            return {
                "deposit_id": tx_id,
                "pool_id": pool_id_hex,
                "amount": amount,
                # shares_minted is None: per-user share state is private. Clients
                # can compute it themselves via the SIWE-gated getUserShares.
                "shares_minted": None,
                "exchange_rate": _exchange_rate(effective_assets, pool_after["total_shares"]),
                "tx_hash": tx_hash,
                "status": deploy_status,
                "error": deploy_error,
            }
        except Exception:
            logger.warning("Post-tx read failed for deposit %s, returning degraded response", tx_id)
            return {
                "deposit_id": tx_id,
                "pool_id": pool_id_hex,
                "amount": amount,
                "shares_minted": None,
                "exchange_rate": None,
                "tx_hash": tx_hash,
                "status": deploy_status,
                "error": deploy_error,
            }

    async def withdraw(
        self,
        pool_id_hex: str,
        user_address: str,
        amount: str,
        nonce: int,
        signature: str,
    ) -> dict:
        """Burn user shares and return the underlying assets.

        Two signatures are required, one from each side of the trust boundary:

        - ``signature``: the user's EIP-712 ``Withdraw(poolId, amount, nonce)``
          consent in the EarnManager's domain. The contract recovers the
          signer and treats them as the effective user; without it any caller
          could force-eject any user from the pool.
        - The pool's accounting ``Transfer(pool -> user, ...)`` signature, which
          the service signs locally with the LP key. This is what authorizes
          accounting to debit the pool's balance.

        Per-user state (``withdrawNonces``, ``userShares``) is private on the
        contract, so the backend can no longer pre-check the supplied nonce
        or share balance: a stale nonce surfaces as ``InvalidWithdrawSignature``
        and a too-large amount as ``InsufficientShares``, both via the on-chain
        revert path.
        """
        validate_address(user_address, "user_address")
        validate_amount(amount, "amount")
        validate_signature(signature, "signature")

        pool_id = bytes.fromhex(pool_id_hex.removeprefix("0x"))
        pool = self.get_pool(pool_id)
        if pool["pool_address"] == "0x0000000000000000000000000000000000000000":
            raise ValueError("Pool not found")
        # No active check — users must always be able to exit paused pools.

        async with self._lp_tx_lock:
            # Sync inside the lock, before moving any strategy assets, so a
            # concurrent deposit can never sync the transient balance this
            # reclaim is about to create.
            await self.sync_total_assets(pool_id_hex)
            reclaim_tx_id = str(uuid.uuid4())
            try:
                await self._reclaim_from_strategy(pool_id_hex, int(amount))
            except Exception as exc:
                # A partial reclaim (redeemed from the protocol but never
                # credited to the pool) must not escape the lock with the
                # denominator understated: roll back what moved, restore the
                # authoritative AUM, then surface the failure.
                logger.exception("Earn withdraw %s: reclaim failed", reclaim_tx_id)
                await self._rollback_reclaim(pool_id_hex, int(amount), reclaim_tx_id)
                await self.sync_total_assets(pool_id_hex)
                raise ValueError(
                    f"Withdraw failed: {sanitize_error(str(exc))}"
                ) from exc

            pool_nonce = await self.accounting.get_transfer_nonce(pool["pool_address"])

            pool_signature = sign_transfer(
                private_key=self.settings.liquidity_provider_secret_key,
                chain_id=self.settings.accounting_chain_id,
                verifying_contract=self.settings.accounting_contract_address,
                to_address=user_address,
                token_id=pool["token_id"],
                amount=int(amount),
                nonce=pool_nonce,
            )

            pool_sig_bytes = bytes.fromhex(pool_signature.removeprefix("0x"))
            user_sig_bytes = bytes.fromhex(signature.removeprefix("0x"))

            # The recipient (user_address) and the share owner can differ; the
            # owner is whoever signed the withdraw consent, and per-user
            # attribution (e.g. the 24h change guard) must key on them.
            try:
                consent_signer = recover_withdraw_signer(
                    chain_id=self.settings.accounting_chain_id,
                    earn_manager_address=self.contract_address,
                    pool_id=pool_id_hex,
                    amount=int(amount),
                    nonce=nonce,
                    signature=signature,
                )
            except Exception:
                logger.exception("Withdraw consent recovery failed")
                consent_signer = None

            tx_id = self._record_transaction(
                operation=EARN_OP_WITHDRAW,
                pool_id_hex=pool_id_hex,
                user_address=user_address,
                token_id=pool["token_id"],
                amount=amount,
                signer_address=pool["pool_address"],
                nonce=pool_nonce,
                signature=pool_signature,
                consent_signer=consent_signer,
            )

            try:
                tx_hash = await asyncio.to_thread(
                    self.sapphire.execute_contract_call,
                    contract_address=self.contract_address,
                    abi=EARN_MANAGER_ABI,
                    function_name="withdraw",
                    args=[
                        pool_id,
                        int(amount),
                        nonce,
                        user_sig_bytes,
                        pool_nonce,
                        pool_sig_bytes,
                    ],
                )
            except Exception as exc:
                logger.exception("Earn withdraw %s failed", tx_id)
                await self._rollback_reclaim(pool_id_hex, int(amount), tx_id)
                # Restore the authoritative AUM before the lock releases: the
                # rollback puts the assets back in the strategy, but the
                # contract's totalAssets still reflects the reclaimed-out state
                # until this resync, and the next op under the lock would
                # otherwise mint against that false denominator.
                await self.sync_total_assets(pool_id_hex)
                error = sanitize_error(str(exc))
                self._update_transaction(tx_id, status=EARN_STATUS_FAILED, error=error)
                return {
                    "withdraw_id": tx_id,
                    "pool_id": pool_id_hex,
                    "amount": amount,
                    "shares_burned": None,
                    "exchange_rate": None,
                    "tx_hash": None,
                    "status": "failed",
                    "error": error,
                }

        self._update_transaction(tx_id, status=EARN_STATUS_COMPLETED, tx_hash=tx_hash)

        try:
            pool_after = self.get_pool(pool_id)
            effective_assets = await self.effective_total_assets(pool_id_hex, pool_after["total_assets"])
            return {
                "withdraw_id": tx_id,
                "pool_id": pool_id_hex,
                "amount": amount,
                # shares_burned is None: per-user share state is private. Clients
                # can compute it themselves via the SIWE-gated getUserShares.
                "shares_burned": None,
                "exchange_rate": _exchange_rate(effective_assets, pool_after["total_shares"]),
                "tx_hash": tx_hash,
                "status": "completed",
                "error": None,
            }
        except Exception:
            logger.warning("Post-tx read failed for withdraw %s, returning degraded response", tx_id)
            return {
                "withdraw_id": tx_id,
                "pool_id": pool_id_hex,
                "amount": amount,
                "shares_burned": None,
                "exchange_rate": None,
                "tx_hash": tx_hash,
                "status": "completed",
                "error": None,
            }

    async def effective_total_assets(self, pool_id_hex: str, on_chain_total: int) -> int:
        """Live AUM for a pool, derived from the strategy when available.

        For Aave-style strategies, on-chain totalAssets only records principal
        at deposit/withdraw time; live yield lives in the aToken balance, so
        we ask the strategy directly. ManualStrategy reports 0, in which case
        the on-chain total is authoritative (no external capital).

        Best-effort: any strategy failure falls back to the on-chain value so
        reads stay available even when the protocol RPC is down.
        """
        strategy = self._registry.get(pool_id_hex)
        if strategy.name == "manual":
            return on_chain_total
        try:
            external = await strategy.total_assets()
            idle = await strategy.idle_assets()
        except Exception:
            logger.exception(
                "strategy AUM read failed pool=%s strategy=%s; falling back to on-chain",
                pool_id_hex, strategy.name,
            )
            return on_chain_total
        total = external + idle
        return total if total > 0 else on_chain_total

    async def strict_total_assets(self, pool_id_hex: str, on_chain_total: int) -> Optional[int]:
        """Every asset the pool's shares are backed by, or None.

        ``effective_total_assets`` degrades to the on-chain principal when the
        strategy read fails, which is right for a balance read that should stay
        available. It is wrong for a rate: on-chain totalAssets only moves on
        sync, so comparing it against a stored yield-inclusive sample invents a
        loss that never happened. Anything that stores or compares a rate takes
        this form and skips instead of guessing.

        Idle funds count too. An undeployed deposit has shares against it while
        the money sits in the pool's accounting balance rather than in Aave, so
        counting only the strategy would report those shares as backed by
        nothing.
        """
        strategy = self._registry.get(pool_id_hex)
        if strategy.name == "manual":
            return on_chain_total
        try:
            external = await strategy.total_assets()
            idle = await strategy.idle_assets()
        except Exception:
            logger.exception(
                "strategy AUM read failed pool=%s strategy=%s; no rate snapshot",
                pool_id_hex, strategy.name,
            )
            return None
        total = external + idle
        return total if total > 0 else on_chain_total

    async def rate_snapshot(self, pool_id_hex: str) -> Optional[tuple[int, int]]:
        """A coherent ``(total_assets, total_shares)`` pair, or None.

        Assets sit in the strategy and in the pool's accounting balance while
        shares live on-chain, so no single read returns both. Read the share
        count, read the assets, then read the share count again: if it moved, a
        cashflow landed mid-read and the pair describes no one instant. A
        same-rate deposit caught that way would otherwise pair new assets with
        old shares and look like a jump in value.
        """
        pool_id = bytes.fromhex(pool_id_hex.removeprefix("0x"))
        before = await asyncio.to_thread(self.get_pool, pool_id)
        assets = await self.strict_total_assets(pool_id_hex, before["total_assets"])
        if assets is None:
            return None
        after = await asyncio.to_thread(self.get_pool, pool_id)
        if int(after["total_shares"]) != int(before["total_shares"]):
            logger.info(
                "Pool %s share count moved while reading assets; no rate snapshot",
                pool_id_hex,
            )
            return None
        return assets, int(before["total_shares"])

    async def sync_total_assets(self, pool_id_hex: str) -> Optional[int]:
        """Confirm EarnManager.totalAssets equals every asset backing the
        pool's shares, syncing it on-chain if not, and return that confirmed
        value — or None if it could not be established.

        Backing is strategy assets PLUS idle assets (funds credited to the
        pool but not yet deployed, e.g. an undeployed deposit or a reclaim
        awaiting redeploy). Counting only the strategy would understate the
        denominator whenever funds sit idle — including right after a failed
        withdrawal rollback — and let the next deposit mint against a false,
        low denominator.

        None means "could not confirm": the strategy read failed, the sync tx
        failed, or the reading would lower the denominator by more than
        SYNC_MAX_DROP_BPS. Deposit
        treats None as fail-closed and refuses to mint; withdraw treats it as
        best-effort, since burning shares on a stale rate cannot inflate the
        pool and users must always be able to exit.
        """
        strategy = self._registry.get(pool_id_hex)
        pool_id = bytes.fromhex(pool_id_hex.removeprefix("0x"))
        on_chain = self.get_pool(pool_id)["total_assets"]
        # Manual pools hold no external capital, so on-chain totalAssets is
        # already authoritative — nothing to read or write.
        if strategy.name == "manual":
            return on_chain

        try:
            external = await strategy.total_assets()
            idle = await strategy.idle_assets()
        except Exception:
            logger.exception(
                "sync_total_assets read failed pool=%s strategy=%s",
                pool_id_hex, strategy.name,
            )
            return None

        backing = external + idle
        if backing == on_chain:
            return on_chain
        if backing < on_chain and (on_chain - backing) * 10_000 > on_chain * SYNC_MAX_DROP_BPS:
            # A large drop is far more likely a transient — funds mid-flight
            # between the protocol and the pool balance, e.g. a partially
            # credited reclaim — than a real loss. Writing it would let the
            # next deposit mint against the dip, so refuse and leave the
            # denominator where it is; a genuine loss needs an operator sync.
            # Small drops within SYNC_MAX_DROP_BPS still sync, covering
            # issuance fees and slippage drift.
            logger.warning(
                "sync_total_assets read backing=%d against on_chain=%d pool=%s; "
                "drop exceeds %d bps, refusing to lower the denominator",
                backing, on_chain, pool_id_hex, SYNC_MAX_DROP_BPS,
            )
            return None

        try:
            await asyncio.to_thread(
                self.sapphire.execute_contract_call,
                contract_address=self.contract_address,
                abi=EARN_MANAGER_ABI,
                function_name="syncTotalAssets",
                args=[pool_id, backing],
            )
        except Exception:
            logger.exception(
                "syncTotalAssets tx failed pool=%s old=%d new=%d",
                pool_id_hex, on_chain, backing,
            )
            return None
        logger.info(
            "syncTotalAssets succeeded pool=%s old=%d new=%d",
            pool_id_hex, on_chain, backing,
        )
        return backing

    def _record_transaction(
        self,
        *,
        operation: str,
        pool_id_hex: str,
        user_address: str,
        token_id: str,
        amount: str,
        signer_address: str,
        nonce: int,
        signature: str,
        consent_signer: Optional[str] = None,
    ) -> str:
        tx_id = str(uuid.uuid4())
        now = int(time.time())
        db = get_db()
        db_write(
            db,
            """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, status, created_at, updated_at,
                consent_signer)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tx_id, operation, pool_id_hex, user_address.lower(), token_id, amount,
                signer_address.lower(), nonce, signature,
                EARN_STATUS_PENDING, now, now,
                consent_signer.lower() if consent_signer else None,
            ),
        )
        logger.info(
            "earn %s %s signed: signer=%s to=%s token=%s amount=%s nonce=%s",
            operation, tx_id, signer_address, user_address, token_id, amount, nonce,
        )
        return tx_id

    def _update_transaction(self, tx_id: str, **fields) -> None:
        db = get_db()
        fields["updated_at"] = int(time.time())
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [tx_id]
        db_write(db, f"UPDATE earn_transactions SET {set_clause} WHERE id = ?", tuple(values))

    async def get_all_balances(
        self, token_hex: str, user_address: Optional[str] = None
    ) -> list[dict]:
        """Return the token-bearer's positions across every pool.

        Reads are SIWE-gated on the contract: the caller must obtain an
        encrypted auth token from accounting's ROFL service and pass it
        through. The backend never resolves the user address — that happens
        on-chain inside ``getUserShares(poolId, token)``. ``user_address`` is
        only known on the JWT path and only feeds the 24h change fields, which
        stay null without it.
        """
        if not token_hex:
            raise ValueError("token is required")
        pools = await asyncio.to_thread(self.list_pools)

        async def fetch_balance(pool: dict) -> Optional[dict]:
            pool_id = bytes.fromhex(pool["pool_id"].removeprefix("0x"))
            shares = await asyncio.to_thread(
                self.get_user_shares_via_token, pool_id, token_hex
            )
            if shares == 0:
                return None
            underlying = await asyncio.to_thread(self.convert_to_assets, pool_id, shares)
            # The change needs assets and shares from one instant; the balance
            # itself only needs to stay available, so it falls back to the
            # on-chain total when no coherent snapshot can be taken.
            snapshot = await self.rate_snapshot(pool["pool_id"])
            effective_assets = snapshot[0] if snapshot else pool["total_assets"]
            try:
                change = (
                    await asyncio.to_thread(
                        change_24h,
                        user_address,
                        pool["pool_id"],
                        shares,
                        snapshot[0],
                        snapshot[1],
                        int(time.time()),
                    )
                    if snapshot
                    else None
                )
            except Exception:
                logger.exception("24h change failed for pool %s", pool["pool_id"])
                change = None
            return {
                "pool_id": pool["pool_id"],
                "token_id": pool["token_id"],
                "shares": str(shares),
                "underlying_amount": str(underlying),
                "exchange_rate": _exchange_rate(effective_assets, pool["total_shares"]),
                "change_24h": change.amount if change else None,
                "change_24h_pct": change.pct if change else None,
            }

        results = await asyncio.gather(*[fetch_balance(p) for p in pools])
        return [b for b in results if b is not None]


_service_instance: Optional[VaultService] = None


def get_vault_service() -> VaultService:
    global _service_instance
    if _service_instance is None:
        _service_instance = VaultService()
    return _service_instance
