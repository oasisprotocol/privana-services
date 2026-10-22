import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

from web3 import Web3
from web3.exceptions import ContractLogicError, TransactionNotFound, Web3RPCError

from src.clients.base_evm import EvmClient
from src.core.config import load_settings
from src.core.db import connect, db_write, get_db
from src.core.validation import URL_PATTERN

logger = logging.getLogger(__name__)

BATCH_SIZE = 10
IDLE_INTERVAL_SEC = 2.0
RECEIPT_POLL_SEC = 1.0
RECEIPT_TIMEOUT_SEC = 120.0
# A tx still unsettled this long after its first attempt is failed, so it
# cannot hold up the txs behind it. It is never signed at a new nonce, which
# could send it twice: its swap parks for a person to check.
STUCK_AFTER_SEC = 900
# A retry raises both fees by at least this much, or nodes refuse to replace
# the earlier attempt.
FEE_BUMP_BPS = 1250

# Status of a queued tx.
QUEUED = "queued"
MINED = "mined"
REVERTED = "reverted"
FAILED = "failed"

# What became of a tx a round tried to send.
SENT = "sent"
SKIPPED = "skipped"
STOPPED = "stopped"

# Node replies meaning it already holds the tx.
ALREADY_KNOWN = ("already known", "known transaction", "alreadyknown")
# Node replies meaning the nonce is not this tx's to take yet: retry next round.
NONCE_TAKEN = ("nonce too low", "nonce too high", "replacement transaction underpriced",
               "transaction underpriced")


@dataclass(frozen=True)
class TxIntent:
    """A tx to send from the batcher's account. `owner` names what it is for,
    such as `swap:<id>:lifi`. A tx that depends on an earlier one in the same
    batch, like a swap after its approval, cannot be simulated before that
    one is mined, so it sets `simulate` to False."""

    owner: str
    to: str
    gas: int
    data: str = "0x"
    value: int = 0
    simulate: bool = True


