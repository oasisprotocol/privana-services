import asyncio
import logging
import time
import uuid
from typing import Optional

from src.clients.accounting import get_accounting_client
from src.clients.sapphire import get_sapphire_client
from src.config import load_settings
from src.db import db_write, get_db
from src.models.swap import SwapRecord, SwapStatus
from src.services.eip712 import sign_transfer

logger = logging.getLogger(__name__)


class SwapExecutor:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.accounting = get_accounting_client()
        self.sapphire = get_sapphire_client()
        self._swap_lock = asyncio.Lock()

    async def execute_swap(
        self,
        quote_id: str,
        user_address: str,
        input_nonce: int,
        input_signature: str,
    ) -> SwapRecord:
        quote = self._validate_quote(quote_id, user_address)
        self._validate_signature_format(input_signature)

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
                swap_id, quote_id, user_address.lower(),
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
                    private_key=self.settings.liquidity_provider_private_key,
                    chain_id=self.settings.accounting_chain_id,
                    verifying_contract=self.settings.accounting_contract_address,
                    user_address=self.settings.liquidity_provider_address,
                    to_address=user_address,
                    token_id=quote["to_token_id"],
                    amount=int(quote["to_amount_estimate"]),
                    nonce=lp_nonce,
                )

                input_sig_bytes = bytes.fromhex(input_signature[2:] if input_signature.startswith("0x") else input_signature)
                output_sig_bytes = bytes.fromhex(output_signature[2:] if output_signature.startswith("0x") else output_signature)

                tx_hash = await asyncio.to_thread(
                    self.sapphire.execute_swap,
                    user=user_address,
                    input_token_id=bytes.fromhex(quote["from_token_id"][2:]),
                    input_amount=int(quote["from_amount"]),
                    input_nonce=input_nonce,
                    input_signature=input_sig_bytes,
                    output_token_id=bytes.fromhex(quote["to_token_id"][2:]),
                    output_amount=int(quote["to_amount_estimate"]),
                    output_nonce=lp_nonce,
                    output_signature=output_sig_bytes,
                )

            self._update_swap(swap_id, status=SwapStatus.COMPLETED.value, swap_tx_hash=tx_hash)

        except Exception as exc:
            logger.exception(f"Swap {swap_id} failed")
            error_msg = self._sanitize_error(str(exc))
            self._update_swap(swap_id, status=SwapStatus.FAILED.value, error=error_msg)

        return self._get_swap(swap_id)

    def _validate_quote(self, quote_id: str, user_address: str) -> dict:
        db = get_db()
        row = db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        if row is None:
            raise ValueError("Quote not found")

        quote = dict(row)

        if int(time.time()) > quote["expires_at"]:
            raise ValueError("Quote has expired")

        if quote["user_address"] != user_address.lower():
            raise ValueError("Quote was not created for this user")

        return quote

    def _validate_signature_format(self, signature: str) -> None:
        if not signature.startswith("0x"):
            raise ValueError("Signature must start with 0x")
        try:
            sig_bytes = bytes.fromhex(signature[2:])
        except ValueError:
            raise ValueError("Signature must be valid hex")
        if len(sig_bytes) != 65:
            raise ValueError("Signature must be 65 bytes")

    def _sanitize_error(self, error: str) -> str:
        if "reverted" in error.lower():
            return "Swap transaction reverted on-chain"
        if "insufficient funds" in error.lower():
            return "Insufficient gas funds for transaction"
        if "nonce" in error.lower():
            return "Transaction nonce conflict"
        if len(error) > 200:
            return error[:200]
        return error

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
