import asyncio
import logging
import time
import uuid
from collections import Counter
from decimal import Decimal
from functools import partial
from typing import Optional

from web3 import Web3
from web3.exceptions import ContractLogicError, TransactionNotFound, Web3RPCError

from src.clients.accounting import get_accounting_client
from src.clients.sapphire import get_pool_admin_sapphire_client
from src.core.abi import load_abi
from src.core.config import load_settings
from src.core.db import db_write, get_db
from src.core.eip712 import (
    recover_transfer_signer,
    recover_withdraw_signer,
    sign_transfer,
)
from src.core.validation import (
    sanitize_error,
    validate_address,
    validate_amount,
    validate_signature,
)
from src.services.earn.change import change_24h
from src.services.earn.earned import (
    STATUS_LEDGER_INCOMPLETE,
    Earned,
    earned_active,
)
from src.services.earn.registry import StrategyRegistry, get_strategy_registry
from src.services.earn.strategies.base import ApyPoint
from src.services.user_queue import assert_nonce_free

logger = logging.getLogger(__name__)

EARN_MANAGER_ABI = load_abi("EarnManager")

EARN_OP_DEPOSIT = "deposit"
EARN_OP_WITHDRAW = "withdraw"
EARN_STATUS_SCHEDULED = "scheduled"
EARN_STATUS_EXECUTING = "executing"
EARN_STATUS_PENDING = "pending"
EARN_STATUS_COMPLETED = "completed"
EARN_STATUS_FAILED = "failed"
# Shares were minted on-chain but the funds have not reached the yield
# strategy. Distinct from "failed" because the user's deposit is real and
# irreversible, and distinct from "completed" because the balance earns
# nothing until deployed. Every deposit passes through this state between
# the mint and the strategy routing, so a crash in that window leaves a row
# an operator can find instead of a "completed" row hiding idle funds.
EARN_STATUS_UNDEPLOYED = "undeployed"

SYNC_MAX_DROP_BPS = 100

# Sapphire authenticates a read by wrapping it in a signed query whose leash
# pins a recent block. The client reads the head and then the block before it
# as two calls, so when the chain moves between them the node rejects the
# leash. It is transient by nature: the next attempt builds a fresh one.
_STALE_LEASH = "base block not found"
READ_RETRY_ATTEMPTS = 5
READ_RETRY_BACKOFF_SEC = 0.5

# Protocol-owned principal only moves when an operator records or drops it,
# so re-reading it on every quote buys nothing and costs a signed query.
SEED_CACHE_TTL_SEC = 30


class ReceiptUnknown(Exception):
    """Broadcast, but the receipt could not be read. Recovery settles it."""


def _exchange_rate(total_assets: int, total_shares: int) -> str:
    if total_shares == 0:
        return "1.0"
    return str(Decimal(total_assets) / Decimal(total_shares))


def _settled_shares(pool_id_hex: str) -> Optional[int]:
    """Sum of a pool's settled share movements, or None if one is missing."""
    total = 0
    for row in get_db().execute(
        "SELECT shares_delta FROM earn_transactions WHERE LOWER(pool_id) = ? AND status IN (?, ?)",
        (pool_id_hex.lower(), EARN_STATUS_COMPLETED, EARN_STATUS_UNDEPLOYED),
    ):
        if row["shares_delta"] is None:
            return None
        total += int(row["shares_delta"])
    return total


