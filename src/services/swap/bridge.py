import asyncio
import logging
from typing import Awaitable, Callable, Optional, TypeVar

from eth_account import Account
from privana.client.errors import NetworkError
from privana.signatures import SignWithdrawParams, sign_withdraw_message
from privana.signatures.eip712_types import WithdrawMessage
from privana.types import DepositCheckRequest, WithdrawalRequest

from src.clients.privana import get_swap_lp_privana_client
from src.core.config import load_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

POLL_INTERVAL_SEC = 5.0
MAX_POLL_ATTEMPTS = 120
MAX_NETWORK_RETRIES = 10
ACCEPTED_SUBMISSION_STATUSES = {"submitted", "pending", "accepted"}
SAPPHIRE_TESTNET_CHAIN_ID = 23295

# One per process, shared by every bridge: the LP has a single withdrawal nonce.
withdrawal_lock = asyncio.Lock()


def _normalize_tx_hash(tx_hash: str) -> str:
    return tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"


class AccountingBridge:
    def __init__(
        self,
        poll_interval_sec: float = POLL_INTERVAL_SEC,
        client_factory: Optional[Callable[[], Awaitable]] = None,
    ) -> None:
        settings = load_settings()
        self._lp_key = settings.liquidity_provider_secret_key
        self._lp_address = settings.liquidity_provider_address
        self._contract = settings.accounting_contract_address
        self._network = (
            "testnet" if settings.accounting_chain_id == SAPPHIRE_TESTNET_CHAIN_ID else "mainnet"
        )
        self._poll_interval_sec = poll_interval_sec
        self._max_poll_attempts = MAX_POLL_ATTEMPTS
        self._client_factory = client_factory or get_swap_lp_privana_client

    async def _retry(self, op: str, factory: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(MAX_NETWORK_RETRIES):
            try:
                return await factory()
            except NetworkError as exc:
                logger.warning(
                    "bridge.%s: transient network error (attempt %d): %s", op, attempt + 1, exc
                )
                await asyncio.sleep(self._poll_interval_sec)
        raise RuntimeError(f"bridge.{op}: network retries exhausted")

    async def _get_pending_withdrawals(self):
        # Re-acquire per attempt: the factory refreshes the bearer token when
        # it nears expiry, so a poll loop outliving the JWT keeps working.
        client = await self._client_factory()
        return await client.get_pending_withdrawals(self._lp_address)

    async def withdraw_to_chain(self, token_id: str, amount: int) -> int:
        # LiFi swaps run in parallel and all withdraw from the LP account. Hold
        # the lock until this request shows up as pending: by then its nonce is
        # spent, and the one new index is certainly ours.
        async with withdrawal_lock:
            pre = await self._retry("get_pending_withdrawals", self._get_pending_withdrawals)
            pre_indices = {w.index for w in pre.pending_withdrawals}

            async def _get_nonce():
                client = await self._client_factory()
                return await client.get_withdrawal_nonce(self._lp_address)

            nonce = (await self._retry("get_withdrawal_nonce", _get_nonce)).nonce
            signature = sign_withdraw_message(
                SignWithdrawParams(
                    account=Account.from_key(self._lp_key),
                    network=self._network,
                    verifying_contract=self._contract,
                    message=WithdrawMessage(token_id=token_id, amount=amount, nonce=nonce),
                )
            )
            client = await self._client_factory()
            submission = await client.request_withdrawal(
                WithdrawalRequest(token_id=token_id, amount=amount, nonce=nonce, signature=signature)
            )
            if submission.status not in ACCEPTED_SUBMISSION_STATUSES:
                raise RuntimeError(
                    f"withdrawal rejected: status={submission.status} detail={submission.detail}"
                )
            own_index = await self._await_new_index(pre_indices)

        for _ in range(self._max_poll_attempts):
            async def _get_info():
                fresh = await self._client_factory()
                return await fresh.get_withdrawal_info(own_index)

            info = await self._retry("get_withdrawal_info", _get_info)
            if info.resolved:
                logger.info(
                    "bridge.withdraw_to_chain: resolved index=%d tx=%s",
                    own_index, info.tx_identifier,
                )
                return own_index
            await asyncio.sleep(self._poll_interval_sec)
        raise RuntimeError(f"withdrawal unresolved after {self._max_poll_attempts} polls")

    async def _await_new_index(self, pre_indices: set[int]) -> int:
        for _ in range(self._max_poll_attempts):
            pending = await self._retry(
                "get_pending_withdrawals", self._get_pending_withdrawals
            )
            new = {w.index for w in pending.pending_withdrawals} - pre_indices
            if new:
                return min(new)
            await asyncio.sleep(self._poll_interval_sec)
        raise RuntimeError(f"withdrawal unresolved after {self._max_poll_attempts} polls")

    async def await_deposit_credit(self, chain_id: int, tx_hash: str, amount: int) -> None:
        # Accounting has no deposit watcher and rejects a transfer until it is
        # final on the source chain, so keep reporting it. Replays are
        # idempotent: pending while the sweep runs, then credited.
        tx_hash = _normalize_tx_hash(tx_hash)
        request = DepositCheckRequest(chain_id=chain_id, tx_hash=tx_hash, amount=amount)
        last: object = None
        for _ in range(self._max_poll_attempts):
            try:
                client = await self._client_factory()
                check = await client.check_deposit(request)
                if check.status == "credited":
                    return
                last = f"status={check.status} detail={check.detail}"
            except Exception as exc:
                last = exc
                logger.warning("bridge.check_deposit %s not accepted yet: %s", tx_hash, exc)
            await asyncio.sleep(self._poll_interval_sec)
        raise RuntimeError(
            f"deposit {tx_hash} not credited after {self._max_poll_attempts} polls: {last}"
        )

    async def get_deposit_address(self) -> str:
        async def _get_address():
            client = await self._client_factory()
            return await client.get_deposit_address()

        deposit = await self._retry("get_deposit_address", _get_address)
        return deposit.deposit_address
