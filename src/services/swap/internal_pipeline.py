"""Internal venue swap execution pipeline."""
import asyncio
import logging
import time
from typing import Optional

from web3 import Web3

from src.clients.accounting import get_accounting_client
from src.clients.lifi import LIFI_DEFAULT_SLIPPAGE_BPS
from src.clients.sapphire import get_sapphire_client
from src.core.abi import load_abi
from src.core.config import load_settings
from src.core.db import db_write, get_db
from src.core.eip712 import sign_transfer
from src.core.fee_policy import resolve_internal_fee
from src.core.fees import calculate_fee
from src.core.validation import sanitize_error
from src.services.swap.executor import get_swap_executor
from src.services.swap.quote_service import get_quote_service
from src.services.swap.worker import lp_transfer_lock
from src.services.user_queue import users_with_inflight_work

logger = logging.getLogger(__name__)
# The Sapphire client node will relay transactions with up to 10 future nonces.
BATCH_SIZE = 10
# Seconds after submission, beyond the receipt wait, before a swap whose
# transaction cannot be found is considered never mined.
STALE_SWAP_TIMEOUT = 60
SWAP_MANAGER_ABI = load_abi("SwapManager")
# Each swap is re-priced when it runs, so a quote can be used long after it was
# made. The batch holds the LP transfer lock meanwhile, so a slow price must
# not hold it for long.
REPRICE_TIMEOUT_SEC = 5
# A swap that cannot be priced goes back to the queue, and fails once it has
# waited this long since it was scheduled.
PRICING_GIVE_UP_SEC = 600


class PricingUnavailable(RuntimeError):
    """No current price for the swap; nothing about the swap itself is wrong."""


def _payout(swap: dict) -> int:
    """What the LP pays: the amount priced at execution, once there is one."""
    return int(swap.get("to_amount_executed") or swap["to_amount_estimate"])


