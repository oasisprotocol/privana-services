"""Internal venue swap execution pipeline."""
import asyncio
import logging
import time
from typing import Optional

from web3 import Web3

from src.clients.accounting import get_accounting_client
from src.clients.sapphire import get_sapphire_client
from src.core.abi import load_abi
from src.core.config import load_settings
from src.core.db import db_write, get_db
from src.core.eip712 import sign_transfer
from src.core.validation import sanitize_error
from src.services.swap.executor import get_swap_executor
from src.services.swap.worker import lp_transfer_lock
from src.services.user_queue import users_with_inflight_work

logger = logging.getLogger(__name__)
# The Sapphire client node will relay transactions with up to 10 future nonces.
BATCH_SIZE = 10
# Number of seconds after quote expiry to consider an in-flight swap stale.
STALE_SWAP_TIMEOUT = 13
SWAP_MANAGER_ABI = load_abi("SwapManager")


class InternalSwapPipeline:
    def __init__(self) -> None:
        self.settings = load_settings()
        self._internal_lock = asyncio.Lock()

    def _update(self, swap_id: str, **fields) -> None:
        get_swap_executor()._update_swap(swap_id, **fields)

    def _claim(self, venue: str, limit: int) -> list[dict]:
        rows = get_db().execute(
            "SELECT * FROM swaps WHERE status = 'scheduled' AND venue = ? "
            "ORDER BY created_at, rowid LIMIT ?", (venue, limit),
        ).fetchall()
        claimed = []
        # Earn spends the same per-user transfer nonce from its own queue, so
        # the set starts with whatever either pipeline already has in flight.
        users = users_with_inflight_work()
        for row in rows:
            # Simulations use the current ledger state. A second swap from the
            # same user must wait for the first to consume its transfer nonce.
            if row["user_address"].lower() in users:
                continue
            changed = db_write(
                get_db(), "UPDATE swaps SET status = 'executing', updated_at = ? "
                "WHERE id = ? AND status = 'scheduled'", (int(time.time()), row["id"]),
            ).rowcount
            if changed:
                claimed.append(dict(row))
                users.add(row["user_address"].lower())
        return claimed

    def _args(self, swap: dict, lp_nonce: int) -> list:
        signature = sign_transfer(
            private_key=self.settings.liquidity_provider_secret_key,
            chain_id=self.settings.accounting_chain_id,
            verifying_contract=self.settings.accounting_contract_address,
            to_address=swap["user_address"], token_id=swap["to_token_id"],
            amount=int(swap["to_amount_estimate"]), nonce=lp_nonce,
        )
        return [
            Web3.to_checksum_address(swap["user_address"]),
            bytes.fromhex(swap["from_token_id"][2:]), int(swap["from_amount"]),
            int(swap["input_nonce"]), bytes.fromhex(swap["input_signature"].removeprefix("0x")),
            bytes.fromhex(swap["to_token_id"][2:]), int(swap["to_amount_estimate"]),
            lp_nonce, bytes.fromhex(signature.removeprefix("0x")),
        ]

    def _call(self, args: list) -> dict:
        return dict(contract_address=self.settings.swap_manager_contract_address,
                    abi=SWAP_MANAGER_ABI, function_name="swap", args=args)

    async def _is_stale(self, sapphire, swap: dict) -> bool:
        """True when the quote has expired and there is no sensible way it was completed."""
        quote = get_db().execute(
            "SELECT expires_at FROM quotes WHERE id = ?", (swap["quote_id"],)
        ).fetchone()
        if quote and int(time.time()) < quote["expires_at"] + STALE_SWAP_TIMEOUT:
            return False
        return True

    async def _settle(self, sapphire, swap: dict) -> None:
        try:
            receipt = await asyncio.to_thread(sapphire.wait_for_receipt, swap["swap_tx_hash"])
        except Exception as exc:
            logger.warning("internal swap %s tx_hash %s waiting for receipt failed: %s", swap["id"], swap["swap_tx_hash"], exc)
            if await self._is_stale(sapphire, swap):
                logger.warning("internal swap %s tx hash %s is stale",
                               swap["id"], swap["swap_tx_hash"])
                self._update(swap["id"], status="failed",
                             error=f"Transaction does not exist on-chain: {swap['swap_tx_hash']}")
            else:
                self._update(swap["id"], error=sanitize_error(str(exc)))
            return
        if receipt["status"] == 1:
            logger.info("internal swap %s settled", swap["id"])
            self._update(swap["id"], status="completed", error=None,
                         to_amount_actual=swap["to_amount_estimate"])
        else:
            logger.warning("internal swap %s tx hash %s failed", swap["id"], swap['swap_tx_hash'])
            self._update(swap["id"], status="failed",
                         error=f"Transaction reverted: {swap['swap_tx_hash']}")

    async def run_internal_once(self) -> None:
        async with self._internal_lock:
            await self._run_internal_once()

    async def _run_internal_once(self) -> None:
        active = get_db().execute(
            "SELECT * FROM swaps WHERE venue = 'internal' AND status = 'executing'"
        ).fetchall()
        queued = get_db().execute(
            "SELECT COUNT(*) FROM swaps WHERE venue = 'internal' AND status = 'scheduled'"
        ).fetchone()[0]
        logger.info("internal swap: %d active, %d queued", len(active), queued)
        if not active and queued == 0:
            return
        sapphire = await asyncio.to_thread(get_sapphire_client)
        async with lp_transfer_lock:
            if active:
                for row in active:
                    swap = dict(row)
                    if swap["swap_tx_hash"]:
                        await self._settle(sapphire, swap)
                    elif swap["output_signature"]:
                        logger.warning(
                            "internal swap %s submission outcome unknown; "
                            "manual recovery required",
                            swap["id"],
                        )
                        self._update(swap["id"],
                                     error="Submission outcome unknown; manual recovery required")
                    else:
                        logger.warning("internal swap %s changing status from 'executing' to 'scheduled'", swap["id"])
                        self._update(swap["id"], status="scheduled")
                # Wait until a subsequent iteration before submitting more.
                return
            accounting = get_accounting_client()
            tx_nonce = await asyncio.to_thread(
                sapphire.w3.eth.get_transaction_count,
                sapphire.account.address,
                "pending",
            )
            lp_nonce = await accounting.get_transfer_nonce(self.settings.liquidity_provider_address)
            swaps = self._claim("internal", BATCH_SIZE)
            prepared = []
            balances = {}
            for swap in swaps:
                try:
                    token = swap["to_token_id"]
                    if token not in balances:
                        balance = await accounting.get_lp_balance(token)
                        balances[token] = int(balance.balance)
                    amount = int(swap["to_amount_estimate"])
                    if balances[token] < amount:
                        raise ValueError("Insufficient liquidity for this swap")
                    # Every preflight uses the CURRENT LP nonce; the real calls
                    # below are signed with consecutive future nonces. Simulating
                    # a future nonce against current state would always revert.
                    await asyncio.to_thread(
                        sapphire.simulate_contract_call, **self._call(self._args(swap, lp_nonce))
                    )
                    balances[token] -= amount
                    prepared.append(swap)
                except Exception as exc:
                    logger.warning("internal swap %s preflight failed: %s", swap["id"], exc)
                    self._update(swap["id"], status="failed", error=sanitize_error(str(exc)))
            sent = []
            for index, swap in enumerate(prepared):
                try:
                    args = self._args(swap, lp_nonce + index)
                    self._update(swap["id"], output_nonce=lp_nonce + index,
                                 output_signature="0x" + args[-1].hex())
                    tx_hash = await asyncio.to_thread(
                        sapphire.submit_contract_call, **self._call(args), gas_limit=1_000_000, nonce=tx_nonce + index
                    )
                    self._update(swap["id"], swap_tx_hash=tx_hash)
                    swap["swap_tx_hash"] = tx_hash
                    sent.append(swap)
                except Exception as exc:
                    logger.exception("internal swap %s submission outcome unknown", swap["id"])
                    self._update(swap["id"],
                                 status="scheduled",
                                 error="Submission outcome unknown; retrying: "
                                 + sanitize_error(str(exc)))
                    # Don't leave a gap in the LP nonce sequence after a send error.
                    for unsent in prepared[index + 1:]:
                        self._update(unsent["id"], status="scheduled")
                    break
            # All sends in the batch precede any receipt wait. No next batch
            # is submitted until these transactions have settled.
            await asyncio.gather(*(self._settle(sapphire, swap) for swap in sent))


_internal_pipeline_instance: Optional[InternalSwapPipeline] = None

def get_internal_pipeline() -> InternalSwapPipeline:
    global _internal_pipeline_instance
    if _internal_pipeline_instance is None:
        _internal_pipeline_instance = InternalSwapPipeline()
    return _internal_pipeline_instance
