import asyncio
import logging
import time
from typing import Any, Optional

from web3 import Web3

from src.clients.accounting import get_accounting_client
from src.clients.sapphire import get_sapphire_client
from src.core.abi import load_abi
from src.core.config import load_settings
from src.core.db import db_write, get_db
from src.core.eip712 import sign_transfer
from src.core.validation import sanitize_error
from src.models.swap import SwapRecord, SwapStatus
from src.services.swap.quote_service import load_unexpired_quote

logger = logging.getLogger(__name__)

SWAP_MANAGER_ABI = load_abi("SwapManager")
SWAP_GAS_LIMIT = 1000000
LP_RETRY_ATTEMPTS = 100
LP_RETRY_DELAY_SEC = 3.0


class InternalSwap:
    """Fills a swap from LP liquidity in one atomic SwapManager call."""

    def __init__(
        self,
        accounting: Optional[Any] = None,
        sapphire: Optional[Any] = None,
    ) -> None:
        self.settings = load_settings()
        self.accounting = accounting or get_accounting_client()
        self.sapphire = sapphire or get_sapphire_client()

    async def execute_swap(self, swap: SwapRecord) -> None:
        self._update_swap(swap.id, status=SwapStatus.EXECUTING.value)
        try:
            load_unexpired_quote(swap.quote_id)

            lp_balance = await self.accounting.get_lp_balance(swap.to_token_id)
            if int(lp_balance.balance) < int(swap.to_amount_estimate):
                raise ValueError("Insufficient liquidity for this swap")

            tx_hash = await self._swap_with_retries(swap)
            self._update_swap(
                swap.id, status=SwapStatus.COMPLETED.value, swap_tx_hash=tx_hash
            )
        except Exception as exc:
            logger.exception("Swap %s failed", swap.id)
            self._update_swap(
                swap.id,
                status=SwapStatus.FAILED.value,
                error=sanitize_error(str(exc)),
            )

    async def _swap_with_retries(self, swap: SwapRecord) -> str:
        """Broadcast the swap, re-reading the LP nonce on every attempt.

        A concurrent Li.Fi transfer can spend the nonce between our read and
        our broadcast, which reverts the swap. Re-signing against the advanced
        nonce is the recovery; replaying is harmless because the swap is atomic
        and both transfer nonces are single-use.
        """
        last_exc: Exception = RuntimeError("swap not attempted")
        for attempt in range(LP_RETRY_ATTEMPTS):
            if attempt:
                await asyncio.sleep(LP_RETRY_DELAY_SEC)
            try:
                return await self._sign_and_broadcast(swap)
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "swap %s attempt %d/%d failed: %s",
                    swap.id, attempt + 1, LP_RETRY_ATTEMPTS, sanitize_error(str(exc)),
                )
        raise last_exc

    async def _sign_and_broadcast(self, swap: SwapRecord) -> str:
        user_address = Web3.to_checksum_address(swap.user_address)
        lp_nonce = await self.accounting.get_transfer_nonce(
            self.settings.liquidity_provider_address
        )
        output_signature = sign_transfer(
            private_key=self.settings.liquidity_provider_secret_key,
            chain_id=self.settings.accounting_chain_id,
            verifying_contract=self.settings.accounting_contract_address,
            to_address=user_address,
            token_id=swap.to_token_id,
            amount=int(swap.to_amount_estimate),
            nonce=lp_nonce,
        )
        self._update_swap(
            swap.id,
            output_nonce=lp_nonce,
            output_signature=output_signature,
        )
        logger.info(
            "swap %s signed output: lp=%s to=%s token=%s amount=%s nonce=%s",
            swap.id,
            self.settings.liquidity_provider_address,
            user_address,
            swap.to_token_id,
            swap.to_amount_estimate,
            lp_nonce,
        )

        swap_args = [
            user_address,
            bytes.fromhex(swap.from_token_id.removeprefix("0x")),
            int(swap.from_amount),
            swap.input_nonce,
            bytes.fromhex(swap.input_signature.removeprefix("0x")),
            bytes.fromhex(swap.to_token_id.removeprefix("0x")),
            int(swap.to_amount_estimate),
            lp_nonce,
            bytes.fromhex(output_signature.removeprefix("0x")),
        ]

        await self._simulate_swap(swap.id, swap_args)

        return await asyncio.to_thread(
            self.sapphire.execute_contract_call,
            contract_address=self.settings.swap_manager_contract_address,
            abi=SWAP_MANAGER_ABI,
            function_name="swap",
            args=swap_args,
            gas_limit=SWAP_GAS_LIMIT,
        )

    async def _simulate_swap(self, swap_id: str, swap_args: list) -> None:
        """Dry-run the swap so a guaranteed revert costs no gas.

        The user's balance in the Accounting contract is access-gated, so a
        query signed with the LP key cannot read it and check it the way LP
        liquidity is checked above. Simulating the real call asks the contract
        the same question for free, and turns an on-chain revert — which costs
        the LP gas and reports back an opaque "failed" — into a reason string.
        """
        try:
            await asyncio.to_thread(
                self.sapphire.simulate_contract_call,
                contract_address=self.settings.swap_manager_contract_address,
                abi=SWAP_MANAGER_ABI,
                function_name="swap",
                args=swap_args,
            )
        except Exception as exc:
            raise ValueError(
                f"swap would revert on-chain: {sanitize_error(str(exc))}"
            ) from exc

    def _get_swap(self, swap_id: str) -> SwapRecord:
        db = get_db()
        row = db.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
        if row is None:
            raise ValueError(f"Swap {swap_id} not found")
        return SwapRecord(**dict(row))

    def _update_swap(self, swap_id: str, **fields) -> None:
        db = get_db()
        fields["updated_at"] = int(time.time())
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [swap_id]
        db_write(db, f"UPDATE swaps SET {set_clause} WHERE id = ?", tuple(values))


_pipeline_instance: Optional[InternalSwap] = None


def get_internal_pipeline() -> InternalSwap:
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = InternalSwap()
    return _pipeline_instance