class EvmBatcher:
    """The only sender for one account on one chain. It runs in its own thread
    with its own database connection: callers on the event loop queue txs with
    `submit` and poll `result`. Each round signs the oldest queued txs with
    consecutive nonces from a fresh count, sends them back to back, and waits
    only for the last receipt."""

    def __init__(self, evm: EvmClient, chain_id: int, db: sqlite3.Connection) -> None:
        self.evm = evm
        self.chain_id = chain_id
        self.db = db

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                progressed = self.run_round()
            except Exception as exc:
                logger.warning("evm batcher %s round failed: %s", self.chain_id, _error(exc))
                progressed = False
            if not progressed:
                stop.wait(IDLE_INTERVAL_SEC)

    def run_round(self) -> bool:
        """One batch. Returns whether it settled or sent anything."""
        rows = [dict(r) for r in self.db.execute(
            "SELECT * FROM evm_transactions WHERE chain_id = ? AND address = ? AND status = ? "
            "ORDER BY id LIMIT ?",
            (self.chain_id, self.evm.address.lower(), QUEUED, BATCH_SIZE),
        ).fetchall()]
        if not rows:
            return False
        used = self.evm.w3.eth.get_transaction_count(self.evm.address, "latest")
        unsent, progressed = [], False
        for row in rows:
            settled = self._settle(row, used)
            if settled:
                progressed = True
            elif self._stuck(row):
                self._finish(row["id"], FAILED, error=f"not mined {STUCK_AFTER_SEC}s after it was first sent")
                progressed = True
            elif settled is None:
                # An earlier attempt holds a used nonce but has no receipt yet:
                # signing again now could send it twice.
                return progressed
            else:
                unsent.append(row)
        # A node behind the others can report a count below a nonce already
        # mined here: start past both.
        nonce = max(used, self._next_after_mined())
        fees = self.evm.fees()
        sent, last_hash = [], None
        for row in unsent:
            outcome = self._send(row, nonce, fees)
            if outcome == STOPPED:
                break
            if outcome == SENT:
                sent.append(row)
                last_hash = self._attempts(self._row(row["id"]))[-1][1]
                nonce += 1
        if last_hash is not None:
            self._await_receipt(last_hash)
            for row in sent:
                self._settle(self._row(row["id"]), None)
        return progressed or bool(sent)

    def _send(self, row: dict, nonce: int, fees: dict) -> str:
        if row["attempts"]:
            fees = self._bumped(fees, self._attempts(row)[-1][1])
        tx = {
            "from": self.evm.address,
            "to": Web3.to_checksum_address(row["to_address"]),
            "data": row["data"],
            "value": int(row["value"]),
            "gas": row["gas"],
            "nonce": nonce,
            "chainId": self.chain_id,
            **fees,
        }
        # A retry keeps its place in the nonce order: it is never dropped on a
        # simulation, since its earlier attempt may still be mined.
        if row["simulate"] and not row["attempts"]:
            try:
                self.evm.w3.eth.call({k: tx[k] for k in ("from", "to", "data", "value", "gas")})
            except (ContractLogicError, Web3RPCError) as exc:
                # The node answered: the tx would fail, and nothing was signed.
                self._finish(row["id"], FAILED, error=f"simulation failed: {_error(exc)}")
                return SKIPPED
            except Exception as exc:
                # The RPC, not the tx: what this round already sent is still awaited.
                logger.warning("evm batcher %s simulation at nonce %d failed: %s", self.chain_id, nonce, _error(exc))
                return STOPPED
        tx_hash, raw = self.evm.sign(tx)
        # Saved before broadcast: a later round checks it for a receipt before signing again.
        db_write(
            self.db,
            "UPDATE evm_transactions SET attempts = TRIM(COALESCE(attempts, '') || ' ' || ?), "
            "sent_at = COALESCE(sent_at, ?), updated_at = ? WHERE id = ?",
            (f"{nonce}:{tx_hash}", int(time.time()), int(time.time()), row["id"]),
        )
        try:
            self.evm.broadcast(raw)
        except Web3RPCError as exc:
            message = str(exc).lower()
            if any(phrase in message for phrase in ALREADY_KNOWN):
                logger.info("evm batcher %s node already holds %s at nonce %d", self.chain_id, tx_hash, nonce)
                return SENT
            # A retry's earlier attempt may still be mined, so it is never failed here.
            if any(phrase in message for phrase in NONCE_TAKEN) or row["attempts"]:
                logger.info("evm batcher %s nonce %d not taken (%s), retrying next round",
                            self.chain_id, nonce, _error(exc))
                return STOPPED
            # The node refused it outright, so the nonce stays free for the next tx.
            self._finish(row["id"], FAILED, error=_error(exc))
            return SKIPPED
        except Exception as exc:
            # It may have reached the node: the next round looks for its receipt.
            logger.warning("evm batcher %s broadcast at nonce %d failed: %s", self.chain_id, nonce, _error(exc))
            return STOPPED
        logger.info("evm batcher %s sent %s for %s at nonce %d", self.chain_id, tx_hash, row["owner"], nonce)
        return SENT

    def _settle(self, row: dict, used: Optional[int]) -> Optional[bool]:
        """Whether one of the row's attempts was mined, recording its receipt.
        None when an attempt's nonce is below `used` but no receipt shows yet."""
        attempts = self._attempts(row)
        for nonce, tx_hash in attempts:
            receipt = self.evm.get_receipt(tx_hash)
            if receipt is not None:
                status = MINED if receipt["status"] == 1 else REVERTED
                self._finish(row["id"], status, tx_hash=tx_hash, nonce=nonce,
                             logs=Web3.to_json(receipt.get("logs", [])))
                return True
        if used is not None and any(n < used for n, _ in attempts):
            return None
        return False

    def _stuck(self, row: dict) -> bool:
        return row["sent_at"] is not None and time.time() - row["sent_at"] > STUCK_AFTER_SEC

    def _next_after_mined(self) -> int:
        top = self.db.execute(
            "SELECT MAX(nonce) FROM evm_transactions WHERE chain_id = ? AND address = ?",
            (self.chain_id, self.evm.address.lower()),
        ).fetchone()[0]
        return 0 if top is None else top + 1

    def _bumped(self, fees: dict, last_hash: str) -> dict:
        """`fees`, raised past the last attempt's if the node still holds it."""
        try:
            last = self.evm.w3.eth.get_transaction(last_hash)
        except TransactionNotFound:
            return fees
        bump = lambda fee: fee + fee * FEE_BUMP_BPS // 10_000 + 1  # noqa: E731
        return {
            "maxFeePerGas": max(fees["maxFeePerGas"], bump(last["maxFeePerGas"])),
            "maxPriorityFeePerGas": max(fees["maxPriorityFeePerGas"], bump(last["maxPriorityFeePerGas"])),
        }

    def _await_receipt(self, tx_hash: str) -> None:
        deadline = time.monotonic() + RECEIPT_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if self.evm.get_receipt(tx_hash) is not None:
                return
            time.sleep(RECEIPT_POLL_SEC)
        logger.warning("evm batcher %s receipt of %s not seen, retrying next round", self.chain_id, tx_hash)

    def _row(self, tx_id: int) -> dict:
        return dict(self.db.execute("SELECT * FROM evm_transactions WHERE id = ?", (tx_id,)).fetchone())

    @staticmethod
    def _attempts(row: dict) -> list[tuple[int, str]]:
        pairs = (a.split(":", 1) for a in (row["attempts"] or "").split())
        return [(int(n), h) for n, h in pairs]

    def _finish(self, tx_id: int, status: str, tx_hash: Optional[str] = None, nonce: Optional[int] = None,
                logs: Optional[str] = None, error: Optional[str] = None) -> None:
        db_write(
            self.db,
            "UPDATE evm_transactions SET status = ?, tx_hash = ?, nonce = ?, logs = ?, error = ?, "
            "updated_at = ? WHERE id = ? AND status = ?",
            (status, tx_hash, nonce, logs, error, int(time.time()), tx_id, QUEUED),
        )
        logger.info("evm batcher %s tx %d %s%s", self.chain_id, tx_id, status,
                    f": {error}" if error else f" as {tx_hash}")


