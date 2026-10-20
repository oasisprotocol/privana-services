import asyncio
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

from web3 import Web3

from src.clients.base_evm import EvmClient, get_evm_client
from src.core.config import load_settings
from src.core.db import db_write, get_db

logger = logging.getLogger(__name__)

# How often the batcher settles mined txs and sends unmined ones again.
MAINTAIN_INTERVAL_SEC = 6.0


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
        self._maintainer: Optional[asyncio.Task] = None
        # Rounds read the open txs, then write them back: two at once would
        # each write over what the other just recorded.
        self._maintaining = asyncio.Lock()

    async def submit(self, intent: TxIntent) -> SentTx:
        """Return once `intent` is saved and handed to the node. Its receipt is
        the caller's to watch: a send error after the save proves nothing."""
        self.start()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put((intent, future))
        return await future

    def saved(self, nonce: int) -> Optional[dict]:
        """The tx saved at `nonce`, with its status once settled."""
        row = get_db().execute(
            "SELECT * FROM evm_transactions WHERE chain_id = ? AND address = ? AND nonce = ?",
            (self.chain_id, self.address, nonce),
        ).fetchone()
        return dict(row) if row else None

    async def maintain(self) -> None:
        """One round over the account's unsettled txs, lowest nonce first. A tx
        whose nonce the chain has used settles by whichever version was mined.
        The rest go out again, re-signed at fees priced now once the base fee
        outgrows them: a stuck low nonce holds up every later one."""
        async with self._maintaining:
            rows = get_db().execute(
                "SELECT nonce, raw FROM evm_transactions WHERE chain_id = ? AND address = ? "
                "AND status = 'sent' ORDER BY nonce",
                (self.chain_id, self.address),
            ).fetchall()
            if not rows:
                return
            used = await asyncio.to_thread(
                self.evm.w3.eth.get_transaction_count, Web3.to_checksum_address(self.address), "latest"
            )
            record = self._recorder(asyncio.get_running_loop())
            for nonce, raw in rows:
                try:
                    if nonce < used:
                        await self._settle(nonce)
                    else:
                        await asyncio.to_thread(self.evm.rebroadcast, raw, record)
                except Exception as exc:
                    # One tx's failure leaves the others to this round.
                    logger.warning(
                        "evm batcher %s nonce %d not maintained: %s", self.chain_id, nonce, type(exc).__name__
                    )

    async def _settle(self, nonce: int) -> None:
        row = self.saved(nonce)
        if row is None or row["status"] != "sent":
            return
        versions = [row["tx_hash"], *(row["replaced"] or "").split()]
        for tx_hash in versions:
            receipt = await asyncio.to_thread(self.evm.get_receipt, tx_hash)
            if receipt is None:
                continue
            status = "mined" if receipt["status"] == 1 else "reverted"
            db_write(
                get_db(),
                "UPDATE evm_transactions SET status = ?, tx_hash = ?, replaced = ?, updated_at = ? "
                "WHERE chain_id = ? AND address = ? AND nonce = ? AND status = 'sent'",
                (status, tx_hash, " ".join(h for h in versions if h != tx_hash) or None,
                 int(time.time()), self.chain_id, self.address, nonce),
            )
            logger.info("evm batcher %s nonce %d %s as %s", self.chain_id, nonce, status, tx_hash)
            return
        # Nodes lag: a used nonce without a receipt yet proves nothing.
        logger.info("evm batcher %s nonce %d used, receipt not seen yet", self.chain_id, nonce)

    def _recorder(self, loop: asyncio.AbstractEventLoop):
        """The callback a rebroadcast hands a re-signed version to. The version
        is saved on the loop, which owns the database, and a calling thread
        waits for it: nothing goes out unsaved."""

        def record(tx_hash: str, nonce: int, raw: str) -> None:
            try:
                on_loop = asyncio.get_running_loop() is loop
            except RuntimeError:
                on_loop = False
            if on_loop:
                self._record_version(tx_hash, nonce, raw)
                return

            async def on_the_loop() -> None:
                self._record_version(tx_hash, nonce, raw)

            asyncio.run_coroutine_threadsafe(on_the_loop(), loop).result()

        return record

    def _record_version(self, tx_hash: str, nonce: int, raw: str) -> None:
        # SET reads the row as it was, so the old hash moves into `replaced`.
        changed = db_write(
            get_db(),
            "UPDATE evm_transactions SET replaced = TRIM(COALESCE(replaced, '') || ' ' || tx_hash), "
            "tx_hash = ?, raw = ?, updated_at = ? "
            "WHERE chain_id = ? AND address = ? AND nonce = ? AND status = 'sent'",
            (tx_hash, raw, int(time.time()), self.chain_id, self.address, nonce),
        ).rowcount
        if not changed:
            # Settled meanwhile: raising here keeps the new version from going out.
            raise RuntimeError(f"nonce {nonce} settled before its new version was saved")

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())
        if self._maintainer is None or self._maintainer.done():
            self._maintainer = asyncio.create_task(self._maintain_forever())

    async def _maintain_forever(self) -> None:
        while True:
            await asyncio.sleep(MAINTAIN_INTERVAL_SEC)
            try:
                await self.maintain()
            except Exception as exc:
                # The next round looks again; nothing is lost by skipping one.
                logger.warning("evm batcher %s round failed: %s", self.chain_id, type(exc).__name__)

    async def _run(self) -> None:
        # Txs saved before a restart hold the nonces below every new one, so
        # they go out again before anything new queues behind them.
        try:
            await self.maintain()
        except Exception as exc:
            logger.warning("evm batcher %s recovery round failed: %s", self.chain_id, type(exc).__name__)
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
            if future.done():
                # Its caller stopped waiting: sending it now would leave a tx nobody tracks.
                continue
            try:
                nonce, tx_hash, raw = await self._sign_and_save(intent, nonce, fees, future)
            except _Abandoned:
                continue
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

    async def _sign_and_save(
        self, intent: TxIntent, nonce: int, fees: dict, future: asyncio.Future
    ) -> tuple[int, str, str]:
        while True:
            tx_hash, raw = await asyncio.to_thread(self.evm.sign, self._tx(intent, nonce, fees))
            if future.done():
                # Cancelled while it was signed: unsaved, so the nonce stays free.
                raise _Abandoned
            try:
                self._save(intent.owner, nonce, tx_hash, raw)
                return nonce, tx_hash, raw
            except sqlite3.IntegrityError:
                # Only a tx already saved at this nonce moves past it; any other
                # rejection is this intent's own.
                if self.saved(nonce) is None:
                    raise
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

    def _save(self, owner: str, nonce: int, tx_hash: str, raw: str) -> None:
        now = int(time.time())
        db_write(
            get_db(),
            "INSERT INTO evm_transactions (chain_id, address, nonce, owner, tx_hash, "
            "raw, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.chain_id, self.address, nonce, owner, tx_hash, raw, now, now),
        )


