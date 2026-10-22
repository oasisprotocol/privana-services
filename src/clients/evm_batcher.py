import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

from web3 import Web3
from web3.exceptions import ContractLogicError, TransactionNotFound, Web3RPCError

from src.clients.base_evm import RPC_SETTINGS, EvmClient
from src.core.config import load_settings
from src.core.db import connect, db_write, get_db
from src.core.validation import URL_PATTERN

logger = logging.getLogger(__name__)

BATCH_SIZE = 10
IDLE_INTERVAL_SEC = 2.0
RECEIPT_POLL_SEC = 1.0
RECEIPT_TIMEOUT_SEC = 120.0
# Nodes serve a receipt a little after counting its nonce. A tx whose nonce is
# still used with no receipt this long after it was first sent lost the nonce
# to another tx, so it can never be mined.
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
ALREADY_KNOWN = ("already known", "alreadyknown")
KNOWN_TRANSACTION = "known transaction"
# Node replies about the nonce rather than the tx: the round stops and a later
# one looks again.
NONCE_REPLIES = ("nonce too low", "nonce too high", "replacement transaction underpriced")
# A key can sit in a URL or, in a requests error, in the path after "url: ".
_RPC_PATH = re.compile(r"(?<=url: )\S+")


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

    def __init__(self, evm: EvmClient, chain_id: int, db: sqlite3.Connection, stop: threading.Event) -> None:
        self.evm = evm
        self.chain_id = chain_id
        self.db = db
        self.stop = stop

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                progressed = self.run_round()
            except Exception as exc:
                logger.warning("evm batcher %s round failed: %s", self.chain_id, _error(exc))
                progressed = False
            if not progressed:
                self.stop.wait(IDLE_INTERVAL_SEC)

    def run_round(self) -> bool:
        """One batch. Returns whether it settled or sent anything."""
        rows = [dict(r) for r in self.db.execute(
            "SELECT * FROM evm_transactions WHERE chain_id = ? AND address = ? AND status = ? "
            "ORDER BY id LIMIT ?",
            (self.chain_id, self.evm.address.lower(), QUEUED, BATCH_SIZE),
        ).fetchall()]
        if not rows:
            return False
        # A node behind the others can report a count below a nonce already
        # mined here: take the higher.
        used = max(self.evm.w3.eth.get_transaction_count(self.evm.address, "latest"), self._next_after_mined())
        unsent, progressed = [], False
        for row in rows:
            settled = self._settle(row, used)
            if settled:
                progressed = True
            elif settled is None:
                # An attempt holds a used nonce but has no receipt yet: signing
                # again could send it twice. Once stuck, it is failed instead,
                # never signed at a new nonce, and its swap parks for a person.
                if not self._stuck(row):
                    return progressed
                self._finish(row["id"], FAILED, error="nonce taken by another tx")
                progressed = True
            elif row["attempts"] and int(time.time()) - row["updated_at"] < RECEIPT_TIMEOUT_SEC:
                # Its last attempt may still be mined: it gets a full receipt
                # wait before it is signed again, and the txs behind wait too.
                return progressed
            else:
                unsent.append(row)
        # Past any tx this round just found mined.
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
            try:
                fees = self._bumped(fees, row)
            except Exception as exc:
                logger.warning("evm batcher %s fee lookup at nonce %d failed: %s", self.chain_id, nonce, _error(exc))
                return STOPPED
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
            if any(phrase in message for phrase in ALREADY_KNOWN) or message.startswith(KNOWN_TRANSACTION):
                logger.info("evm batcher %s node already holds %s at nonce %d", self.chain_id, tx_hash, nonce)
                return SENT
            # A retry's earlier attempt may still be mined, so it is never failed here.
            if any(phrase in message for phrase in NONCE_REPLIES) or row["attempts"]:
                logger.info("evm batcher %s send at nonce %d refused (%s), retrying in a later round",
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

    def _bumped(self, fees: dict, row: dict) -> dict:
        """`fees`, raised past the newest attempt the node still holds, which a
        retry must outbid to replace."""
        for _, tx_hash in reversed(self._attempts(row)):
            try:
                held = self.evm.w3.eth.get_transaction(tx_hash)
            except TransactionNotFound:
                continue
            return {
                key: max(fees[key], held[key] + held[key] * FEE_BUMP_BPS // 10_000 + 1)
                for key in ("maxFeePerGas", "maxPriorityFeePerGas")
            }
        return fees

    def _await_receipt(self, tx_hash: str) -> None:
        deadline = time.monotonic() + RECEIPT_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if self.evm.get_receipt(tx_hash) is not None:
                return
            if self.stop.wait(RECEIPT_POLL_SEC):
                return
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
    """`exc` for a log line, without the RPC URL or path that can carry its key."""
    return f"{type(exc).__name__}: {_RPC_PATH.sub('[url]', URL_PATTERN.sub('[url]', str(exc)))}"


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


# Long enough for a thread in the middle of an RPC call (30s timeout) to end its round.
STOP_TIMEOUT_SEC = 60.0

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


def stop_evm_batchers() -> None:
    _stop.set()
    for thread in _threads:
        thread.join(STOP_TIMEOUT_SEC)
    _threads.clear()
    _serving.clear()


def _serve(setting: str, rpc_url: str) -> None:
    db = None
    try:
        db = connect()
        evm = EvmClient(rpc_url, load_settings().liquidity_provider_secret_key)
        failures = 0
        while not _stop.is_set():
            try:
                chain_id = evm.w3.eth.chain_id
                break
            except Exception as exc:
                # Once a minute while it stays down.
                if failures % 30 == 0:
                    logger.warning("evm batcher for %s: RPC unreachable (%s)", setting, _error(exc))
                failures += 1
                _stop.wait(IDLE_INTERVAL_SEC)
        else:
            return
        if _serving.setdefault(chain_id, setting) != setting:
            logger.error("evm batcher for %s not started: chain %s is already served by %s",
                         setting, chain_id, _serving[chain_id])
            return
        logger.info("evm batcher %s started for %s", chain_id, setting)
        EvmBatcher(evm, chain_id, db, _stop).run()
    except Exception as exc:
        logger.error("evm batcher for %s stopped: %s", setting, _error(exc))
    finally:
        if db is not None:
            db.close()
