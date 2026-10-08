import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import httpx
from eth_abi import decode
from hexbytes import HexBytes
from privana.types import TransferFundsRequest
from web3 import Web3

from src.clients.accounting import get_accounting_client
from src.clients.base_evm import NATIVE_TOKEN, TransactionPendingError, get_evm_client, is_native
from src.clients.lifi import LIFI_DEFAULT_SLIPPAGE_BPS, get_lifi_client
from src.clients.privana import get_swap_lp_privana_client
from src.core.config import load_settings
from src.core.db import db_write, get_db
from src.core.eip712 import sign_transfer
from src.core.fees import calculate_fee
from src.core.validation import sanitize_error
from src.models.swap import LifiSwapStep, SwapRecord, SwapStatus, SwapVenue
from src.services.swap.bridge import AccountingBridge
from src.services.swap.failure import log_swap_failure
from src.services.swap.worker import lp_transfer_lock

logger = logging.getLogger(__name__)

ACCEPTED_SUBMISSION_STATUSES = {"submitted", "pending", "accepted"}
LIFI_STATUS_DONE = "DONE"
LIFI_STATUS_FAILED = "FAILED"
LIFI_STATUS_INVALID = "INVALID"
LIFI_SUBSTATUS_COMPLETED = "COMPLETED"
STATUS_POLL_INTERVAL_SEC = 6.0
MAX_STATUS_POLLS = 720
MAX_INPUT_CONFIRM_POLLS = 60
CREDIT_MAX_RETRIES = 20
DEPOSIT_MAX_RETRIES = 10
REFUND_BALANCE_POLLS = 30
# About 30 min for a withdrawal to reach the swap pool wallet.
FUNDING_POLLS = 300
# About 15 min for a sent tx to be mined.
TX_RECEIPT_POLLS = 150
# About 1 min for an approval with an unknown outcome to land.
APPROVAL_POLLS = 10

# Emitted by the LiFi diamond for a same-chain swap (lifinance/contracts ILiFi.sol).
SWAP_COMPLETED_TOPIC = Web3.keccak(
    text="LiFiGenericSwapCompleted(bytes32,string,string,address,address,address,uint256,uint256)"
)
# Its data: integrator, referrer, receiver, fromAssetId, toAssetId, fromAmount, toAmount.
SWAP_COMPLETED_DATA = ["string", "string", "address", "address", "address", "uint256", "uint256"]


