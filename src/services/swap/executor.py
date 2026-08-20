import asyncio
import logging
import time
import uuid
from typing import Optional

from web3 import Web3

from src.clients.accounting import get_accounting_client
from src.clients.sapphire import get_sapphire_client
from src.core.abi import load_abi
from src.core.config import load_settings
from src.core.db import db_write, get_db
from src.core.eip712 import recover_transfer_signer, sign_transfer
from src.core.validation import sanitize_error, validate_signature
from src.models.swap import SwapRecord, SwapStatus, SwapVenue
from src.services.swap.lifi_pipeline import get_lifi_pipeline

logger = logging.getLogger(__name__)

SWAP_MANAGER_ABI = load_abi("SwapManager")


class SwapExecutor:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.accounting = get_accounting_client()
        self.sapphire = get_sapphire_client()
        self._swap_lock = asyncio.Lock()

    async def execute_swap(
        self,
        quote_id: str,
        input_nonce: int,
        input_signature: str,
    ) -> SwapRecord:
        quote = self._validate_quote(quote_id)
        validate_signature(input_signature, "input_signature")
        user_address = self._recover_signer(quote, input_nonce, input_signature)
        if quote["user_address"] != user_address.lower():
            raise ValueError("Quote was not created for this user")

        if quote.get("venue") == SwapVenue.LIFI.value:
            return await get_lifi_pipeline().launch(
                quote, user_address, input_nonce, input_signature
            )

        return await self.execute_swap_internal(
            quote, user_address, input_nonce, input_signature
        )

    async def execute_swap_internal(
        self,
        quote: dict,
        user_address: str,
        input_nonce: int,
        input_signature: str,
    ) -> SwapRecord:
        lp_balance = await self.accounting.get_lp_balance(quote["to_token_id"])
        if int(lp_balance.balance) < int(quote["to_amount_estimate"]):
            raise ValueError("Insufficient liquidity for this swap")

        swap_id = str(uuid.uuid4())
        now = int(time.time())
        db = get_db()
        db_write(
            db,
            """INSERT INTO swaps
               (id, quote_id, user_address, from_token_id, to_token_id,
                from_amount, to_amount_estimate, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                swap_id, quote["id"], user_address.lower(),
                quote["from_token_id"], quote["to_token_id"],
                quote["from_amount"], quote["to_amount_estimate"],
                SwapStatus.PENDING.value, now, now,
            ),
        )

        try:
            async with self._swap_lock:
                lp_nonce = await self.accounting.get_transfer_nonce(
                    self.settings.liquidity_provider_address
                )

                output_signature = sign_transfer(
                    private_key=self.settings.liquidity_provider_secret_key,
                    chain_id=self.settings.accounting_chain_id,
                    verifying_contract=self.settings.accounting_contract_address,
                    to_address=user_address,
                    token_id=quote["to_token_id"],
                    amount=int(quote["to_amount_estimate"]),
                    nonce=lp_nonce,
                )

                input_sig_bytes = bytes.fromhex(input_signature[2:] if input_signature.startswith("0x") else input_signature)
                output_sig_bytes = bytes.fromhex(output_signature[2:] if output_signature.startswith("0x") else output_signature)

                self._update_swap(
                    swap_id,
                    output_nonce=lp_nonce,
                    output_signature=output_signature,
                )
                logger.info(
                    "swap %s signed output: lp=%s to=%s token=%s amount=%s nonce=%s",
                    swap_id,
                    self.settings.liquidity_provider_address,
                    user_address,
                    quote["to_token_id"],
                    quote["to_amount_estimate"],
                    lp_nonce,
                )

                swap_args = [
                    Web3.to_checksum_address(user_address),
                    bytes.fromhex(quote["from_token_id"][2:]),
                    int(quote["from_amount"]),
                    input_nonce,
                    input_sig_bytes,
                    bytes.fromhex(quote["to_token_id"][2:]),
                    int(quote["to_amount_estimate"]),
                    lp_nonce,
                    output_sig_bytes,
                ]

                await self._reject_if_swap_would_revert(swap_id, swap_args)

                tx_hash = await asyncio.to_thread(
                    self.sapphire.execute_contract_call,
                    contract_address=self.settings.swap_manager_contract_address,
                    abi=SWAP_MANAGER_ABI,
                    function_name="swap",
                    args=swap_args,
                    gas_limit=1000000,
                )

            self._update_swap(swap_id, status=SwapStatus.COMPLETED.value, swap_tx_hash=tx_hash)

        except ValueError as exc:
            # A ValueError means the request was rejected rather than settled,
            # so it propagates as a 400. The row was already inserted though,
            # and leaving it PENDING would strand it in the unsettled feed
            # forever. _reject_if_swap_would_revert records the precise chain
            # reason itself, so only close out rows it did not already touch.
            if self._get_swap(swap_id).status == SwapStatus.PENDING.value:
                self._update_swap(
                    swap_id,
                    status=SwapStatus.FAILED.value,
                    error=sanitize_error(str(exc)),
                )
            raise
        except Exception as exc:
            logger.exception(f"Swap {swap_id} failed")
            error_msg = sanitize_error(str(exc))
            self._update_swap(swap_id, status=SwapStatus.FAILED.value, error=error_msg)

        return self._get_swap(swap_id)

    async def _reject_if_swap_would_revert(self, swap_id: str, swap_args: list) -> None:
        """Dry-run the swap and reject it as a bad request if it cannot succeed.

        The user's balance in the Accounting contract is access-gated, so a
        query signed with the LP key cannot read it and check it the way LP
        liquidity is above. Simulating the real call
        asks the contract the same question for free, and turns a guaranteed
        on-chain revert — which costs the LP gas and reports back an opaque
        "failed" — into a 400 before anything is broadcast.
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
            reason = sanitize_error(str(exc))
            logger.warning("Swap %s rejected by simulation: %s", swap_id, reason)
            self._update_swap(
                swap_id, status=SwapStatus.FAILED.value, error=reason
            )
            raise ValueError(
                "Swap cannot be executed: it would revert on-chain. This usually "
                "means an insufficient balance or an already-used transfer nonce."
            ) from exc

    def _validate_quote(self, quote_id: str) -> dict:
        db = get_db()
        row = db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        if row is None:
            raise ValueError("Quote not found")

        quote = dict(row)

        if int(time.time()) >= quote["expires_at"]:
            raise ValueError("Quote has expired")

        return quote

    def _recover_signer(self, quote: dict, input_nonce: int, input_signature: str) -> str:
        try:
            return recover_transfer_signer(
                chain_id=self.settings.accounting_chain_id,
                verifying_contract=self.settings.accounting_contract_address,
                to_address=quote["liquidity_provider"],
                token_id=quote["from_token_id"],
                amount=int(quote["from_amount"]),
                nonce=input_nonce,
                signature=input_signature,
            )
        except Exception as exc:
            raise ValueError("input_signature does not match the quoted transfer") from exc

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


_executor_instance: Optional[SwapExecutor] = None


def get_swap_executor() -> SwapExecutor:
    global _executor_instance
    if _executor_instance is None:
        _executor_instance = SwapExecutor()
    return _executor_instance