class InternalSwapPipeline:
    def __init__(self) -> None:
        self.settings = load_settings()
        self._internal_lock = asyncio.Lock()
        self._logged_counts: Optional[tuple[int, int, int]] = None

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
            amount=_payout(swap), nonce=lp_nonce,
        )
        return [
            Web3.to_checksum_address(swap["user_address"]),
            bytes.fromhex(swap["from_token_id"][2:]), int(swap["from_amount"]),
            int(swap["input_nonce"]), bytes.fromhex(swap["input_signature"].removeprefix("0x")),
            bytes.fromhex(swap["to_token_id"][2:]), _payout(swap),
            lp_nonce, bytes.fromhex(signature.removeprefix("0x")),
        ]

    async def _fresh_payout(self, swap: dict) -> int:
        """The output at the current price, net of the fee due now. Below the
        quote's floor the price moved beyond its slippage and the swap fails."""
        quote = get_db().execute(
            "SELECT to_amount_min, slippage_bps FROM quotes WHERE id = ?", (swap["quote_id"],)
        ).fetchone()
        if quote is None:
            raise ValueError("Scheduled swap quote not found")
        quotes = get_quote_service()
        try:
            from_info = await quotes.accounting.get_token_info(swap["from_token_id"])
            to_info = await quotes.accounting.get_token_info(swap["to_token_id"])
            routes = await asyncio.wait_for(
                quotes._price_route(
                    from_info, to_info, swap["from_amount"],
                    quote["slippage_bps"] or LIFI_DEFAULT_SLIPPAGE_BPS,
                ),
                REPRICE_TIMEOUT_SEC,
            )
        except Exception as exc:
            raise PricingUnavailable(type(exc).__name__) from exc
        if not routes.get("routes"):
            raise PricingUnavailable("no route")
        route = routes["routes"][0]
        # Checked again at the current price: a quote can be used long after it was made.
        quotes._enforce_max_swap_size(route)
        # Fees are those due now, so an exemption that has ended no longer applies.
        decision = resolve_internal_fee(swap["user_address"], int(time.time()))
        net, _ = calculate_fee(int(route["toAmount"]), decision.fee_bps)
        floor = int(quote["to_amount_min"])
        if net < floor:
            raise ValueError(f"execution quote below floor: net={net} floor={floor}")
        return net

    def _call(self, args: list) -> dict:
        return dict(contract_address=self.settings.swap_manager_contract_address,
                    abi=SWAP_MANAGER_ABI, function_name="swap", args=args)

    def _defer_unpriced(self, swap: dict, exc: Exception) -> None:
        if int(time.time()) - swap["created_at"] >= PRICING_GIVE_UP_SEC:
            logger.warning("internal swap %s failed: no price for %ds", swap["id"], PRICING_GIVE_UP_SEC)
            self._update(swap["id"], status="failed", error="pricing unavailable")
            return
        logger.info("internal swap %s waiting for a price: %s", swap["id"], exc)
        self._update(swap["id"], status="scheduled")

    async def _is_stale(self, sapphire, swap: dict) -> bool:
        """True when the transaction has been out long enough that it would have been mined."""
        submitted_at = swap.get("submitted_at") or swap["updated_at"]
        return int(time.time()) >= submitted_at + STALE_SWAP_TIMEOUT

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
                         to_amount_actual=str(_payout(swap)))
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
        lifi_running = get_db().execute(
            "SELECT COUNT(*) FROM swaps WHERE venue = 'lifi' AND status IN ('executing', 'refunding')"
        ).fetchone()[0]
        # This runs every second and ROFL keeps a short log, so an unchanged
        # line would push the errors worth reading out of it.
        counts = (len(active), queued, lifi_running)
        if counts != self._logged_counts:
            logger.info(
                "swaps: internal %d active, %d queued; lifi %d in progress", *counts,
            )
            self._logged_counts = counts
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
            # The simulation runs at the quoted amount first: it is cheap, and
            # only a swap that passes is worth an external price lookup.
            candidates = []
            for swap in swaps:
                try:
                    # Every preflight uses the CURRENT LP nonce; the real calls
                    # below are signed with consecutive future nonces. Simulating
                    # a future nonce against current state would always revert.
                    await asyncio.to_thread(
                        sapphire.simulate_contract_call, **self._call(self._args(swap, lp_nonce))
                    )
                    candidates.append(swap)
                except Exception as exc:
                    logger.warning("internal swap %s preflight failed: %s", swap["id"], exc)
                    self._update(swap["id"], status="failed", error=sanitize_error(str(exc)))
            # A swap signed for an amount before keeps it: that signature may
            # already have paid out.
            prices = await asyncio.gather(*(
                self._fresh_payout(swap) for swap in candidates if not swap.get("to_amount_executed")
            ), return_exceptions=True)
            priced = iter(prices)
            prepared = []
            balances = {}
            for swap in candidates:
                try:
                    if not swap.get("to_amount_executed"):
                        price = next(priced)
                        if isinstance(price, PricingUnavailable):
                            self._defer_unpriced(swap, price)
                            continue
                        if isinstance(price, BaseException):
                            raise price
                        swap["to_amount_executed"] = str(price)
                    token = swap["to_token_id"]
                    if token not in balances:
                        balance = await accounting.get_lp_balance(token)
                        balances[token] = int(balance.balance)
                    amount = _payout(swap)
                    if balances[token] < amount:
                        raise ValueError("Insufficient liquidity for this swap")
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
                                 output_signature="0x" + args[-1].hex(),
                                 to_amount_executed=swap["to_amount_executed"])
                    tx_hash = await asyncio.to_thread(
                        sapphire.submit_contract_call, **self._call(args), gas_limit=1_000_000, nonce=tx_nonce + index
                    )
                    submitted_at = int(time.time())
                    self._update(swap["id"], swap_tx_hash=tx_hash, submitted_at=submitted_at)
                    swap["swap_tx_hash"] = tx_hash
                    swap["submitted_at"] = submitted_at
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