def _error(exc: Exception) -> str:
    # An RPC URL in the text can carry its API key.
    return f"{type(exc).__name__}: {URL_PATTERN.sub('[url]', str(exc))}"


def submit(chain_id: int, intent: TxIntent) -> int:
    """Queue `intent` for the LP account's batcher on `chain_id` and return its id."""
    now = int(time.time())
    return db_write(
        get_db(),
        "INSERT INTO evm_transactions (chain_id, address, owner, to_address, data, value, gas, "
        "simulate, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (chain_id, load_settings().liquidity_provider_address.lower(), intent.owner, intent.to,
         intent.data, str(intent.value), intent.gas, int(intent.simulate), now, now),
    ).lastrowid


def result(tx_id: int) -> Optional[dict]:
    """The queued tx: `status` is QUEUED until it is MINED, REVERTED or FAILED."""
    row = get_db().execute("SELECT * FROM evm_transactions WHERE id = ?", (tx_id,)).fetchone()
    return dict(row) if row else None


# Settings naming the RPC of each chain the LP account sends on.
RPC_SETTINGS = ("base_rpc_url", "ethereum_rpc_url", "hyperevm_rpc_url")

_stop = threading.Event()
_threads: list[threading.Thread] = []
# Chain id -> the setting whose batcher serves it: a second RPC for the same
# chain would start a second sender for the same nonces.
_serving: dict[int, str] = {}


def start_evm_batchers() -> None:
    """Start one batcher thread per configured chain."""
    _stop.clear()
    settings = load_settings()
    for setting in RPC_SETTINGS:
        rpc_url = getattr(settings, setting)
        if rpc_url:
            thread = threading.Thread(target=_serve, args=(setting, rpc_url), name=f"evm-batcher-{setting}", daemon=True)
            thread.start()
            _threads.append(thread)


def stop_evm_batchers(timeout: float = 60.0) -> None:
    _stop.set()
    for thread in _threads:
        thread.join(timeout)
    _threads.clear()
    _serving.clear()


def _serve(setting: str, rpc_url: str) -> None:
    db = connect()
    try:
        evm = EvmClient(rpc_url, load_settings().liquidity_provider_secret_key)
        while not _stop.is_set():
            try:
                chain_id = evm.w3.eth.chain_id
                break
            except Exception as exc:
                # Type only: the error text can carry the RPC URL and its key.
                logger.warning("evm batcher for %s: RPC unreachable (%s)", setting, type(exc).__name__)
                _stop.wait(IDLE_INTERVAL_SEC)
        else:
            return
        if _serving.setdefault(chain_id, setting) != setting:
            logger.error("evm batcher for %s not started: chain %s is already served by %s",
                         setting, chain_id, _serving[chain_id])
            return
        logger.info("evm batcher %s started for %s", chain_id, setting)
        EvmBatcher(evm, chain_id, db).run(_stop)
    finally:
        db.close()