class TxReverted(RuntimeError):
    """The tx was mined and reverted: it moved nothing."""


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
        # One client for every chain in tests; per chain otherwise.
        self.evm = evm
        self._privana_factory = privana_factory or get_swap_lp_privana_client
        self._poll_interval_sec = poll_interval_sec
        self._credit_max_retries = CREDIT_MAX_RETRIES
        self._deposit_max_retries = DEPOSIT_MAX_RETRIES
        self._tasks: dict[str, asyncio.Task] = {}

    async def launch(
        self, quote: dict, user_address: str, input_nonce: int, input_signature: str,
        swap_id: str,
    ) -> SwapRecord:
        try:
            await self._submit_input(quote, input_nonce, input_signature)
        except Exception as exc:
            self._update_swap(
                swap_id, status=SwapStatus.FAILED.value, error=sanitize_error(str(exc))
            )
            log_swap_failure(swap_id, "input transfer rejected", exc)
            return self._get_swap(swap_id)

        self._update_swap(
            swap_id,
            status=SwapStatus.EXECUTING.value,
            step=LifiSwapStep.INPUT_TRANSFER.value,
        )
        self.spawn_background(swap_id, quote, input_nonce)
        return self._get_swap(swap_id)

    def spawn_background(self, swap_id: str, quote: dict, input_nonce: int) -> None:
        # One runner per swap: a second would pay it twice.
        if swap_id in self._tasks:
            logger.warning("lifi swap %s already has a runner", swap_id)
            return
        task = asyncio.create_task(self._run(swap_id, quote, input_nonce))
        self._tasks[swap_id] = task

        def release(done: asyncio.Task) -> None:
            if self._tasks.get(swap_id) is done:
                del self._tasks[swap_id]

        task.add_done_callback(release)

    def _evm_for(self, chain_id: int):
        return self.evm if self.evm is not None else get_evm_client(chain_id)

    async def _run(self, swap_id: str, quote: dict, input_nonce: int) -> None:
        try:
            # Launch starts at input_transfer; recovery resumes at lifi_execute or deposit.
            row = self._row(swap_id)
            if row["step"] == LifiSwapStep.DEPOSIT.value:
                received = int(row["to_amount_received"])
                to_info = await self.accounting.get_token_info(quote["to_token_id"])
            else:
                if row["step"] == LifiSwapStep.INPUT_TRANSFER.value:
                    await self._confirm_input(quote, input_nonce)
                    await self._withdraw(swap_id, quote)
                received, to_info = await self._lifi_execute(swap_id, quote)
                self._update_swap(
                    swap_id, step=LifiSwapStep.DEPOSIT.value, to_amount_received=str(received)
                )
            await self._deposit(swap_id, to_info, received)
            self._update_swap(swap_id, step=LifiSwapStep.CREDIT.value)
            credited = await self._credit(quote, received)
            self._update_swap(
                swap_id,
                status=SwapStatus.COMPLETED.value,
                to_amount_actual=str(credited),
            )
        except Exception as exc:
            log_swap_failure(swap_id, "lifi swap failed", exc)
            row = self._row(swap_id)
            reason = sanitize_error(str(exc))
            if row["step"] in (LifiSwapStep.DEPOSIT.value, LifiSwapStep.CREDIT.value) or (
                row["step"] == LifiSwapStep.LIFI_EXECUTE.value
                and row["lifi_tx_hash"] is not None
                and not isinstance(exc, TxReverted)
            ):
                # Refund only on proof the input is unspent: past a sent LiFi tx,
                # that is its reverted receipt.
                self._park(swap_id, reason)
                return
            await self._refund(swap_id, quote, row["step"], reason)

    def _row(self, swap_id: str) -> dict:
        return dict(get_db().execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone())

    def _park(self, swap_id: str, reason: str) -> None:
        """Mark the swap for manual recovery. Moves nothing."""
        log_swap_failure(swap_id, f"parked for manual recovery: {reason}")
        self._update_swap(
            swap_id,
            status=SwapStatus.FAILED.value,
            error=f"{reason}; manual recovery required",
        )

    async def _refund(
        self, swap_id: str, quote: dict, step: Optional[str], reason: str
    ) -> None:
        refundable = {LifiSwapStep.WITHDRAW.value, LifiSwapStep.LIFI_EXECUTE.value}
        if step not in refundable:
            logger.info("lifi swap %s failed at step %s, nothing to refund: %s", swap_id, step, reason)
            self._update_swap(swap_id, status=SwapStatus.FAILED.value, error=reason)
            return

        logger.info("lifi swap %s refunding from step %s: %s", swap_id, step, reason)
        self._update_swap(swap_id, status=SwapStatus.REFUNDING.value, error=reason)
        try:
            if step == LifiSwapStep.LIFI_EXECUTE.value:
                logger.info("lifi swap %s re-depositing input tokens", swap_id)
                from_info = await self.accounting.get_token_info(quote["from_token_id"])
                amount = int(quote["from_amount"])
                # A saved redeposit is settled by its receipt, not by the balance it spends.
                if self._row(swap_id)["deposit_tx_hash"] is None:
                    evm = self._evm_for(from_info.chain_id)
                    for _ in range(REFUND_BALANCE_POLLS):
                        if await self._holds(evm, from_info.token_address, amount):
                            break
                        await asyncio.sleep(self._poll_interval_sec)
                    else:
                        raise RuntimeError("input tokens not returned on-chain")
                await self._deposit(swap_id, from_info, amount)
            await self._lp_transfer(quote["user_address"], quote["from_token_id"], int(quote["from_amount"]))
            self._update_swap(swap_id, status=SwapStatus.REFUNDED.value)
            logger.info("lifi swap %s refunded", swap_id)
        except Exception as exc:
            self._update_swap(
                swap_id,
                status=SwapStatus.FAILED.value,
                error=f"{reason}; refund failed, manual recovery required: {sanitize_error(str(exc))}",
            )
            log_swap_failure(swap_id, "refund failed", exc)

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
        evm = self._evm_for(from_info.chain_id)
        if self._row(swap_id)["lifi_tx_hash"] is None:
            await self._send_lifi_tx(swap_id, quote, from_info, to_info, evm)
        receipt = await self._await_tx(swap_id, "lifi", evm)
        # The swap's reported output, not the wallet balance, which other swaps and gas move.
        if from_info.chain_id == to_info.chain_id:
            received = _swap_output(receipt, evm.address, _lifi_token(to_info.token_address))
        else:
            received = await self._bridge_output(
                self._row(swap_id)["lifi_tx_hash"], from_info, to_info, evm.address
            )
        return received, to_info

    async def _send_lifi_tx(
        self, swap_id: str, quote: dict, from_info: Any, to_info: Any, evm: Any
    ) -> None:
        """Send the swap once the wallet holds its input, and gas for a native
        input. Sent earlier, it reverts. The wallet is shared, so its balance
        does not prove this swap's withdrawal arrived."""
        amount = int(quote["from_amount"])
        for _ in range(FUNDING_POLLS):
            if await self._holds(evm, from_info.token_address, amount):
                exec_quote = await self._execution_quote(
                    swap_id, quote, from_info, to_info, evm.address
                )
                async with evm.tx_lock:
                    gas = int(exec_quote["transactionRequest"]["gasLimit"], 16)
                    # Swaps share the wallet, so its balance is checked again under the lock.
                    if await self._holds(evm, from_info.token_address, amount, gas):
                        await self._approve(
                            evm, from_info.token_address,
                            exec_quote["estimate"]["approvalAddress"], amount,
                        )
                        await self._send_tx(
                            swap_id, "lifi", evm.send_transaction_request,
                            exec_quote["transactionRequest"],
                        )
                        return
            await asyncio.sleep(self._poll_interval_sec)
        raise RuntimeError(f"swap funding unconfirmed after {FUNDING_POLLS} polls")

    async def _execution_quote(
        self, swap_id: str, quote: dict, from_info: Any, to_info: Any, from_address: str
    ) -> dict:
        request = dict(
            from_chain_id=from_info.chain_id,
            to_chain_id=to_info.chain_id,
            from_token_address=_lifi_token(from_info.token_address),
            to_token_address=_lifi_token(to_info.token_address),
            from_amount=quote["from_amount"],
            from_address=from_address,
        )
        fee_bps = self.settings.fee_bps
        floor = int(quote["to_amount_min"])
        # Quotes stored before the slippage_bps column existed were priced at LiFi's default.
        slippage_bps = quote.get("slippage_bps")
        if slippage_bps is None:
            slippage_bps = LIFI_DEFAULT_SLIPPAGE_BPS
        # The floor came from a route priced at this slippage. A different one
        # moves toAmountMin, not the price.
        exec_quote = await self.lifi.get_execution_quote(**request, slippage_bps=slippage_bps)
        if _net_min(exec_quote, fee_bps) < floor:
            # The price fell since the quote. Spend less of the tolerance so
            # the minimum LiFi enforces on chain still pays out the floor.
            narrowed_bps = _slippage_bps_to_floor(
                int(exec_quote["estimate"]["toAmount"]), floor, fee_bps
            )
            if narrowed_bps is not None:
                logger.info("lifi swap %s narrowing slippage to %s bps", swap_id, narrowed_bps)
                exec_quote = await self.lifi.get_execution_quote(
                    **request, slippage_bps=narrowed_bps
                )
        net_min = _net_min(exec_quote, fee_bps)
        if net_min < floor:
            raise RuntimeError(
                f"execution quote below floor: net_min={net_min} floor={floor}"
            )
        return exec_quote

    async def _holds(self, evm: Any, token: Optional[str], amount: int, gas: int = 0) -> bool:
        """Whether the wallet holds `amount`, plus `gas` at the fee cap when
        `token` is the native coin that pays for it."""
        try:
            if gas and is_native(token):
                amount += await asyncio.to_thread(evm.max_gas_cost, gas)
            return await asyncio.to_thread(evm.balance_of, token, evm.address) >= amount
        except Exception as exc:
            logger.warning("balance or fees for %s unavailable: %s", token, exc)
            return False

    async def _approve(self, evm: Any, token: Optional[str], spender: str, amount: int) -> None:
        """An approval whose receipt timed out can still be mined, so its allowance
        decides. Priced out, it holds up every later tx of the wallet, so it is
        rebroadcast like the swap's txs. Its versions stay in memory: its allowance
        settles it, not a receipt. The wait stays short: it holds the chain's tx
        lock, and a refund is safe until the LiFi tx is sent."""
        signed: list[str] = []

        def keep(tx_hash: str, nonce: int, raw: str) -> None:
            signed.append(raw)

        try:
            await asyncio.to_thread(evm.ensure_allowance, token, spender, amount, on_signed=keep)
            return
        except TransactionPendingError as exc:
            logger.warning("approval %s outcome unknown: %s", exc.tx_hash, exc)
        for _ in range(APPROVAL_POLLS):
            await asyncio.sleep(self._poll_interval_sec)
            try:
                if await asyncio.to_thread(evm.allowance, token, spender) >= amount:
                    return
                await asyncio.to_thread(evm.rebroadcast, signed[-1], keep)
            except Exception as exc:
                logger.warning("approval of %s for %s still pending: %s", token, spender, exc)
        raise RuntimeError(
            f"allowance of {token} for {spender} unconfirmed after {APPROVAL_POLLS} polls"
        )

    async def _send_tx(
        self, swap_id: str, kind: str, send: Callable[..., str], *args: Any
    ) -> None:
        """Send a tx, saved to the `{kind}_tx_*` columns before broadcast. Once
        saved, a send error proves nothing: only the receipt settles it."""
        hash_column = f"{kind}_tx_hash"

        def record(tx_hash: str, nonce: int, raw: str) -> None:
            self._update_swap(swap_id, **{hash_column: tx_hash, f"{kind}_tx_raw": raw})
            logger.info("lifi swap %s submitting %s tx %s at nonce %d", swap_id, kind, tx_hash, nonce)

        try:
            await asyncio.to_thread(send, *args, on_signed=record)
        except Exception as exc:
            tx_hash = self._row(swap_id)[hash_column]
            if tx_hash is None:
                raise
            logger.warning(
                "lifi swap %s %s tx %s outcome unknown: %s", swap_id, kind, tx_hash, exc
            )

    async def _await_tx(self, swap_id: str, kind: str, evm: Any) -> Any:
        """The receipt of the `{kind}_tx_*` tx, rebroadcast until a version of it is
        mined. That version becomes `{kind}_tx_hash` and the others stay in
        `{kind}_tx_replaced`, since a receipt is not final. Nodes lag, so a missing
        receipt proves nothing, even once the nonce is used."""
        hash_column, raw_column = f"{kind}_tx_hash", f"{kind}_tx_raw"
        replaced_column = f"{kind}_tx_replaced"

        def replace(tx_hash: str, nonce: int, raw: str) -> None:
            row = self._row(swap_id)
            self._update_swap(swap_id, **{
                hash_column: tx_hash, raw_column: raw,
                replaced_column: " ".join(filter(None, (row[replaced_column], row[hash_column]))),
            })
            logger.info("lifi swap %s submitting %s tx %s at nonce %d", swap_id, kind, tx_hash, nonce)

        for _ in range(TX_RECEIPT_POLLS):
            row = self._row(swap_id)
            versions = [row[hash_column], *(row[replaced_column] or "").split()]
            try:
                mined = await self._mined(evm, versions)
                if mined is None:
                    await asyncio.to_thread(evm.rebroadcast, row[raw_column], replace)
            except Exception as exc:
                # A failed lookup says nothing about the tx.
                logger.warning("transaction %s lookup failed: %s", row[hash_column], exc)
            else:
                if mined is not None:
                    tx_hash, receipt = mined
                    if tx_hash != row[hash_column]:
                        self._update_swap(swap_id, **{
                            hash_column: tx_hash,
                            replaced_column: " ".join(h for h in versions if h != tx_hash),
                        })
                    if receipt["status"] != 1:
                        raise TxReverted(f"transaction reverted: {tx_hash}")
                    return receipt
            await asyncio.sleep(self._poll_interval_sec)
        raise RuntimeError(
            f"receipt of {self._row(swap_id)[hash_column]} unavailable after {TX_RECEIPT_POLLS} polls"
        )

    async def _mined(self, evm: Any, hashes: list[str]) -> Optional[tuple[str, Any]]:
        """The first of `hashes` with a receipt, and the receipt."""
        for tx_hash in hashes:
            receipt = await asyncio.to_thread(evm.get_receipt, tx_hash)
            if receipt is not None:
                return tx_hash, receipt
        return None

    async def _bridge_output(
        self, tx_hash: str, from_info: Any, to_info: Any, recipient: str
    ) -> int:
        """What LiFi delivered to `recipient`. Anything but a full delivery raises."""
        token = _lifi_token(to_info.token_address)
        for _ in range(MAX_STATUS_POLLS):
            try:
                status = await self.lifi.get_status(tx_hash, from_info.chain_id, to_info.chain_id)
            except Exception as exc:
                # LiFi answers 404 until it indexes the tx.
                if not (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404):
                    logger.warning("lifi status of %s unavailable: %s", tx_hash, exc)
                status = {}
            state, substatus = status.get("status"), status.get("substatus")
            # Only DONE with COMPLETED is a full delivery. DONE with PARTIAL or
            # REFUNDED, FAILED and INVALID are final. Anything else keeps polling.
            if state == LIFI_STATUS_DONE and substatus == LIFI_SUBSTATUS_COMPLETED:
                receiving = status["receiving"]
                if (
                    int(receiving["chainId"]) != to_info.chain_id
                    or receiving["token"]["address"].lower() != token.lower()
                    or status["toAddress"].lower() != recipient.lower()
                ):
                    raise RuntimeError(f"lifi delivered {tx_hash} elsewhere: {receiving}")
                return int(receiving["amount"])
            if state in (LIFI_STATUS_DONE, LIFI_STATUS_FAILED, LIFI_STATUS_INVALID):
                raise RuntimeError(f"lifi route of {tx_hash} ended {state} {substatus}")
            await asyncio.sleep(self._poll_interval_sec)
        raise RuntimeError(f"lifi execution status polling exhausted for {tx_hash}")

    async def _deposit(self, swap_id: str, info: Any, amount: int) -> None:
        """Transfer `amount` from the swap pool wallet to its Accounting deposit
        address, then wait for Accounting to credit the swap pool account's
        private balance. A new transfer is signed only after the last one reverts."""
        evm = self._evm_for(info.chain_id)
        last_error: Optional[Exception] = None
        for attempt in range(self._deposit_max_retries):
            logger.info(
                "lifi swap %s deposit attempt %d/%d", swap_id, attempt + 1, self._deposit_max_retries
            )
            try:
                if self._row(swap_id)["deposit_tx_hash"] is None:
                    await self._transfer_to_deposit_address(
                        swap_id, evm, info.token_address, amount
                    )
                try:
                    await self._await_tx(swap_id, "deposit", evm)
                except TxReverted:
                    self._update_swap(
                        swap_id, deposit_tx_hash=None, deposit_tx_raw=None,
                        deposit_tx_replaced=None,
                    )
                    raise
                await self.bridge.await_deposit_credit(
                    info.chain_id, self._row(swap_id)["deposit_tx_hash"], amount
                )
                logger.info("lifi swap %s deposit attempt %d succeeded", swap_id, attempt + 1)
                return
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "lifi swap %s deposit attempt %d failed: %s", swap_id, attempt + 1, exc
                )
                await asyncio.sleep(self._poll_interval_sec)
        raise RuntimeError(f"deposit retries exhausted: {last_error}")

    async def _transfer_to_deposit_address(
        self, swap_id: str, evm: Any, token_address: Optional[str], amount: int
    ) -> None:
        deposit_address = await self.bridge.get_deposit_address()
        async with evm.tx_lock:
            await self._send_tx(
                swap_id, "deposit", evm.transfer, token_address, deposit_address, amount
            )

    async def _credit(self, quote: dict, received: int) -> int:
        credited, _ = calculate_fee(received, self.settings.fee_bps)
        await self._lp_transfer(quote["user_address"], quote["to_token_id"], credited)
        return credited

    async def _lp_transfer(self, to_address: str, token_id: str, amount: int) -> None:
        logger.info("lifi swap _lp_transfer %s of token %s to %s", amount, token_id, to_address)
        client = await self._privana_factory()
        # Waiting for internal settlement does not consume payout retries.
        while True:
            async with lp_transfer_lock:
                # A receipt timeout leaves internal transactions executing.
                # Do not reuse their reserved ledger nonces after their worker
                # has released the lock to retry receipt lookup later.
                unresolved = get_db().execute(
                    "SELECT 1 FROM swaps WHERE venue = 'internal' "
                    "AND status = 'executing' AND output_signature IS NOT NULL LIMIT 1"
                ).fetchone()
                if not unresolved:
                    return await self._submit_lp_transfer(client, to_address, token_id, amount)
            await asyncio.sleep(self._poll_interval_sec)

    async def _submit_lp_transfer(self, client, to_address: str, token_id: str, amount: int) -> None:
        logger.info("lifi swap _submit_lp_transfer %s of token %s to %s", amount, token_id, to_address)
        # Caller owns lp_transfer_lock through acceptance AND confirmation.
        last_detail = None
        for _ in range(self._credit_max_retries):
            lp_nonce = await self.accounting.get_transfer_nonce(
                self.settings.liquidity_provider_address
            )
            signature = sign_transfer(
                private_key=self.settings.liquidity_provider_secret_key,
                chain_id=self.settings.accounting_chain_id,
                verifying_contract=self.settings.accounting_contract_address,
                to_address=to_address,
                token_id=token_id,
                amount=amount,
                nonce=lp_nonce,
            )
            submission = await client.transfer_funds(
                TransferFundsRequest(
                    to_address=to_address,
                    token_id=token_id,
                    amount=amount,
                    nonce=lp_nonce,
                    signature=signature,
                )
            )
            if submission.status in ACCEPTED_SUBMISSION_STATUSES:
                for _ in range(MAX_INPUT_CONFIRM_POLLS):
                    current = await self.accounting.get_transfer_nonce(
                        self.settings.liquidity_provider_address
                    )
                    if current > lp_nonce:
                        return
                    await asyncio.sleep(self._poll_interval_sec)
                raise RuntimeError("LP transfer not confirmed on ledger")
            last_detail = submission.detail
            await asyncio.sleep(self._poll_interval_sec)
        raise RuntimeError(f"credit retries exhausted: {last_detail}")

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