class VaultService:
    def __init__(self, registry: Optional[StrategyRegistry] = None) -> None:
        self.settings = load_settings()
        self.sapphire = get_pool_admin_sapphire_client()
        self.accounting = get_accounting_client()
        self._pools_tx_lock = asyncio.Lock()
        self._registry = registry if registry is not None else get_strategy_registry()
        self._seed_read_warned = False
        self._seed_cache: dict[str, tuple[float, int]] = {}
        self.contract_address = Web3.to_checksum_address(
            self.settings.earn_manager_contract_address
        )
        self.contract = self.sapphire.w3.eth.contract(
            address=self.contract_address,
            abi=EARN_MANAGER_ABI,
        )
        self._history = self.sapphire.reader.eth.contract(
            address=self.contract_address,
            abi=EARN_MANAGER_ABI,
        )
        self._ledger_scanned: dict[str, tuple[Optional[int], int]] = {}

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

    def _assert_pool_custody(self, pool: dict) -> None:
        """Refuse to touch a pool whose account is not the one this service
        signs for.

        Deposits land in `pool.poolAddress`, while a withdrawal is debited
        from whoever signs the pool's accounting transfer, which is always
        this service's earn account. When the two differ the money goes into
        one account and the payout is attempted from another, so shares get
        minted that can never be redeemed. `createPool` has no setter for the
        address, so the only repair is a new pool; refusing here keeps funds
        out of the old one until that happens.
        """
        expected = (self.settings.earn_pool_address or "").lower()
        actual = (pool.get("pool_address") or "").lower()
        if not expected or actual != expected:
            raise ValueError(
                "Pool is not served by this deployment: its account is "
                f"{pool.get('pool_address')} but this service signs for "
                f"{self.settings.earn_pool_address}"
            )

    def get_pool(self, pool_id: bytes) -> dict:
        pool = self._read_with_retry(self.contract.functions.pools(pool_id).call, "pools")
        return {
            "token_id": "0x" + pool[0].hex(),
            "pool_address": pool[1],
            "total_shares": pool[2],
            "total_assets": pool[3],
            "active": pool[4],
        }

    def list_pools(self) -> list[dict]:
        count = self._read_with_retry(
            self.contract.functions.getPoolCount().call, "getPoolCount"
        )
        pools = []
        for i in range(count):
            pool_id = self._read_with_retry(
                self.contract.functions.poolIds(i).call, "poolIds"
            )
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
        return self._read_with_retry(
            self.contract.functions.getUserShares(pool_id, token_bytes).call,
            "getUserShares",
        )

    def get_withdraw_nonce_via_token(self, token_hex: str) -> int:
        """Read the caller's withdraw nonce via the SIWE auth-gated view.

        Frontend obtains this before signing a ``Withdraw`` consent so the
        supplied nonce matches storage at submission time.
        """
        token_bytes = bytes.fromhex(token_hex.removeprefix("0x"))
        return self._read_with_retry(
            self.contract.functions.getWithdrawNonce(token_bytes).call,
            "getWithdrawNonce",
        )

    @staticmethod
    def _read_with_retry(call, label: str):
        """Run a contract read, retrying a stale signed-query leash.

        Only that one error is retried. Anything else is the chain's real
        answer and is left to the caller.
        """
        for attempt in range(1, READ_RETRY_ATTEMPTS + 1):
            try:
                return call()
            except Web3RPCError as exc:
                if _STALE_LEASH not in str(exc) or attempt == READ_RETRY_ATTEMPTS:
                    raise
                logger.warning(
                    "%s hit a stale signed-query leash (attempt %d/%d); retrying",
                    label, attempt, READ_RETRY_ATTEMPTS,
                )
                time.sleep(READ_RETRY_BACKOFF_SEC * attempt)

    def get_seeded_assets(self, pool_id: bytes, *, fresh: bool = False) -> int:
        """Protocol-owned principal recorded against the pool.

        Reverts against a contract that predates seeding, which has no such
        function, and against any caller that is not the pool admin, since
        how much of a pool is protocol capital is not public. Both say the
        same thing about valuation: there is no seed to net out. Reading
        them as zero is what lets the service run against a proxy that has
        not been upgraded yet, rather than failing every quote until it is.
        """
        key = pool_id.hex()
        cached = self._seed_cache.get(key)
        if not fresh and cached is not None and time.time() - cached[0] < SEED_CACHE_TTL_SEC:
            return cached[1]
        try:
            value = self._read_with_retry(
                self.contract.functions.getSeededAssets(pool_id).call, "getSeededAssets",
            )
        except ContractLogicError:
            if not self._seed_read_warned:
                logger.warning(
                    "getSeededAssets reverted; treating pools as unseeded. Expected "
                    "before the EarnManager upgrade lands, otherwise check that this "
                    "service signs as the pool admin."
                )
                self._seed_read_warned = True
            value = 0
        self._seed_cache[key] = (time.time(), value)
        return value

    def _net_of_seed(self, gross: int, seeded: int, total_shares: int) -> int:
        """User-backed assets, given everything the pool holds.

        Seed principal is senior: it comes off the top, so a shortfall lands
        on the user tranche first and the clamp at zero is the point at which
        the seed itself would have to be marked down by an operator. While no
        shares exist there is nobody to earn, so the whole balance stays with
        the seed and the first depositor does not find yield already on the
        books.

        A pool nobody has seeded is left exactly as it was before seeding
        existed. Otherwise an unseeded pool sitting at zero shares would have
        its idle balance reported as nothing, and the sync path would go on to
        record that balance as protocol principal it never was.
        """
        if seeded == 0:
            return gross
        if total_shares == 0:
            return 0
        return max(gross - seeded, 0)

    def convert_to_shares(self, pool_id: bytes, assets: int) -> int:
        return self._read_with_retry(
            self.contract.functions.convertToShares(pool_id, assets).call,
            "convertToShares",
        )

    def convert_to_assets(self, pool_id: bytes, shares: int) -> int:
        return self._read_with_retry(
            self.contract.functions.convertToAssets(pool_id, shares).call,
            "convertToAssets",
        )

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

        pool, shares_estimate, strategy_aum, idle, seeded, transfer_nonce = await asyncio.gather(
            asyncio.to_thread(self.get_pool, pool_id),
            asyncio.to_thread(self.convert_to_shares, pool_id, amount_int),
            self._strategy_total_assets_safe(pool_id_hex),
            self._strategy_idle_assets_safe(pool_id_hex),
            asyncio.to_thread(self.get_seeded_assets, pool_id),
            self.accounting.get_transfer_nonce(user_address),
        )

        if pool["pool_address"] == "0x0000000000000000000000000000000000000000":
            raise ValueError("Pool not found")
        if not pool["active"]:
            raise ValueError("Pool is not active")

        # The rate a deposit actually mints at is the one the sync writes, so
        # the quote has to net the seed out the same way. Quoting the gross
        # strategy balance would price every share as if the foundation's
        # principal backed it.
        gross = (strategy_aum or 0) + (idle or 0)
        effective_assets = (
            self._net_of_seed(gross, seeded, pool["total_shares"])
            if gross else pool["total_assets"]
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

    async def _strategy_idle_assets_safe(self, pool_id_hex: str) -> Optional[int]:
        """Best-effort idle read, paired with ``_strategy_total_assets_safe``
        so a quote values the same balance the sync does.
        """
        strategy = self._registry.get(pool_id_hex)
        if strategy.name == "manual":
            return None
        try:
            return await strategy.idle_assets()
        except Exception:
            logger.exception(
                "_strategy_idle_assets_safe failed pool=%s strategy=%s",
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

    async def _submit_and_settle(
        self, tx_id: str, *, function_name: str, args: list
    ) -> tuple[str, int]:
        """Broadcast, record the hash, then wait for the receipt.

        Split the way the swap pipeline splits it: a receipt wait that times
        out must leave a hash behind, or a transaction that later confirms is
        indistinguishable from one that never went out, and the recovery pass
        has nothing to reconcile against.
        """
        tx_hash = await asyncio.to_thread(
            self.sapphire.submit_contract_call,
            contract_address=self.contract_address,
            abi=EARN_MANAGER_ABI,
            function_name=function_name,
            args=args,
        )
        self._update_transaction(tx_id, tx_hash=tx_hash)
        try:
            receipt = await asyncio.to_thread(self.sapphire.wait_for_receipt, tx_hash)
        except Exception as exc:
            raise ReceiptUnknown(tx_hash) from exc
        if receipt["status"] != 1:
            raise RuntimeError(f"Transaction reverted: {tx_hash}")
        return tx_hash, receipt["blockNumber"]

    def _schedule(
        self,
        *,
        operation: str,
        pool_id_hex: str,
        user_address: str,
        amount: str,
        nonce: int,
        signature: str,
    ) -> dict:
        """Record a request and hand it to the worker.

        Admission mirrors ``schedule_swap``: an identical signed request
        returns the operation it already created, the signer is recovered and
        bound rather than trusted, and the checks that settle a request
        outright still answer immediately. What is deferred is the part that
        takes minutes — bridging and supplying on another chain — because a
        request that waits for that is a request the gateway hangs up on.
        """
        validate_address(user_address, "user_address")
        validate_amount(amount, "amount")
        validate_signature(signature, "signature")
        pool_id = bytes.fromhex(pool_id_hex.removeprefix("0x"))

        # An HTTP retry returns the original operation rather than queueing a
        # second one, which is also how a caller learns the outcome of a
        # request whose response it lost.
        existing = get_db().execute(
            "SELECT id, status FROM earn_transactions WHERE operation = ? "
            "AND LOWER(pool_id) = LOWER(?) AND user_address = ? "
            "AND input_nonce = ? AND LOWER(input_signature) = LOWER(?)",
            (operation, pool_id_hex, user_address.lower(), nonce, signature),
        ).fetchone()
        if existing is not None:
            return {"id": existing["id"], "status": existing["status"]}

        pool = self.get_pool(pool_id)
        if pool["pool_address"] == "0x0000000000000000000000000000000000000000":
            raise ValueError("Pool not found")
        if not pool["active"]:
            raise ValueError("Pool is not active")
        self._assert_pool_custody(pool)

        if operation == EARN_OP_WITHDRAW:
            # A withdraw reclaims from the strategy before the contract ever
            # checks consent, so an unverified one is a way to make the pool
            # redeem and roll back on demand.
            recovered = recover_withdraw_signer(
                chain_id=self.settings.accounting_chain_id,
                earn_manager_address=self.settings.earn_manager_contract_address,
                pool_id=pool_id_hex,
                amount=int(amount),
                nonce=nonce,
                signature=signature,
            )
        else:
            recovered = recover_transfer_signer(
                chain_id=self.settings.accounting_chain_id,
                verifying_contract=self.settings.accounting_contract_address,
                to_address=pool["pool_address"],
                token_id=pool["token_id"],
                amount=int(amount),
                nonce=nonce,
                signature=signature,
            )
        if recovered.lower() != user_address.lower():
            raise ValueError(f"{operation} was not signed by user_address")
        if operation == EARN_OP_DEPOSIT:
            assert_nonce_free(user_address, nonce)

        tx_id = str(uuid.uuid4())
        now = int(time.time())
        db_write(
            get_db(),
            """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, input_nonce, input_signature,
                status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tx_id, operation, pool_id_hex, user_address.lower(),
                pool["token_id"], amount,
                user_address.lower(), nonce, signature, nonce, signature,
                EARN_STATUS_SCHEDULED, now, now,
            ),
        )
        logger.info(
            "earn %s %s scheduled: user=%s pool=%s amount=%s nonce=%s",
            operation, tx_id, user_address, pool_id_hex, amount, nonce,
        )
        return {"id": tx_id, "status": EARN_STATUS_SCHEDULED}

    def schedule_deposit(
        self, *, pool_id_hex: str, user_address: str, amount: str, nonce: int, signature: str
    ) -> dict:
        return self._schedule(
            operation=EARN_OP_DEPOSIT, pool_id_hex=pool_id_hex,
            user_address=user_address, amount=amount, nonce=nonce, signature=signature,
        )

    def schedule_withdraw(
        self, *, pool_id_hex: str, user_address: str, amount: str, nonce: int, signature: str
    ) -> dict:
        return self._schedule(
            operation=EARN_OP_WITHDRAW, pool_id_hex=pool_id_hex,
            user_address=user_address, amount=amount, nonce=nonce, signature=signature,
        )

    async def deposit(
        self,
        pool_id_hex: str,
        user_address: str,
        amount: str,
        nonce: int,
        signature: str,
        scheduled_id: Optional[str] = None,
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
        self._assert_pool_custody(pool)

        # Probe before minting anything: a paused vault or stale oracle means
        # the routing step is guaranteed to fail, and refusing here keeps the
        # user's funds in their balance instead of minting shares that land
        # undeployed. Withdraw stays ungated — exits must not depend on the
        # external protocol looking healthy.
        if not await self._registry.get(pool_id_hex).is_healthy():
            raise ValueError(
                "Pool strategy is unhealthy; deposits are temporarily refused"
            )

        sig_bytes = bytes.fromhex(signature.removeprefix("0x"))

        async with self._pools_tx_lock:
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
                existing_id=scheduled_id,
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
                tx_hash, block = await self._submit_and_settle(
                    tx_id,
                    function_name="deposit",
                    args=[
                        pool_id,
                        Web3.to_checksum_address(user_address),
                        int(amount),
                        nonce,
                        sig_bytes,
                    ],
                )
            except ReceiptUnknown as exc:
                logger.warning("Earn deposit %s receipt unknown; left pending for recovery", tx_id)
                return {
                    "deposit_id": tx_id,
                    "pool_id": pool_id_hex,
                    "amount": amount,
                    "shares_minted": None,
                    "exchange_rate": None,
                    "tx_hash": str(exc),
                    "status": EARN_STATUS_PENDING,
                    "error": None,
                }
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

            self._update_transaction(
                tx_id, status=EARN_STATUS_UNDEPLOYED, tx_hash=tx_hash
            )
            await self._record_share_delta(tx_id, pool_id, block, amount)

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
                self._update_transaction(tx_id, error=deploy_error)
            else:
                self._update_transaction(tx_id, status=EARN_STATUS_COMPLETED)
                await self._complete_undeployed_if_clear(pool_id_hex)

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
        scheduled_id: Optional[str] = None,
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
        # Custody still has to line up: the payout is debited from the account
        # this service signs for, so a mismatch would revert on accounting
        # after the reclaim had already moved funds.
        self._assert_pool_custody(pool)

        async with self._pools_tx_lock:
            # Sync inside the lock, before moving any strategy assets, so a
            # concurrent deposit can never sync the transient balance this
            # reclaim is about to create.
            synced = await self.sync_total_assets(pool_id_hex)
            # Burning against a stale denominator cannot inflate an unseeded
            # pool, so those exits stay best-effort. A seeded pool is
            # different: the seed is senior, so a withdrawal priced on a
            # denominator that no longer holds would pay the user out of
            # foundation principal.
            if synced is None and await asyncio.to_thread(
                partial(self.get_seeded_assets, pool_id, fresh=True)
            ):
                raise ValueError(
                    "Pool valuation could not be confirmed; withdraw refused. "
                    "Retry shortly."
                )
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

            # Signed by the pool's own account: accounting checks the signature
            # against pool_address, which is the earn account, not the swap LP.
            pool_signature = sign_transfer(
                private_key=self.settings.earn_pool_secret_key,
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
                existing_id=scheduled_id,
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
                tx_hash, block = await self._submit_and_settle(
                    tx_id,
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
            except ReceiptUnknown as exc:
                # No rollback: the burn may have landed and needs the funds.
                logger.warning("Earn withdraw %s receipt unknown; left pending for recovery", tx_id)
                return {
                    "withdraw_id": tx_id,
                    "pool_id": pool_id_hex,
                    "amount": amount,
                    "shares_burned": None,
                    "exchange_rate": None,
                    "tx_hash": str(exc),
                    "status": EARN_STATUS_PENDING,
                    "error": None,
                }
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

            await self._record_share_delta(tx_id, pool_id, block, amount)

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

    async def deploy_idle(self, pool_id_hex: str) -> int:
        """Move whatever is sitting in the pool's accounting balance into the
        pool's strategy, and return how much moved.

        Seed principal is paid into the pool account from outside and then
        recorded, so it arrives idle and earns nothing until it is deployed.
        The same is true of a deposit whose routing failed and of a reclaim
        left over from a withdrawal that reverted, so this deploys the whole
        idle balance rather than tracking which part is which.

        Net assets do not change: the funds move from one side of the backing
        figure to the other. The lock is what makes that safe, since it holds
        across the whole bridge, so no deposit or withdrawal can read the
        balance while the funds are in flight and no withdrawal can have its
        reclaim deployed out from under it.
        """
        strategy = self._registry.get(pool_id_hex)
        if strategy.name == "manual":
            return 0

        async with self._pools_tx_lock:
            # A bridge still listed as pending may or may not have landed, so
            # the balances below cannot be trusted until it clears. The next
            # sweep sees the settled picture.
            if await strategy.in_flight_assets() > 0:
                return 0
            idle = await strategy.idle_assets()
            stranded = await strategy.stranded_assets()
            minimum = await strategy.min_deploy_amount()
            # The strategy bridges only what is not already on the earn
            # account, so idle and stranded funds deploy together.
            amount = idle + stranded
            if amount <= 0 or amount < minimum:
                if amount == 0:
                    self._complete_undeployed(pool_id_hex)
                return 0
            if not await strategy.is_healthy():
                logger.info(
                    "Idle deploy pool=%s: %d idle but the strategy is unhealthy; leaving it",
                    pool_id_hex, amount,
                )
                return 0
            logger.info("Idle deploy pool=%s: routing %d into %s",
                        pool_id_hex, amount, strategy.name)
            await self._route_to_strategy(pool_id_hex, amount)
            # Backing is unchanged, but totalAssets is written from a reading
            # taken before the move, so refresh it while the lock still
            # guarantees nothing else is mid-flight.
            await self.sync_total_assets(pool_id_hex)
            left = await strategy.idle_assets()
            if (left == 0 or left < minimum) and await strategy.stranded_assets() == 0:
                self._complete_undeployed(pool_id_hex)
            return amount

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
        gross = external + idle
        if gross == 0:
            return on_chain_total
        pool_id = bytes.fromhex(pool_id_hex.removeprefix("0x"))
        try:
            seeded = await asyncio.to_thread(self.get_seeded_assets, pool_id)
            shares = self.get_pool(pool_id)["total_shares"]
        except Exception:
            logger.exception(
                "seed read failed pool=%s; falling back to on-chain", pool_id_hex
            )
            return on_chain_total
        return self._net_of_seed(gross, seeded, shares)

    async def strict_total_assets(
        self,
        pool_id_hex: str,
        on_chain_total: int,
        total_shares: Optional[int] = None,
    ) -> Optional[int]:
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
        gross = external + idle
        if gross == 0:
            return on_chain_total
        pool_id = bytes.fromhex(pool_id_hex.removeprefix("0x"))
        try:
            seeded = await asyncio.to_thread(self.get_seeded_assets, pool_id)
            shares = (
                total_shares
                if total_shares is not None
                else self.get_pool(pool_id)["total_shares"]
            )
        except Exception:
            logger.exception("seed read failed pool=%s; no rate snapshot", pool_id_hex)
            return None
        return self._net_of_seed(gross, seeded, shares)

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
        assets = await self.strict_total_assets(
            pool_id_hex, before["total_assets"], total_shares=int(before["total_shares"])
        )
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
        awaiting redeploy) MINUS protocol-owned seed principal, which sits in
        the same balance but backs no shares. Counting only the strategy would
        understate the denominator whenever funds sit idle — including right after a failed
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

        gross = external + idle
        pool = self.get_pool(pool_id)
        seeded = await asyncio.to_thread(partial(self.get_seeded_assets, pool_id, fresh=True))

        if seeded > 0 and pool["total_shares"] == 0:
            # Nobody holds a claim yet, so everything the pool holds is still
            # the seed's, yield included. Roll it into the baseline instead of
            # leaving it as user assets the first depositor would arrive to
            # find already on the books.
            if gross != seeded:
                try:
                    await asyncio.to_thread(
                        self.sapphire.execute_contract_call,
                        contract_address=self.contract_address,
                        abi=EARN_MANAGER_ABI,
                        function_name="setSeededAssets",
                        args=[pool_id, gross],
                    )
                except Exception:
                    logger.exception(
                        "setSeededAssets tx failed pool=%s old=%d new=%d",
                        pool_id_hex, seeded, gross,
                    )
                    return None
            return 0 if on_chain == 0 else await self._write_total_assets(pool_id, pool_id_hex, on_chain, 0)

        backing = self._net_of_seed(gross, seeded, pool["total_shares"])
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

        return await self._write_total_assets(pool_id, pool_id_hex, on_chain, backing)

    async def _write_total_assets(
        self, pool_id: bytes, pool_id_hex: str, on_chain: int, target: int
    ) -> Optional[int]:
        try:
            await asyncio.to_thread(
                self.sapphire.execute_contract_call,
                contract_address=self.contract_address,
                abi=EARN_MANAGER_ABI,
                function_name="syncTotalAssets",
                args=[pool_id, target],
            )
        except Exception:
            logger.exception(
                "syncTotalAssets tx failed pool=%s old=%d new=%d",
                pool_id_hex, on_chain, target,
            )
            return None
        logger.info(
            "syncTotalAssets succeeded pool=%s old=%d new=%d",
            pool_id_hex, on_chain, target,
        )
        return target

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
        existing_id: Optional[str] = None,
    ) -> str:
        if existing_id is not None:
            # Queued path: the row was written when the request came in. Fill in
            # what execution settled on — a withdraw signs with the pool's key,
            # not the caller's — and move it out of the queue.
            self._update_transaction(
                existing_id,
                # token_id is only knowable from the pool, which scheduling does
                # not read. Fill it here or the row stays blank for every
                # consumer that keys on it, the unsettled feed included.
                token_id=token_id,
                signer_address=signer_address.lower(),
                nonce=nonce,
                signature=signature,
                consent_signer=consent_signer.lower() if consent_signer else None,
                status=EARN_STATUS_PENDING,
            )
            logger.info(
                "earn %s %s signed: signer=%s to=%s token=%s amount=%s nonce=%s",
                operation, existing_id, signer_address, user_address, token_id,
                amount, nonce,
            )
            return existing_id
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

    def shares_moved_in_block(self, pool_id: bytes, block: int) -> tuple[int, int]:
        """The pool's totalShares change across one block, and that block's time.

        Per-user shares are confidential, but the worker submits one cashflow
        at a time, so this is that cashflow's movement. A third-party deposit
        in the same block would be folded in; the pool-wide check catches it.
        """
        def total_shares(at: int) -> int:
            pool = self._read_with_retry(
                partial(self._history.functions.pools(pool_id).call, block_identifier=at),
                "pools",
            )
            return int(pool[2])

        moved = total_shares(block) - total_shares(block - 1)
        return moved, int(self.sapphire.w3.eth.get_block(block)["timestamp"])

    @staticmethod
    def _settlement(amount: str, delta: int, settled_at: int) -> dict:
        # What this cashflow paid, not the pool's later ratio.
        rate = str(Decimal(int(amount)) / Decimal(abs(delta))) if delta else None
        return {"shares_delta": str(delta), "exchange_rate": rate, "settled_at": settled_at}

    async def _record_share_delta(
        self, tx_id: str, pool_id: bytes, block: int, amount: str
    ) -> None:
        """Persist this cashflow's signed share movement and settlement rate.

        A failed read leaves it NULL for ``repair_share_ledger``.
        """
        try:
            delta, settled_at = await asyncio.to_thread(
                self.shares_moved_in_block, pool_id, block
            )
        except Exception:
            logger.exception("Share movement read failed for %s", tx_id)
            return
        self._update_transaction(tx_id, **self._settlement(amount, delta, settled_at))

    async def repair_share_ledger(self) -> None:
        """Re-derive share movements in pools that disagree with totalShares.

        One wrong row blanks earned for the whole pool. Rows with a hash are
        re-read from their block, including failed rows that landed. A pool is
        rescanned only once its figures move or a scan missed a receipt.
        """
        for pool in await asyncio.to_thread(self.list_pools):
            pool_id_hex = pool["pool_id"]
            chain = int(pool["total_shares"])
            figures = (_settled_shares(pool_id_hex), chain)
            if figures[0] == chain or self._ledger_scanned.get(pool_id_hex) == figures:
                continue
            try:
                complete = await asyncio.to_thread(self._repair_pool_ledger, pool_id_hex)
            except Exception:
                logger.exception("earn ledger repair failed pool=%s", pool_id_hex)
                continue
            accounted = _settled_shares(pool_id_hex)
            logger.warning(
                "earn ledger repair done pool=%s accounted=%s chain=%d complete=%s",
                pool_id_hex, accounted, chain, complete,
            )
            if complete:
                self._ledger_scanned[pool_id_hex] = (accounted, chain)

    def _repair_pool_ledger(self, pool_id_hex: str) -> bool:
        """Returns whether every row's receipt could be read."""
        rows = get_db().execute(
            """SELECT id, operation, amount, status, tx_hash, shares_delta, updated_at
               FROM earn_transactions
               WHERE LOWER(pool_id) = ? AND tx_hash IS NOT NULL AND status IN (?, ?, ?)""",
            (pool_id_hex.lower(), EARN_STATUS_COMPLETED, EARN_STATUS_UNDEPLOYED, EARN_STATUS_FAILED),
        ).fetchall()
        landed, complete = [], True
        for row in rows:
            try:
                receipt = self.sapphire.w3.eth.get_transaction_receipt(row["tx_hash"])
            except TransactionNotFound:
                # Not proof it never landed, so rescan later.
                logger.warning("earn ledger repair: no receipt for %s", row["id"])
                complete = False
                continue
            if receipt["status"] == 1:
                landed.append((dict(row), receipt["blockNumber"]))

        blocks = Counter(block for _, block in landed)
        pool_id = bytes.fromhex(pool_id_hex.removeprefix("0x"))
        for row, block in landed:
            if blocks[block] > 1:
                logger.warning(
                    "earn ledger repair skipped %s: block %d holds another cashflow",
                    row["id"], block,
                )
                continue
            delta, settled_at = self.shares_moved_in_block(pool_id, block)
            if row["status"] != EARN_STATUS_FAILED and row["shares_delta"] == str(delta):
                continue
            fields = self._settlement(row["amount"], delta, settled_at)
            if row["status"] == EARN_STATUS_FAILED:
                # Dated when it landed. The idle deployer completes a deposit.
                status = (
                    EARN_STATUS_UNDEPLOYED if row["operation"] == EARN_OP_DEPOSIT
                    else EARN_STATUS_COMPLETED
                )
                fields.update(status=status, error=None, updated_at=settled_at)
                if row["operation"] == EARN_OP_WITHDRAW:
                    logger.warning(
                        "earn withdraw %s landed after its reclaim was rolled back; "
                        "check the pool's backing",
                        row["id"],
                    )
            else:
                fields["updated_at"] = row["updated_at"]
            self._update_transaction(row["id"], **fields)
            logger.warning(
                "earn ledger repaired %s pool=%s shares_delta %s -> %d status %s -> %s",
                row["id"], pool_id_hex, row["shares_delta"], delta,
                row["status"], fields.get("status", row["status"]),
            )
        return complete

    async def _complete_undeployed_if_clear(self, pool_id_hex: str) -> None:
        """A routed deposit sweeps whatever was left raw on the earn account, so
        once the pool has nothing idle either, earlier undeployed deposits
        have been put to work along with it. Bookkeeping only: a failed read
        here must not fail the deposit that just succeeded.
        """
        try:
            strategy = self._registry.get(pool_id_hex)
            if (
                strategy.name != "manual"
                and await strategy.idle_assets() == 0
                and await strategy.stranded_assets() == 0
                and await strategy.in_flight_assets() == 0
            ):
                self._complete_undeployed(pool_id_hex)
        except Exception:
            logger.warning("undeployed-row reconcile skipped pool=%s", pool_id_hex, exc_info=True)

    def _complete_undeployed(self, pool_id_hex: str) -> None:
        """Deposits whose routing failed sit as ``undeployed`` with their funds
        idle in the pool. Once the idle balance has been deployed those funds
        are working, so the rows have nothing left to wait for. ``updated_at``
        is left alone: value history reads it as the deposit's settlement time.
        """
        db_write(
            get_db(),
            "UPDATE earn_transactions SET status = ?, error = NULL "
            "WHERE lower(pool_id) = ? AND operation = ? AND status = ?",
            (EARN_STATUS_COMPLETED, pool_id_hex.lower(), EARN_OP_DEPOSIT, EARN_STATUS_UNDEPLOYED),
        )

    def _update_transaction(self, tx_id: str, **fields) -> None:
        db = get_db()
        fields.setdefault("updated_at", int(time.time()))
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
            try:
                earned = await asyncio.to_thread(
                    earned_active,
                    user_address,
                    pool["pool_id"],
                    shares,
                    underlying,
                    int(pool["total_shares"]),
                )
            except Exception:
                logger.exception("earned failed for pool %s", pool["pool_id"])
                earned = Earned(active=None, status=STATUS_LEDGER_INCOMPLETE)
            return {
                "pool_id": pool["pool_id"],
                "token_id": pool["token_id"],
                "shares": str(shares),
                "underlying_amount": str(underlying),
                "exchange_rate": _exchange_rate(effective_assets, pool["total_shares"]),
                "change_24h": change.amount if change else None,
                "change_24h_pct": change.pct if change else None,
                "earned_active": earned.active,
                "earned_active_status": earned.status,
                "cost_basis": earned.cost_basis,
                "deposit_count": earned.deposit_count,
                "first_deposit_at": earned.first_deposit_at,
            }

        results = await asyncio.gather(*[fetch_balance(p) for p in pools])
        return [b for b in results if b is not None]


_service_instance: Optional[VaultService] = None


def get_vault_service() -> VaultService:
    global _service_instance
    if _service_instance is None:
        _service_instance = VaultService()
    return _service_instance
