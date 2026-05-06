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
from src.core.eip712 import sign_transfer
from src.core.validation import sanitize_error, validate_address, validate_amount, validate_signature
from src.services.earn.registry import StrategyRegistry, get_strategy_registry

logger = logging.getLogger(__name__)

EARN_MANAGER_ABI = load_abi("EarnManager")

EARN_OP_DEPOSIT = "deposit"
EARN_OP_WITHDRAW = "withdraw"
EARN_STATUS_PENDING = "pending"
EARN_STATUS_COMPLETED = "completed"
EARN_STATUS_FAILED = "failed"


def _exchange_rate(total_assets: int, total_shares: int) -> str:
    if total_shares == 0:
        return "1.0"
    return str(Decimal(total_assets) / Decimal(total_shares))


class VaultService:
    def __init__(self, registry: Optional[StrategyRegistry] = None) -> None:
        self.settings = load_settings()
        self.sapphire = get_sapphire_client()
        self.accounting = get_accounting_client()
        self._withdraw_lock = asyncio.Lock()
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

    def get_pool(self, pool_id: bytes) -> dict:
        pool = self.contract.functions.getPool(pool_id).call()
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

    def get_user_shares(self, user_address: str, pool_id: bytes) -> int:
        # Read the public mapping directly. EarnManager.getUserShares() runs
        # an accounting.balanceOf auth gate that reverts when called from
        # this service (no msg.sender attribution); the public storage
        # getter skips the gate and returns the same value.
        return self.contract.functions.userShares(
            pool_id,
            Web3.to_checksum_address(user_address),
        ).call()

    def get_withdraw_nonce(self, user_address: str) -> int:
        return self.contract.functions.withdrawNonces(
            Web3.to_checksum_address(user_address),
        ).call()

    def convert_to_shares(self, pool_id: bytes, assets: int) -> int:
        return self.contract.functions.convertToShares(pool_id, assets).call()

    def convert_to_assets(self, pool_id: bytes, shares: int) -> int:
        return self.contract.functions.convertToAssets(pool_id, shares).call()

    def get_user_balance(self, user_address: str, pool_id: bytes) -> dict:
        shares = self.get_user_shares(user_address, pool_id)
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

        await self.sync_total_assets(pool_id_hex)

        sig_bytes = bytes.fromhex(signature.removeprefix("0x"))

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

        shares_before = self.get_user_shares(user_address, pool_id)

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
            self._update_transaction(tx_id, status=EARN_STATUS_FAILED, error=sanitize_error(str(exc)))
            return {
                "pool_id": pool_id_hex,
                "amount": amount,
                "shares_minted": None,
                "exchange_rate": None,
                "tx_hash": None,
                "status": "failed",
            }

        self._update_transaction(tx_id, status=EARN_STATUS_COMPLETED, tx_hash=tx_hash)

        await self._route_to_strategy(pool_id_hex, int(amount))

        try:
            shares_after = self.get_user_shares(user_address, pool_id)
            shares_minted = shares_after - shares_before
            pool_after = self.get_pool(pool_id)
            effective_assets = await self.effective_total_assets(pool_id_hex, pool_after["total_assets"])
            return {
                "pool_id": pool_id_hex,
                "amount": amount,
                "shares_minted": str(shares_minted),
                "exchange_rate": _exchange_rate(effective_assets, pool_after["total_shares"]),
                "tx_hash": tx_hash,
                "status": "completed",
            }
        except Exception:
            logger.warning("Post-tx read failed for deposit %s, returning degraded response", tx_id)
            return {
                "pool_id": pool_id_hex,
                "amount": amount,
                "shares_minted": None,
                "exchange_rate": None,
                "tx_hash": tx_hash,
                "status": "completed",
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

        - ``signature``: the user's EIP-712 ``Withdraw(user, poolId, amount, nonce)``
          consent in the EarnManager's domain, where ``nonce`` matches
          ``EarnManager.withdrawNonces[user]``. This is what authorizes the
          burn of the user's shares; without it any caller could force-eject
          any user from the pool.
        - The pool's accounting ``Transfer(pool -> user, ...)`` signature, which
          the service signs locally with the LP key. This is what authorizes
          accounting to debit the pool's balance.

        ``EarnManager.withdraw`` verifies the user signature first, then runs
        the share-burn and the accounting transfer with the pool signature.
        """
        validate_address(user_address, "user_address")
        validate_amount(amount, "amount")
        validate_signature(signature, "signature")

        # Pre-flight nonce check. The contract reads ``withdrawNonces[user]``
        # from storage and recovers the signer against that value, so a stale
        # client nonce produces a generic ``InvalidWithdrawSignature`` revert
        # only after we burn gas dispatching the tx. Comparing the client's
        # claimed nonce against the live storage value here turns that into
        # an immediate 400 with a clear message; the client refetches via
        # ``GET /v1/earn/withdraw/nonce`` and re-signs.
        current_nonce = self.get_withdraw_nonce(user_address)
        if nonce != current_nonce:
            raise ValueError(
                f"Stale withdraw nonce: expected {current_nonce}, got {nonce}. "
                "Refetch via /v1/earn/withdraw/nonce and re-sign."
            )

        pool_id = bytes.fromhex(pool_id_hex.removeprefix("0x"))
        pool = self.get_pool(pool_id)
        if pool["pool_address"] == "0x0000000000000000000000000000000000000000":
            raise ValueError("Pool not found")
        # No active check — users must always be able to exit paused pools.

        await self.sync_total_assets(pool_id_hex)

        shares = self.get_user_shares(user_address, pool_id)
        max_withdraw = self.convert_to_assets(pool_id, shares) if shares > 0 else 0
        if int(amount) > max_withdraw:
            raise ValueError("Insufficient shares for this withdrawal")

        await self._reclaim_from_strategy(pool_id_hex, int(amount))

        async with self._withdraw_lock:
            pool_nonce = await self.accounting.get_transfer_nonce(pool["pool_address"])

            pool_signature = sign_transfer(
                private_key=self.settings.liquidity_provider_secret_key,
                chain_id=self.settings.accounting_chain_id,
                verifying_contract=self.settings.accounting_contract_address,
                user_address=pool["pool_address"],
                to_address=user_address,
                token_id=pool["token_id"],
                amount=int(amount),
                nonce=pool_nonce,
            )

            pool_sig_bytes = bytes.fromhex(pool_signature.removeprefix("0x"))
            user_sig_bytes = bytes.fromhex(signature.removeprefix("0x"))

            tx_id = self._record_transaction(
                operation=EARN_OP_WITHDRAW,
                pool_id_hex=pool_id_hex,
                user_address=user_address,
                token_id=pool["token_id"],
                amount=amount,
                signer_address=pool["pool_address"],
                nonce=pool_nonce,
                signature=pool_signature,
            )

            shares_before = self.get_user_shares(user_address, pool_id)

            try:
                tx_hash = await asyncio.to_thread(
                    self.sapphire.execute_contract_call,
                    contract_address=self.contract_address,
                    abi=EARN_MANAGER_ABI,
                    function_name="withdraw",
                    args=[
                        pool_id,
                        Web3.to_checksum_address(user_address),
                        int(amount),
                        pool_nonce,
                        pool_sig_bytes,
                        user_sig_bytes,
                    ],
                )
            except Exception as exc:
                logger.exception("Earn withdraw %s failed", tx_id)
                self._update_transaction(tx_id, status=EARN_STATUS_FAILED, error=sanitize_error(str(exc)))
                return {
                    "pool_id": pool_id_hex,
                    "amount": amount,
                    "shares_burned": None,
                    "exchange_rate": None,
                    "tx_hash": None,
                    "status": "failed",
                }

        self._update_transaction(tx_id, status=EARN_STATUS_COMPLETED, tx_hash=tx_hash)

        try:
            shares_after = self.get_user_shares(user_address, pool_id)
            shares_burned = shares_before - shares_after
            pool_after = self.get_pool(pool_id)
            effective_assets = await self.effective_total_assets(pool_id_hex, pool_after["total_assets"])
            return {
                "pool_id": pool_id_hex,
                "amount": amount,
                "shares_burned": str(shares_burned),
                "exchange_rate": _exchange_rate(effective_assets, pool_after["total_shares"]),
                "tx_hash": tx_hash,
                "status": "completed",
            }
        except Exception:
            logger.warning("Post-tx read failed for withdraw %s, returning degraded response", tx_id)
            return {
                "pool_id": pool_id_hex,
                "amount": amount,
                "shares_burned": None,
                "exchange_rate": None,
                "tx_hash": tx_hash,
                "status": "completed",
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
        except Exception:
            logger.exception(
                "strategy.total_assets failed pool=%s strategy=%s; falling back to on-chain",
                pool_id_hex, strategy.name,
            )
            return on_chain_total
        return external if external > 0 else on_chain_total

    async def sync_total_assets(self, pool_id_hex: str) -> Optional[int]:
        """Push the strategy's live AUM into EarnManager.totalAssets so
        share math reflects accrued yield.

        No-ops for manual pools (no external capital) and when the strategy
        already matches on-chain. Best-effort: a failed sync logs and
        returns None instead of raising, so deposit and withdraw paths can
        still proceed against a slightly stale exchange rate. Calls
        `EarnManager.syncTotalAssets(poolId, newTotalAssets)` on Sapphire.
        """
        strategy = self._registry.get(pool_id_hex)
        if strategy.name == "manual":
            return None

        pool_id = bytes.fromhex(pool_id_hex.removeprefix("0x"))
        try:
            external = await strategy.total_assets()
        except Exception:
            logger.exception(
                "sync_total_assets read failed pool=%s strategy=%s",
                pool_id_hex, strategy.name,
            )
            return None
        if external <= 0:
            return None

        on_chain = self.get_pool(pool_id)["total_assets"]
        if external == on_chain:
            return on_chain

        try:
            await asyncio.to_thread(
                self.sapphire.execute_contract_call,
                contract_address=self.contract_address,
                abi=EARN_MANAGER_ABI,
                function_name="syncTotalAssets",
                args=[pool_id, external],
            )
        except Exception:
            logger.exception(
                "syncTotalAssets tx failed pool=%s old=%d new=%d",
                pool_id_hex, on_chain, external,
            )
            return None
        logger.info(
            "syncTotalAssets succeeded pool=%s old=%d new=%d",
            pool_id_hex, on_chain, external,
        )
        return external

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
    ) -> str:
        tx_id = str(uuid.uuid4())
        now = int(time.time())
        db = get_db()
        db_write(
            db,
            """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tx_id, operation, pool_id_hex, user_address.lower(), token_id, amount,
                signer_address.lower(), nonce, signature,
                EARN_STATUS_PENDING, now, now,
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

    async def get_all_balances(self, user_address: str) -> list[dict]:
        validate_address(user_address, "user_address")
        pools = await asyncio.to_thread(self.list_pools)

        async def fetch_balance(pool: dict) -> Optional[dict]:
            pool_id = bytes.fromhex(pool["pool_id"].removeprefix("0x"))
            shares = await asyncio.to_thread(self.get_user_shares, user_address, pool_id)
            if shares == 0:
                return None
            underlying = await asyncio.to_thread(self.convert_to_assets, pool_id, shares)
            effective_assets = await self.effective_total_assets(pool["pool_id"], pool["total_assets"])
            return {
                "pool_id": pool["pool_id"],
                "token_id": pool["token_id"],
                "shares": str(shares),
                "underlying_amount": str(underlying),
                "exchange_rate": _exchange_rate(effective_assets, pool["total_shares"]),
            }

        results = await asyncio.gather(*[fetch_balance(p) for p in pools])
        return [b for b in results if b is not None]


_service_instance: Optional[VaultService] = None


def get_vault_service() -> VaultService:
    global _service_instance
    if _service_instance is None:
        _service_instance = VaultService()
    return _service_instance
