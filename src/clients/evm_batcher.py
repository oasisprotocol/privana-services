import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

from web3 import Web3
from web3.exceptions import ContractLogicError, TransactionNotFound, Web3RPCError

from src.clients.base_evm import ERC20_ABI, RPC_SETTINGS, EvmClient, is_native
from src.core.config import load_settings
from src.core.db import connect, db_write, db_write_many, get_db
from src.core.validation import redact_error

logger = logging.getLogger(__name__)

BATCH_SIZE = 10
TRANSFER_GAS_LIMIT = 100_000
NATIVE_TRANSFER_GAS_LIMIT = 21_000
APPROVE_GAS_LIMIT = 80_000
IDLE_INTERVAL_SEC = 2.0
RECEIPT_POLL_SEC = 1.0
RECEIPT_TIMEOUT_SEC = 120.0
# Nodes serve a receipt a little after counting its nonce. A tx whose nonce
# has been used this long with no receipt for it lost the nonce to another tx,
# so it can never be mined.
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

# Node replies meaning it will never take the tx as signed. Any other reply
# may come from a node that took it anyway, so the tx is tried again later at
# the same nonce instead of being failed.
REFUSED = ("insufficient funds", "intrinsic gas too low", "exceeds block gas limit")
# Simulation replies meaning the call fails as built, though not by a revert.
CALL_FAILED = ("out of gas", "invalid opcode", "stack underflow", "stack overflow")


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


_ERC20 = Web3().eth.contract(abi=ERC20_ABI)


def transfer_intent(owner: str, token: Optional[str], to: str, amount: int) -> TxIntent:
    """Send `amount` of `token`, or of the native coin when `token` is native."""
    if is_native(token):
        return TxIntent(owner=owner, to=to, gas=NATIVE_TRANSFER_GAS_LIMIT, value=amount)
    data = _ERC20.encode_abi("transfer", args=[Web3.to_checksum_address(to), amount])
    return TxIntent(owner=owner, to=token, gas=TRANSFER_GAS_LIMIT, data=data)