class _Abandoned(Exception):
    """Its caller stopped waiting before the intent was saved."""


def _settle(future: asyncio.Future, result: object = None, exc: Optional[BaseException] = None) -> None:
    # A caller that stopped waiting has cancelled its future.
    if future.done():
        return
    if exc is not None:
        future.set_exception(exc)
    else:
        future.set_result(result)


_batchers: dict[int, EvmBatcher] = {}
# Callers reach it from threads, and two batchers for one chain would hand out the same nonces.
_batchers_lock = threading.Lock()


async def recover_evm_batchers() -> None:
    """Start the LP batcher of every chain holding unsettled txs, so they settle
    or go out again after a restart without waiting for a new send there."""
    address = load_settings().liquidity_provider_address.lower()
    chains = get_db().execute(
        "SELECT DISTINCT chain_id FROM evm_transactions WHERE address = ? AND status = 'sent'",
        (address,),
    ).fetchall()
    for (chain_id,) in chains:
        try:
            batcher = await asyncio.to_thread(get_evm_batcher, chain_id)
        except Exception as exc:
            # The chain's RPC may be down at boot: keep trying without holding up startup.
            logger.warning("evm batcher %s not recovered yet: %s", chain_id, type(exc).__name__)
            task = asyncio.create_task(_recover_later(chain_id))
            _retries.add(task)
            task.add_done_callback(_retries.discard)
            continue
        batcher.start()


_retries: set[asyncio.Task] = set()


def start_evm_batcher_recovery() -> None:
    """Recover in the background: a slow RPC must not hold up startup."""
    task = asyncio.create_task(recover_evm_batchers())
    _retries.add(task)
    task.add_done_callback(_retries.discard)


async def _recover_later(chain_id: int) -> None:
    while True:
        await asyncio.sleep(MAINTAIN_INTERVAL_SEC)
        try:
            batcher = await asyncio.to_thread(get_evm_batcher, chain_id)
        except Exception as exc:
            logger.warning("evm batcher %s not recovered yet: %s", chain_id, type(exc).__name__)
            continue
        batcher.start()
        return


def get_evm_batcher(chain_id: int) -> EvmBatcher:
    """The LP account's batcher on `chain_id`: one per chain, as its nonces are."""
    with _batchers_lock:
        if chain_id not in _batchers:
            _batchers[chain_id] = EvmBatcher(get_evm_client(chain_id), chain_id)
        return _batchers[chain_id]