def _lifi_token(token_address: Optional[str]) -> str:
    return NATIVE_TOKEN if is_native(token_address) else token_address


def _swap_output(receipt: Any, recipient: str, token: str) -> int:
    """What the swap in `receipt` paid `recipient` in `token`. Only the called
    contract's log counts: anything the swap touches can emit the topic."""
    diamond = receipt["to"].lower()
    for log in receipt["logs"]:
        topics = log["topics"]
        if (
            log["address"].lower() != diamond
            or not topics
            or HexBytes(topics[0]) != SWAP_COMPLETED_TOPIC
        ):
            continue
        _, _, receiver, _, to_asset, _, to_amount = decode(
            SWAP_COMPLETED_DATA, HexBytes(log["data"])
        )
        if receiver.lower() == recipient.lower() and to_asset.lower() == token.lower():
            return to_amount
    raise RuntimeError(f"receipt shows no LiFi swap paying {recipient} in {token}")


def _net_min(exec_quote: dict, fee_bps: int) -> int:
    """The least the user is credited if LiFi pays out its toAmountMin."""
    net, _ = calculate_fee(int(exec_quote["estimate"]["toAmountMin"]), fee_bps)
    return net


def _slippage_bps_to_floor(to_amount: int, floor: int, fee_bps: int) -> Optional[int]:
    """The largest slippage whose toAmountMin still credits `floor` after the
    fee, or None when the expected output itself falls short of it."""
    # Smallest gross output whose net is at least the floor. The fee rounds
    # down, so the estimate can sit a unit or two above it.
    gross_min = -(-floor * 10_000 // (10_000 - fee_bps))
    while gross_min > 0 and calculate_fee(gross_min - 1, fee_bps)[0] >= floor:
        gross_min -= 1
    if to_amount < gross_min:
        return None
    # Rounded down: a smaller slippage only raises the minimum.
    return (to_amount - gross_min) * 10_000 // to_amount


async def recover_inflight_lifi_swaps(pipeline: Optional[LifiSwapPipeline] = None) -> None:
    db = get_db()
    rows = db.execute(
        """SELECT * FROM swaps
           WHERE venue = ? AND status IN (?, ?)""",
        (
            SwapVenue.LIFI.value,
            SwapStatus.EXECUTING.value,
            SwapStatus.REFUNDING.value,
        ),
    ).fetchall()
    if not rows:
        return

    pipeline = pipeline or get_lifi_pipeline()
    for row in rows:
        # A retried pass leaves a swap to its live runner, and reads each row at
        # its turn: runners move swaps on while an earlier row's refund is awaited.
        if row["id"] in pipeline._tasks:
            continue
        swap = pipeline._row(row["id"])
        if swap["status"] not in (SwapStatus.EXECUTING.value, SwapStatus.REFUNDING.value):
            continue
        # Cleanup keeps the quote of a swap in flight.
        quote = dict(
            db.execute("SELECT * FROM quotes WHERE id = ?", (swap["quote_id"],)).fetchone()
        )
        step = swap.get("step")
        if swap["status"] == SwapStatus.EXECUTING.value and step in (
            LifiSwapStep.LIFI_EXECUTE.value, LifiSwapStep.DEPOSIT.value
        ):
            logger.info("lifi swap %s resuming at step %s", swap["id"], step)
            pipeline.spawn_background(swap["id"], quote, int(swap["input_nonce"]))
            continue
        if step == LifiSwapStep.CREDIT.value:
            pipeline._park(swap["id"], f"interrupted at step {step}")
            continue
        logger.info("lifi swap %s recovered into refund path (step=%s)", swap["id"], step)
        await pipeline._refund(
            swap["id"], quote, step, "service restarted mid-execution"
        )


_pipeline_instance: Optional[LifiSwapPipeline] = None


def get_lifi_pipeline() -> LifiSwapPipeline:
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = LifiSwapPipeline()
    return _pipeline_instance
