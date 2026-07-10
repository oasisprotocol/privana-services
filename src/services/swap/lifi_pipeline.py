import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from privana.types import TransferFundsRequest

from src.clients.accounting import get_accounting_client
from src.clients.base_evm import base_tx_lock, get_base_evm_client
from src.clients.lifi import get_lifi_client
from src.clients.privana import get_authenticated_privana_client
from src.core.config import load_settings
from src.core.db import db_write, get_db
from src.core.eip712 import sign_transfer
from src.core.fees import calculate_fee
from src.core.validation import sanitize_error
from src.models.swap import LifiSwapStep, SwapRecord, SwapStatus, SwapVenue
from src.services.swap.bridge import AccountingBridge

logger = logging.getLogger(__name__)

ACCEPTED_SUBMISSION_STATUSES = {"submitted", "pending", "accepted"}
LIFI_STATUS_DONE = "DONE"
LIFI_STATUS_FAILED = "FAILED"
STATUS_POLL_INTERVAL_SEC = 10.0
MAX_STATUS_POLLS = 720
MAX_INPUT_CONFIRM_POLLS = 60
CREDIT_MAX_RETRIES = 20

lp_transfer_lock = asyncio.Lock()


class LifiSwapPipeline:
    def __init__(
        self,
        accounting: Optional[Any] = None,
        lifi: Optional[Any] = None,
        bridge: Optional[Any] = None,
        evm: Optional[Any] = None,
        privana_factory: Optional[Callable[[], Awaitable[Any]]] = None,
        poll_interval_sec: float = STATUS_POLL_INTERVAL_SEC,
    ) -> None:
        self.settings = load_settings()
        self.accounting = accounting or get_accounting_client()
        self.lifi = lifi or get_lifi_client()
        self.bridge = bridge or AccountingBridge()
        self.evm = evm or get_base_evm_client()
        self._privana_factory = privana_factory or get_authenticated_privana_client
        self._poll_interval_sec = poll_interval_sec
        self._tasks: set[asyncio.Task] = set()

    async def launch(
        self, quote: dict, user_address: str, input_nonce: int, input_signature: str
    ) -> SwapRecord:
        swap_id = self._insert_swap(quote, user_address)
        try:
            await self._submit_input(quote, input_nonce, input_signature)
        except ValueError as exc:
            self._update_swap(
                swap_id, status=SwapStatus.FAILED.value, error=sanitize_error(str(exc))
            )
            return self._get_swap(swap_id)

        self._update_swap(
            swap_id,
            status=SwapStatus.EXECUTING.value,
            step=LifiSwapStep.INPUT_TRANSFER.value,
        )
        self.spawn_background(swap_id, quote, input_nonce)
        return self._get_swap(swap_id)

    def spawn_background(self, swap_id: str, quote: dict, input_nonce: int) -> None:
        task = asyncio.create_task(self._run(swap_id, quote, input_nonce))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, swap_id: str, quote: dict, input_nonce: int) -> None:
        try:
            await self._confirm_input(quote, input_nonce)
            await self._withdraw(swap_id, quote)
            received, to_info = await self._lifi_execute(swap_id, quote)
            await self._deposit(swap_id, quote, to_info, received)
            credited = await self._credit(quote, received)
            self._update_swap(
                swap_id,
                status=SwapStatus.COMPLETED.value,
                step=LifiSwapStep.CREDIT.value,
                to_amount_actual=str(credited),
            )
        except Exception as exc:
            logger.exception("lifi swap %s failed", swap_id)
            self._update_swap(
                swap_id, status=SwapStatus.FAILED.value, error=sanitize_error(str(exc))
            )

    async def _submit_input(
        self, quote: dict, input_nonce: int, input_signature: str
    ) -> None:
        client = await self._privana_factory()
        submission = await client.transfer_funds(
            TransferFundsRequest(
                to_address=self.settings.liquidity_provider_address,
                token_id=quote["from_token_id"],
                amount=int(quote["from_amount"]),
                nonce=input_nonce,
                signature=input_signature,
            )
        )
        if submission.status not in ACCEPTED_SUBMISSION_STATUSES:
            raise ValueError(
                f"input transfer rejected: status={submission.status} detail={submission.detail}"
            )

    async def _confirm_input(self, quote: dict, input_nonce: int) -> None:
        for _ in range(MAX_INPUT_CONFIRM_POLLS):
            nonce = await self.accounting.get_transfer_nonce(quote["user_address"])
            if nonce > input_nonce:
                return
            await asyncio.sleep(self._poll_interval_sec)
        raise RuntimeError("input transfer not confirmed on ledger")

    async def _withdraw(self, swap_id: str, quote: dict) -> None:
        self._update_swap(swap_id, step=LifiSwapStep.WITHDRAW.value)
        index = await self.bridge.withdraw_to_chain(
            quote["from_token_id"], int(quote["from_amount"])
        )
        self._update_swap(swap_id, withdrawal_index=index)

    async def _lifi_execute(self, swap_id: str, quote: dict) -> tuple[int, Any]:
        self._update_swap(swap_id, step=LifiSwapStep.LIFI_EXECUTE.value)
        from_info = await self.accounting.get_token_info(quote["from_token_id"])
        to_info = await self.accounting.get_token_info(quote["to_token_id"])

        exec_quote = await self.lifi.get_execution_quote(
            from_chain_id=from_info.chain_id,
            to_chain_id=to_info.chain_id,
            from_token_address=from_info.token_address,
            to_token_address=to_info.token_address,
            from_amount=quote["from_amount"],
            from_address=self.evm.address,
        )
        net_min, _ = calculate_fee(
            int(exec_quote["estimate"]["toAmountMin"]), self.settings.fee_bps
        )
        if net_min < int(quote["to_amount_min"]):
            raise RuntimeError(
                f"execution quote below floor: net_min={net_min} floor={quote['to_amount_min']}"
            )

        pre_out = await asyncio.to_thread(
            self.evm.erc20_balance, to_info.token_address, self.evm.address
        )
        async with base_tx_lock:
            await asyncio.to_thread(
                self.evm.ensure_allowance,
                from_info.token_address,
                exec_quote["estimate"]["approvalAddress"],
                int(quote["from_amount"]),
            )
            tx_hash = await asyncio.to_thread(
                self.evm.send_transaction_request, exec_quote["transactionRequest"]
            )
        self._update_swap(swap_id, lifi_tx_hash=tx_hash)

        await self._await_lifi_done(tx_hash, from_info.chain_id, to_info.chain_id)

        post_out = await asyncio.to_thread(
            self.evm.erc20_balance, to_info.token_address, self.evm.address
        )
        received = post_out - pre_out
        if received <= 0:
            raise RuntimeError("no output tokens received from lifi execution")
        return received, to_info

    async def _await_lifi_done(
        self, tx_hash: str, from_chain_id: int, to_chain_id: int
    ) -> None:
        for _ in range(MAX_STATUS_POLLS):
            status = await self.lifi.get_status(tx_hash, from_chain_id, to_chain_id)
            state = status.get("status")
            if state == LIFI_STATUS_DONE:
                return
            if state == LIFI_STATUS_FAILED:
                raise RuntimeError(f"lifi execution failed for {tx_hash}")
            await asyncio.sleep(self._poll_interval_sec)
        raise RuntimeError(f"lifi execution status polling exhausted for {tx_hash}")

    async def _deposit(
        self, swap_id: str, quote: dict, to_info: Any, received: int
    ) -> None:
        self._update_swap(swap_id, step=LifiSwapStep.DEPOSIT.value)
        deposit_address = await self.bridge.get_deposit_address()
        pre_internal = await self.bridge.lp_internal_balance(quote["to_token_id"])
        async with base_tx_lock:
            deposit_tx = await asyncio.to_thread(
                self.evm.transfer_erc20, to_info.token_address, deposit_address, received
            )
        self._update_swap(swap_id, deposit_tx_hash=deposit_tx)
        await self.bridge.await_deposit_credit(
            to_info.chain_id, deposit_tx, received, quote["to_token_id"], pre_internal
        )

    async def _credit(self, quote: dict, received: int) -> int:
        credited, _ = calculate_fee(received, self.settings.fee_bps)
        client = await self._privana_factory()
        last_detail = None
        for _ in range(CREDIT_MAX_RETRIES):
            async with lp_transfer_lock:
                lp_nonce = await self.accounting.get_transfer_nonce(
                    self.settings.liquidity_provider_address
                )
                signature = sign_transfer(
                    private_key=self.settings.liquidity_provider_secret_key,
                    chain_id=self.settings.accounting_chain_id,
                    verifying_contract=self.settings.accounting_contract_address,
                    to_address=quote["user_address"],
                    token_id=quote["to_token_id"],
                    amount=credited,
                    nonce=lp_nonce,
                )
                submission = await client.transfer_funds(
                    TransferFundsRequest(
                        to_address=quote["user_address"],
                        token_id=quote["to_token_id"],
                        amount=credited,
                        nonce=lp_nonce,
                        signature=signature,
                    )
                )
            if submission.status in ACCEPTED_SUBMISSION_STATUSES:
                return credited
            last_detail = submission.detail
            await asyncio.sleep(self._poll_interval_sec)
        raise RuntimeError(f"credit retries exhausted: {last_detail}")

    def _insert_swap(self, quote: dict, user_address: str) -> str:
        swap_id = str(uuid.uuid4())
        now = int(time.time())
        db = get_db()
        db_write(
            db,
            """INSERT INTO swaps
               (id, quote_id, user_address, from_token_id, to_token_id,
                from_amount, to_amount_estimate, status, venue, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                swap_id, quote["id"], user_address.lower(),
                quote["from_token_id"], quote["to_token_id"],
                quote["from_amount"], quote["to_amount_estimate"],
                SwapStatus.PENDING.value, SwapVenue.LIFI.value, now, now,
            ),
        )
        return swap_id

    def _update_swap(self, swap_id: str, **fields) -> None:
        db = get_db()
        fields["updated_at"] = int(time.time())
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [swap_id]
        db_write(db, f"UPDATE swaps SET {set_clause} WHERE id = ?", tuple(values))

    def _get_swap(self, swap_id: str) -> SwapRecord:
        db = get_db()
        row = db.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
        if row is None:
            raise ValueError(f"Swap {swap_id} not found")
        return SwapRecord(**dict(row))


_pipeline_instance: Optional[LifiSwapPipeline] = None


def get_lifi_pipeline() -> LifiSwapPipeline:
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = LifiSwapPipeline()
    return _pipeline_instance
