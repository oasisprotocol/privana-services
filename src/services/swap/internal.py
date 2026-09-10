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
from src.models.swap import SwapRecord, SwapStatus, SwapVenue
from src.services.swap.quote_service import load_unexpired_quote
from src.services.swap.worker import SwapWorker

logger = logging.getLogger(__name__)

SWAP_MANAGER_ABI = load_abi("SwapManager")
SWAP_GAS_LIMIT = 1000000
LP_RETRY_ATTEMPTS = 100
LP_RETRY_DELAY_SEC = 3.0

# Freshly scheduled swaps are submitted BATCH_SIZE at a time: the LP transfer
# nonce and the sender's tx nonce are each read once, then handed out as
# nonce, nonce+1, ... so every tx in the group can be broadcast before any of
# them confirms and they land in the same block.
BATCH_SIZE = 5

# Internal rows left EXECUTING by a restart are simply retried: if the first
# attempt did land, the retry is rejected pre-flight on the spent transfer
# nonce, so claiming executing rows is crash recovery rather than double-execution.
INTERNAL_CLAIM_SQL = """
    SELECT * FROM swaps
    WHERE venue = ? AND status IN (?, ?)
    ORDER BY created_at, id
"""
INTERNAL_CLAIM_PARAMS = (
    SwapVenue.INTERNAL.value,
    SwapStatus.SCHEDULED.value,
    SwapStatus.EXECUTING.value,
)


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

    async def execute_swaps(self, swaps: list[SwapRecord]) -> None:
        """Entry point for one internal-worker pass.

        Rows left EXECUTING by a crash are recovered one at a time through
        the single-swap retry path below, which safely no-ops a retry that
        already landed (the spent input nonce rejects it pre-flight).
        Freshly scheduled rows are grouped into batches of BATCH_SIZE and
        submitted together so they can share a block instead of one call to
        `swap` per block.
        """
        for swap in swaps:
            if swap.status == SwapStatus.EXECUTING.value:
                await self.execute_swap(swap)

        scheduled = [s for s in swaps if s.status == SwapStatus.SCHEDULED.value]
        for i in range(0, len(scheduled), BATCH_SIZE):
            await self._run_batch(scheduled[i : i + BATCH_SIZE])

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

    async def _run_batch(self, swaps: list[SwapRecord]) -> None:
        """Sign and broadcast up to BATCH_SIZE scheduled swaps as one group.

        Each tx still moves the LP's on-ledger transfer nonce by exactly one,
        but assigning nonce, nonce+1, ... up front (instead of re-reading it
        per swap) lets every tx be sent before any of them confirms. Before
        submitting, every candidate is simulated as if it were first in line
        (signed with the batch's shared starting nonce rather than its
        actual assigned one) to catch reverts unrelated to nonce ordering -
        bad signatures, a spent input nonce, insufficient balance - before
        paying gas; it cannot validate the batch's actual nonce sequence,
        since a member's assumed nonce is only valid once every member ahead
        of it has landed. A swap that fails one of these checks - or the
        batch-wide nonce reads below, which aren't any single swap's fault -
        is put back to SCHEDULED to retry fresh next pass, rather than
        failed outright: the cause (a liquidity dip, an RPC hiccup, a nonce
        race with a concurrent Li.Fi transfer) can resolve on its own. An
        expired quote is the one exception: retrying can never succeed, so
        that fails immediately.

        A tx can still revert on-chain despite passing simulation (e.g.
        because a concurrent Li.Fi transfer spent the LP nonce after
        simulation but before broadcast) - only visible once every receipt
        comes back, checked below. Because every member's assigned nonce is
        one past the previous member's, a revert at position i leaves the
        on-ledger nonce where member i left it, which guarantees every
        member submitted after it also reverts. `submitted` is already in
        nonce order, so the first reverted receipt is the one genuine
        failure; everything submitted after it is a cascade victim rather
        than independently at fault, and is put back to SCHEDULED instead
        of failed.
        """
        if not swaps:
            return
        for swap in swaps:
            self._update_swap(swap.id, status=SwapStatus.EXECUTING.value)

        try:
            lp_nonce = await self.accounting.get_transfer_nonce(
                self.settings.liquidity_provider_address
            )
            tx_nonce = await asyncio.to_thread(self.sapphire.get_pending_nonce)
        except Exception:
            logger.exception(
                "Failed to read nonces for a swap batch; retrying next pass"
            )
            for swap in swaps:
                self._update_swap(swap.id, status=SwapStatus.SCHEDULED.value)
            return

        candidates: list[tuple[SwapRecord, list]] = []
        for swap in swaps:
            try:
                load_unexpired_quote(swap.quote_id)
            except Exception as exc:
                logger.exception("Swap %s failed", swap.id)
                self._update_swap(
                    swap.id, status=SwapStatus.FAILED.value, error=sanitize_error(str(exc))
                )
                continue
            try:
                lp_balance = await self.accounting.get_lp_balance(swap.to_token_id)
                if int(lp_balance.balance) < int(swap.to_amount_estimate):
                    raise ValueError("Insufficient liquidity for this swap")
                _, _, sim_args = self._sign_output(swap, lp_nonce)
                candidates.append((swap, sim_args))
            except Exception as exc:
                logger.warning(
                    "swap %s not ready this pass, retrying next batch: %s",
                    swap.id, sanitize_error(str(exc)),
                )
                self._update_swap(swap.id, status=SwapStatus.SCHEDULED.value)

        sim_results = await asyncio.gather(
            *(self._simulate_swap(swap.id, args) for swap, args in candidates),
            return_exceptions=True,
        )

        submitted: list[tuple[SwapRecord, str]] = []
        for (swap, _), sim_result in zip(candidates, sim_results):
            if isinstance(sim_result, Exception):
                logger.warning(
                    "swap %s would revert this pass, retrying next batch: %s",
                    swap.id, sanitize_error(str(sim_result)),
                )
                self._update_swap(swap.id, status=SwapStatus.SCHEDULED.value)
                continue
            try:
                tx_hash = await self._submit_swap(swap, lp_nonce, tx_nonce)
                submitted.append((swap, tx_hash))
                lp_nonce += 1
                tx_nonce += 1
            except Exception as exc:
                logger.exception("Swap %s failed to submit", swap.id)
                self._update_swap(
                    swap.id, status=SwapStatus.FAILED.value, error=sanitize_error(str(exc))
                )

        results = await asyncio.gather(
            *(
                asyncio.to_thread(self.sapphire.wait_for_receipt, tx_hash)
                for _, tx_hash in submitted
            ),
            return_exceptions=True,
        )
        cascading = False
        for (swap, tx_hash), result in zip(submitted, results):
            if cascading:
                logger.warning(
                    "swap %s not confirmed: an earlier batch member reverted "
                    "first, retrying next batch",
                    swap.id,
                )
                self._update_swap(swap.id, status=SwapStatus.SCHEDULED.value)
            elif isinstance(result, Exception):
                cascading = True
                logger.warning(
                    "swap %s did not confirm: %s", swap.id, sanitize_error(str(result))
                )
                self._update_swap(
                    swap.id, status=SwapStatus.FAILED.value, error=sanitize_error(str(result))
                )
            else:
                self._update_swap(
                    swap.id, status=SwapStatus.COMPLETED.value, swap_tx_hash=tx_hash
                )

    async def _swap_with_retries(self, swap: SwapRecord) -> str:
        """Broadcast the swap, re-reading the LP nonce on every attempt.

        A concurrent Li.Fi transfer can spend the LP nonce between our read and
        our broadcast, which reverts the swap. Retry in this case until it
        succeeds or expiry is reached.
        """
        last_exc: Exception = RuntimeError("swap not attempted")
        for attempt in range(LP_RETRY_ATTEMPTS):
            if attempt:
                await asyncio.sleep(LP_RETRY_DELAY_SEC)
            try:
                return await self._sign_and_broadcast(swap)
            except Exception as exc:
                last_exc = exc
                if not await self._lp_nonce_outdated(swap.id):
                    raise
                logger.warning(
                    "swap %s attempt %d/%d failed on LP nonce conflict: %s",
                    swap.id, attempt + 1, LP_RETRY_ATTEMPTS, sanitize_error(str(exc)),
                )
        raise last_exc

    async def _lp_nonce_outdated(self, swap_id: str) -> bool:
        """True when the LP's on-ledger nonce moved past the one this attempt signed."""
        signed: Optional[int] = None
        row = get_db().execute(
            "SELECT output_nonce FROM swaps WHERE id = ?", (swap_id,)
        ).fetchone()
        if row is not None:
            signed = row["output_nonce"]
        if signed is None:
            return False
        try:
            current = await self.accounting.get_transfer_nonce(
                self.settings.liquidity_provider_address
            )
        except Exception:
            logger.debug("could not re-read LP nonce after swap %s failed", swap_id)
            return False
        return current > signed

    async def _sign_and_broadcast(self, swap: SwapRecord) -> str:
        lp_nonce = await self.accounting.get_transfer_nonce(
            self.settings.liquidity_provider_address
        )
        swap_args = await self._build_signed_swap_args(swap, lp_nonce)

        await self._simulate_swap(swap.id, swap_args)

        return await asyncio.to_thread(
            self.sapphire.execute_contract_call,
            contract_address=self.settings.swap_manager_contract_address,
            abi=SWAP_MANAGER_ABI,
            function_name="swap",
            args=swap_args,
            gas_limit=SWAP_GAS_LIMIT,
        )

    async def _submit_swap(self, swap: SwapRecord, lp_nonce: int, tx_nonce: int) -> str:
        """Broadcast a batch member with its actual assigned LP and tx nonce."""
        swap_args = await self._build_signed_swap_args(swap, lp_nonce)
        return await asyncio.to_thread(
            self.sapphire.submit_contract_call,
            contract_address=self.settings.swap_manager_contract_address,
            abi=SWAP_MANAGER_ABI,
            function_name="swap",
            args=swap_args,
            gas_limit=SWAP_GAS_LIMIT,
            nonce=tx_nonce,
        )

    def _sign_output(self, swap: SwapRecord, lp_nonce: int) -> tuple[str, str, list]:
        """Sign the LP's output leg for `lp_nonce` and build the full `swap()` args.

        Pure and local: no I/O, no DB write. Used both for a real submission
        (with the nonce this swap will actually be broadcast with) and, in a
        batch, for a throwaway signature used only to simulate.
        """
        user_address = Web3.to_checksum_address(swap.user_address)
        output_signature = sign_transfer(
            private_key=self.settings.liquidity_provider_secret_key,
            chain_id=self.settings.accounting_chain_id,
            verifying_contract=self.settings.accounting_contract_address,
            to_address=user_address,
            token_id=swap.to_token_id,
            amount=int(swap.to_amount_estimate),
            nonce=lp_nonce,
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
        return user_address, output_signature, swap_args

    async def _build_signed_swap_args(self, swap: SwapRecord, lp_nonce: int) -> list:
        user_address, output_signature, swap_args = self._sign_output(swap, lp_nonce)
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
        return swap_args

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


async def _exec_internal_batch(swaps: list[SwapRecord]) -> None:
    await get_internal_pipeline().execute_swaps(swaps)


_internal_worker: Optional[SwapWorker] = None


def get_internal_worker() -> SwapWorker:
    global _internal_worker
    if _internal_worker is None:
        _internal_worker = SwapWorker(
            name="internal",
            claim_sql=INTERNAL_CLAIM_SQL,
            claim_params=INTERNAL_CLAIM_PARAMS,
            execute_batch=_exec_internal_batch,
        )
    return _internal_worker