def approve_intent(owner: str, token: str, spender: str, amount: int) -> TxIntent:
    data = _ERC20.encode_abi("approve", args=[Web3.to_checksum_address(spender), amount])
    return TxIntent(owner=owner, to=token, gas=APPROVE_GAS_LIMIT, data=data)


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
        # Tx id -> when its nonce was first seen used with no receipt for it.
        self._used_since: dict[int, float] = {}

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                progressed = self.run_round()
            except Exception as exc:
                logger.warning("evm batcher %s round failed: %s", self.chain_id, redact_error(exc))
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
                # again could send it twice. Once stuck, it is failed instead
                # and its swap parks for a person.
                if row["id"] not in self._used_since:
                    logger.warning("evm batcher %s holds the queue: nonce of %s used, no receipt yet",
                                   self.chain_id, row["owner"])
                since = self._used_since.setdefault(row["id"], time.monotonic())
                if time.monotonic() - since < STUCK_AFTER_SEC:
                    return progressed
                taken = min(n for n, _ in self._attempts(row))
                self._finish(row, FAILED, nonce=taken, error="nonce taken by another tx")
                progressed = True
            elif row["attempts"] and int(time.time()) - row["updated_at"] < RECEIPT_TIMEOUT_SEC:
                # Its last attempt may still be mined: it gets a full receipt
                # wait before it is signed again, and the txs behind wait too.
                return progressed
            else:
                unsent.append(row)
        # A new tx starts past any tx this round just found mined. A tx sent
        # before is signed again only at the nonce it already holds: at any
        # other, both versions could be mined.
        nonce = max(used, self._next_after_mined())
        fees = self.evm.fees()
        sent, last_hash = [], None
        for row in unsent:
            at = self._attempts(row)[-1][0] if row["attempts"] else nonce
            outcome = self._send(row, at, fees)
            if outcome == STOPPED:
                break
            if outcome == SENT:
                sent.append(row)
                last_hash = self._attempts(self._row(row["id"]))[-1][1]
                nonce = max(nonce, at + 1)
        if last_hash is not None:
            self._await_receipt(last_hash)
            for row in sent:
                self._settle(self._row(row["id"]), None)
        return progressed or bool(sent)

    def _send(self, row: dict, nonce: int, fees: dict) -> str:
        if row["attempts"]:
            try:
                held = self._newest_held(row)
            except Exception as exc:
                logger.warning("evm batcher %s lookup at nonce %d failed: %s", self.chain_id, nonce, redact_error(exc))
                return STOPPED
            if held is not None:
                if _includable(held, fees):
                    # The node holds a version that can still be mined: wait for it.
                    return STOPPED
                fees = {key: max(fees[key], held[key] + held[key] * FEE_BUMP_BPS // 10_000 + 1)
                        for key in ("maxFeePerGas", "maxPriorityFeePerGas")}
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
                self.evm.w3.eth.call(tx)
            except (ContractLogicError, Web3RPCError) as exc:
                # A revert or a refusal: the tx would fail, and nothing was signed.
                if isinstance(exc, ContractLogicError) or any(p in _rpc_message(exc) for p in REFUSED + CALL_FAILED):
                    self._finish(row, FAILED, error=f"simulation failed: {redact_error(exc)}")
                    return SKIPPED
                logger.warning("evm batcher %s simulation at nonce %d failed: %s", self.chain_id, nonce, redact_error(exc))
                return STOPPED
            except Exception as exc:
                # The RPC, not the tx: what this round already sent is still awaited.
                logger.warning("evm batcher %s simulation at nonce %d failed: %s", self.chain_id, nonce, redact_error(exc))
                return STOPPED
        tx_hash, raw = self.evm.sign(tx)
        # Saved before broadcast: a later round checks it for a receipt before signing again.
        db_write(
            self.db,
            "UPDATE evm_transactions SET attempts = TRIM(COALESCE(attempts, '') || ' ' || ?), "
            "updated_at = ? WHERE id = ?",
            (f"{nonce}:{tx_hash}", int(time.time()), row["id"]),
        )
        try:
            self.evm.w3.eth.send_raw_transaction(raw)
        except Web3RPCError as exc:
            self._log_send_failure(tx_hash, nonce, exc)
            # A retry is never failed: its earlier attempt may still be mined.
            if row["attempts"] or not any(phrase in _rpc_message(exc) for phrase in REFUSED):
                return STOPPED
            # The node will never take it, so the nonce stays free for the next tx.
            self._finish(row, FAILED, error=redact_error(exc))
            return SKIPPED
        except Exception as exc:
            # It may have reached the node: the next round looks for its receipt.
            self._log_send_failure(tx_hash, nonce, exc)
            return STOPPED
        logger.info("evm batcher %s sent %s for %s at nonce %d, attempt %d, max fee %d, tip %d",
                    self.chain_id, tx_hash, row["owner"], nonce, len(self._attempts(row)) + 1,
                    fees["maxFeePerGas"], fees["maxPriorityFeePerGas"])
        return SENT

    def _settle(self, row: dict, used: Optional[int]) -> Optional[bool]:
        """Whether one of the row's attempts was mined, recording its receipt.
        None when an attempt's nonce is below `used` but no receipt shows yet."""
        attempts = self._attempts(row)
        for nonce, tx_hash in attempts:
            receipt = self.evm.get_receipt(tx_hash)
            if receipt is not None:
                status = MINED if receipt["status"] == 1 else REVERTED
                self._finish(row, status, tx_hash=tx_hash, nonce=nonce,
                             logs=Web3.to_json(receipt.get("logs", [])))
                return True
        if used is not None and any(n < used for n, _ in attempts):
            return None
        return False

    def _log_send_failure(self, tx_hash: str, nonce: int, exc: Exception) -> None:
        """What the chain says about a send that failed: logs are the only way
        to trace it."""
        try:
            latest = self.evm.w3.eth.get_transaction_count(self.evm.address, "latest")
            pending = self.evm.w3.eth.get_transaction_count(self.evm.address, "pending")
            try:
                self.evm.w3.eth.get_transaction(tx_hash)
                known = True
            except TransactionNotFound:
                known = False
            logger.warning(
                "evm batcher %s send of %s at nonce %d failed (%s): latest_nonce=%s pending_nonce=%s known_to_node=%s",
                self.chain_id, tx_hash, nonce, redact_error(exc), latest, pending, known,
            )
        except Exception as probe_exc:
            logger.warning("evm batcher %s send of %s at nonce %d failed (%s); state probe failed (%s)",
                           self.chain_id, tx_hash, nonce, redact_error(exc), redact_error(probe_exc))

    def _next_after_mined(self) -> int:
        top = self.db.execute(
            "SELECT MAX(nonce) FROM evm_transactions WHERE chain_id = ? AND address = ?",
            (self.chain_id, self.evm.address.lower()),
        ).fetchone()[0]
        return 0 if top is None else top + 1

    def _newest_held(self, row: dict) -> Optional[dict]:
        """The newest of the row's attempts the node still holds."""
        for _, tx_hash in reversed(self._attempts(row)):
            try:
                return self.evm.w3.eth.get_transaction(tx_hash)
            except TransactionNotFound:
                continue
        return None

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

    def _finish(self, row: dict, status: str, tx_hash: Optional[str] = None, nonce: Optional[int] = None,
                logs: Optional[str] = None, error: Optional[str] = None) -> None:
        self._used_since.pop(row["id"], None)
        db_write(
            self.db,
            "UPDATE evm_transactions SET status = ?, tx_hash = ?, nonce = ?, logs = ?, error = ?, "
            "updated_at = ? WHERE id = ?",
            (status, tx_hash, nonce, logs, error, int(time.time()), row["id"]),
        )
        logger.info("evm batcher %s tx %d for %s %s%s", self.chain_id, row["id"], row["owner"], status,
                    f": {error}" if error else f" as {tx_hash}")


def _includable(held: dict, fees: dict) -> bool:
    """Whether `held` still pays the base fee and tip priced in `fees`."""
    base_fee = (fees["maxFeePerGas"] - fees["maxPriorityFeePerGas"]) // 2
    return (held["maxPriorityFeePerGas"] >= fees["maxPriorityFeePerGas"]
            and held["maxFeePerGas"] >= base_fee + fees["maxPriorityFeePerGas"])


def _rpc_message(exc: Web3RPCError) -> str:
    """The node's own error message: web3 wraps it in the error's text."""
    error = (exc.rpc_response or {}).get("error") or {}
    return str(error.get("message") or exc).lower()


def submit(chain_id: int, *intents: TxIntent) -> list[int]:
    """Queue `intents` for the LP account's batcher on `chain_id` in one write,
    so they get consecutive ids and go out in that order, and return their ids.
    An owner already queued keeps its tx: submitting again after a restart
    sends nothing twice."""
    now = int(time.time())
    address = load_settings().liquidity_provider_address.lower()
    db_write_many(
        get_db(),
        "INSERT INTO evm_transactions (chain_id, address, owner, to_address, data, value, gas, "
        "simulate, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(owner) DO NOTHING",
        [(chain_id, address, i.owner, i.to, i.data, str(i.value), i.gas, int(i.simulate), now, now)
         for i in intents],
    )
    tx_ids = [find_evm_tx(i.owner)["id"] for i in intents]
    for tx_id, intent in zip(tx_ids, intents):
        logger.info("evm batcher %s queued tx %d for %s", chain_id, tx_id, intent.owner)
    return tx_ids


def find_evm_tx(owner: str) -> Optional[dict]:
    """The tx queued for `owner`, on whichever chain."""
    row = get_db().execute("SELECT * FROM evm_transactions WHERE owner = ?", (owner,)).fetchone()
    return dict(row) if row else None


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
                    logger.warning("evm batcher for %s: RPC unreachable (%s)", setting, redact_error(exc))
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
        logger.error("evm batcher for %s stopped: %s", setting, redact_error(exc))
    finally:
        if db is not None:
            db.close()
