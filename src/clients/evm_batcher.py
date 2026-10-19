import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from web3 import Web3

from src.clients.base_evm import EvmClient, get_evm_client
from src.core.db import db_write, get_db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TxIntent:
    """A tx to send from the batcher's account. `owner` names what it is for,
    such as `swap:<id>:lifi`, so a saved tx traces back to its swap."""

    owner: str
    to: str
    gas: int
    data: str = "0x"
    value: int = 0


@dataclass(frozen=True)
class SentTx:
    nonce: int
    tx_hash: str


class EvmBatcher:
    """The only sender for one account on one chain. It hands out the
    account's nonces itself, so a send never waits for another's receipt:
    queued intents are signed with consecutive nonces, saved, then broadcast."""

    def __init__(self, evm: EvmClient, chain_id: int) -> None:
        self.evm = evm
        self.chain_id = chain_id
        self.address = evm.address.lower()
        self._queue: asyncio.Queue[tuple[TxIntent, asyncio.Future]] = asyncio.Queue()
        self._next_nonce: Optional[int] = None
        self._worker: Optional[asyncio.Task] = None

    async def submit(self, intent: TxIntent) -> SentTx:
        """Return once `intent` is saved and handed to the node. Its receipt is
        the caller's to watch: a send error after the save proves nothing."""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put((intent, future))
        return await future

    async def _run(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while not self._queue.empty():
                batch.append(self._queue.get_nowait())
            await self._flush(batch)

    async def _flush(self, batch: list[tuple[TxIntent, asyncio.Future]]) -> None:
        # One price for the whole batch: the intents queued together go out together.
        try:
            fees = await asyncio.to_thread(self.evm.fees)
            nonce = await self._first_free_nonce()
        except Exception as exc:
            for _, future in batch:
                _settle(future, exc=exc)
            return
        for intent, future in batch:
            try:
                tx_hash, raw = await asyncio.to_thread(self.evm.sign, self._tx(intent, nonce, fees))
                if not self._save(intent.owner, nonce, tx_hash, raw):
                    # Taken by a tx saved since the count was read: past it, and sign again.
                    nonce += 1
                    tx_hash, raw = await asyncio.to_thread(self.evm.sign, self._tx(intent, nonce, fees))
                    if not self._save(intent.owner, nonce, tx_hash, raw):
                        raise RuntimeError(f"nonce {nonce} already saved for {self.address}")
            except Exception as exc:
                # Nothing saved, nothing sent: the nonce stays free for the next intent.
                _settle(future, exc=exc)
                continue
            self._next_nonce = nonce + 1
            try:
                await asyncio.to_thread(self.evm.broadcast, raw)
                logger.info("evm batcher %s sent %s for %s at nonce %d", self.chain_id, tx_hash, intent.owner, nonce)
            except Exception as exc:
                # The node may hold it anyway, and the saved raw goes out again.
                logger.warning(
                    "evm batcher %s broadcast of %s at nonce %d failed: %s",
                    self.chain_id, tx_hash, nonce, type(exc).__name__,
                )
            _settle(future, result=SentTx(nonce=nonce, tx_hash=tx_hash))
            nonce += 1

    async def _first_free_nonce(self) -> int:
        if self._next_nonce is None:
            # Past the node's view and past every tx already saved for this
            # account, so a restart never signs a second tx at a used nonce.
            pending = await asyncio.to_thread(
                self.evm.w3.eth.get_transaction_count, Web3.to_checksum_address(self.address), "pending"
            )
            saved = get_db().execute(
                "SELECT MAX(nonce) FROM evm_transactions WHERE chain_id = ? AND address = ?",
                (self.chain_id, self.address),
            ).fetchone()[0]
            self._next_nonce = max(pending, -1 if saved is None else saved + 1)
        return self._next_nonce

    def _tx(self, intent: TxIntent, nonce: int, fees: dict) -> dict:
        return {
            "from": Web3.to_checksum_address(self.address),
            "to": Web3.to_checksum_address(intent.to),
            "data": intent.data,
            "value": intent.value,
            "gas": intent.gas,
            "nonce": nonce,
            "chainId": self.chain_id,
            **fees,
        }

    def _save(self, owner: str, nonce: int, tx_hash: str, raw: str) -> bool:
        """Whether the tx was saved: false when its nonce already holds one."""
        now = int(time.time())
        return db_write(
            get_db(),
            "INSERT OR IGNORE INTO evm_transactions (chain_id, address, nonce, owner, tx_hash, "
            "raw, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.chain_id, self.address, nonce, owner, tx_hash, raw, now, now),
        ).rowcount == 1


def _settle(future: asyncio.Future, result: object = None, exc: Optional[BaseException] = None) -> None:
    # A caller that stopped waiting has cancelled its future.
    if future.done():
        return
    if exc is not None:
        future.set_exception(exc)
    else:
        future.set_result(result)


_batchers: dict[int, EvmBatcher] = {}


def get_evm_batcher(chain_id: int) -> EvmBatcher:
    """The LP account's batcher on `chain_id`: one per chain, as its nonces are."""
    if chain_id not in _batchers:
        _batchers[chain_id] = EvmBatcher(get_evm_client(chain_id), chain_id)
    return _batchers[chain_id]
