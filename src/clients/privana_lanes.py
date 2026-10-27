"""Privana writes, one lane per nonce counter of an account, so each nonce is
signed for one write at a time and settled by the counter on chain.

Transfer lanes spend an account's transfer nonce. The account's Sapphire
batcher runs its lane at the start of each round (`run_transfer_lanes`):
read `transferNonces[signer]`, settle sent rows once their own evm tx is
final, then sign queued rows at consecutive nonces and queue their evm txs in
the same SQLite transaction.

Withdraw lanes spend an account's withdrawal nonce: one asyncio worker per
account (`start_withdraw_lanes`) sends one signed request at a time through
the Privana API, and resends it unchanged until the nonce moves past it.

For callers:
- `submit_privana(signer, *intents)` queues transfer lane rows built by
  `transfer_balance_intent` or `swap_intent` and returns their ids. `signer`
  is the account whose transfer nonce the row spends, which sends it too.
- `submit_withdrawal(signer, owner, token_id, amount)` queues a withdrawal of
  `signer`'s balance to its own address on the token's chain.
- `find_privana_tx(owner)` is the row: `status` stays QUEUED or SENT until
  it is LANDED or FAILED, with `error` saying why.
Submitting an owner again returns its row, so a restart sends nothing twice.
A row is one attempt: to try again after FAILED, submit a new owner.
"""

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from eth_account import Account
from hexbytes import HexBytes
from privana import PrivanaClient
from privana.client.errors import AccountingApiError, NetworkError
from privana.types import WithdrawalRequest
from web3 import Web3

from src.clients.accounting import get_accounting_client
from src.clients.base_evm import EvmClient
from src.clients.evm_batcher import FAILED as EVM_FAILED
from src.clients.evm_batcher import GAS_REFUSED, SAPPHIRE_BATCH_SIZE
from src.clients.evm_batcher import MINED as EVM_MINED
from src.clients.evm_batcher import QUEUED as EVM_QUEUED
from src.clients.privana import ACCEPTED_SUBMISSION_STATUSES, get_swap_lp_privana_client
from src.clients.sapphire import DEFAULT_GAS_LIMIT
from src.core.abi import load_abi
from src.core.config import load_settings
from src.core.db import db_transaction, db_write, db_write_many, get_db
from src.core.eip712 import sign_transfer, sign_withdrawal
from src.models.settings import Settings

logger = logging.getLogger(__name__)

# What a row does.
TRANSFER = "transfer"
SWAP = "swap"
WITHDRAW = "withdraw"

# Status of a row.
QUEUED = "queued"
SENT = "sent"
LANDED = "landed"
FAILED = "failed"

# How long a sender signs nothing new after the node refused it for gas: an
# operator tops it up in minutes, and anything signed sooner is refused too.
GAS_REFUSAL_PAUSE_SEC = 60
# Gas of each kind's call, the limits Sapphire calls are sent with today: a
# swap makes two transfers.
GAS_LIMITS = {TRANSFER: DEFAULT_GAS_LIMIT, SWAP: 2 * DEFAULT_GAS_LIMIT}
FOREIGN_SPEND = "nonce spent elsewhere; manual recovery required"

# A withdrawal lands a block or two after the API sends it.
WITHDRAW_POLL_SEC = 5.0
# The API's own Sapphire tx lands within this even when the proxy cuts its
# reply at 30 s, so a request still not landed is sent again.
WITHDRAW_RESEND_SEC = 120
# Sends of one withdrawal (about half an hour of them) before the lane stops
# resending it and waits for a person. A request whose reply was lost can
# still land, so a withdrawal sent more than once is never failed: its nonce
# is not free until the counter moves past it.
WITHDRAW_MAX_SENDS = 15
# The API is still busy with a request at this nonce: it may yet land.
API_CONFLICT = 409

_ACCOUNTING = Web3().eth.contract(abi=load_abi("Accounting"))
_SWAP_MANAGER = Web3().eth.contract(abi=load_abi("SwapManager"))


@dataclass(frozen=True)
class PrivanaIntent:
    """A transfer lane write. Its signed transfer moves `payload`'s `amount`
    of `token` to `to`; a swap adds the user's side.
    `owner` names what it is for, such as `swap:<id>:credit`."""

    owner: str
    kind: str
    payload: dict


def transfer_balance_intent(owner: str, to: str, token_id: str, amount: int) -> PrivanaIntent:
    """`Accounting.transferBalance` of the signer's `amount` to `to`."""
    return PrivanaIntent(owner, TRANSFER, {"to": to, "token": token_id, "amount": amount})


def swap_intent(owner: str, user: str, input_token_id: str, input_amount: int, input_nonce: int,
                input_signature: str, output_token_id: str, output_amount: int) -> PrivanaIntent:
    """`SwapManager.swap`: the user's signed input to the LP and the LP's
    output to the user, both or neither."""
    return PrivanaIntent(owner, SWAP, {
        "to": user, "token": output_token_id, "amount": output_amount,
        "input": {"token": input_token_id, "amount": input_amount, "nonce": input_nonce,
                  "signature": input_signature},
    })


def submit_privana(signer: str, *intents: PrivanaIntent) -> list[int]:
    """Queue `intents` on `signer`'s transfer lane, in one write and in that
    order, and return their ids."""
    signer = signer.lower()
    if signer not in _transfer_keys():
        raise ValueError(f"no key signs Privana transfers of {signer}")
    if any(i.kind not in GAS_LIMITS for i in intents):
        raise ValueError(f"not a transfer lane kind: {[i.kind for i in intents]}")
    now = int(time.time())
    db_write_many(
        get_db(),
        "INSERT INTO privana_transactions (signer, owner, kind, payload, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(owner) DO NOTHING",
        [(signer, i.owner, i.kind, json.dumps(i.payload), now, now) for i in intents],
    )
    ids = [find_privana_tx(i.owner)["id"] for i in intents]
    for row_id, intent in zip(ids, intents):
        logger.info("privana lane %s queued %s %d for %s", signer, intent.kind, row_id, intent.owner)
    return ids


def submit_withdrawal(signer: str, owner: str, token_id: str, amount: int) -> int:
    """Queue a withdrawal of `amount` of `token_id` from `signer`'s balance
    and return its id."""
    signer = signer.lower()
    if signer not in _withdraw_lanes():
        raise ValueError(f"no withdraw lane for {signer}")
    now = int(time.time())
    db_write(
        get_db(),
        "INSERT INTO privana_transactions (signer, owner, kind, payload, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(owner) DO NOTHING",
        (signer, owner, WITHDRAW, json.dumps({"token": token_id, "amount": amount}), now, now),
    )
    row_id = find_privana_tx(owner)["id"]
    logger.info("privana withdraw lane %s queued withdrawal %d for %s", signer, row_id, owner)
    return row_id


def find_privana_tx(owner: str) -> Optional[dict]:
    row = get_db().execute("SELECT * FROM privana_transactions WHERE owner = ?", (owner,)).fetchone()
    return dict(row) if row else None


def run_transfer_lanes(db: sqlite3.Connection, evm: EvmClient) -> bool:
    """Settle and sign the lanes `evm`'s account sends, in its Sapphire
    batcher's thread and connection. Returns whether anything changed."""
    sender = evm.address.lower()
    if db.execute(
        "SELECT 1 FROM privana_transactions WHERE signer = ? AND kind != ? AND status IN (?, ?) LIMIT 1",
        (sender, WITHDRAW, QUEUED, SENT),
    ).fetchone() is None:
        return False
    gas_refused = db.execute(
        "SELECT 1 FROM evm_transactions WHERE chain_id = ? AND address = ? AND status = ? "
        "AND (instr(error, ?) > 0 OR instr(error, ?) > 0) AND updated_at > ? LIMIT 1",
        (evm.chain_id, sender, EVM_FAILED, *GAS_REFUSED, int(time.time()) - GAS_REFUSAL_PAUSE_SEC),
    ).fetchone() is not None
    try:
        return _run_lane(db, evm, sender, gas_refused)
    except Exception as exc:
        # The batch still goes out after this: it holds what was signed before.
        logger.warning("privana lane %s round failed: %s", sender, exc)
        return False


def _run_lane(db: sqlite3.Connection, evm: EvmClient, signer: str, gas_refused: bool) -> bool:
    # A node behind the others can report a count below a nonce already
    # landed here: take the higher.
    counter = max(get_accounting_client().transfer_nonce(signer), _next_after_landed(db, signer, TRANSFER))
    progressed = False
    for row in _lane_rows(db, signer, SENT):
        progressed = _settle(db, row, counter) or progressed
    # One batch at a time: the next is signed from the counter it leaves.
    if _lane_rows(db, signer, SENT) or not _lane_rows(db, signer, QUEUED, 1):
        return progressed
    if gas_refused:
        logger.warning("privana lane %s signs nothing: %s was refused for gas in the last %d s",
                       signer, evm.address, GAS_REFUSAL_PAUSE_SEC)
        return progressed
    key = _transfer_keys().get(signer)
    if key is None:
        logger.error("privana lane %s cannot sign: no key for it is configured", signer)
        return progressed
    return _sign(db, evm, signer, key, counter) or progressed


def _settle(db: sqlite3.Connection, row: dict, counter: int) -> bool:
    """Settle a sent row once its own evm tx is final. Returns whether it did."""
    tx = db.execute("SELECT status, tx_hash, error, simulate FROM evm_transactions WHERE owner = ?",
                    (row["evm_owner"],)).fetchone()
    if tx["status"] == EVM_QUEUED:
        return False
    if tx["status"] == EVM_MINED:
        # Mined, so its transfer ran: a counter not past it is a lagging read.
        if counter <= row["nonce"]:
            return False
        _finish(db, row, LANDED)
        return True
    if counter > row["nonce"]:
        _finish(db, row, FAILED, error=FOREIGN_SPEND)
        logger.error("privana lane %s: nonce %d of %s %d for %s was spent by a tx we did not send (ours %s)",
                     row["signer"], row["nonce"], row["kind"], row["id"], row["owner"], row["evm_owner"])
        return True
    outcome = tx["error"] or f"reverted as {tx['tx_hash']}"
    # Simulated against the state at its own nonce, so its failure is its own.
    # The rows after it reverted because it did not land, and are simulated
    # in turn once first; a gas refusal is not the row's fault.
    if tx["simulate"] and row["nonce"] == counter and not any(phrase in outcome for phrase in GAS_REFUSED):
        _finish(db, row, FAILED, error=outcome)
        return True
    db_write(
        db,
        "UPDATE privana_transactions SET status = ?, nonce = NULL, signature = NULL, evm_owner = NULL, "
        "error = ?, updated_at = ? WHERE id = ?",
        (QUEUED, outcome, int(time.time()), row["id"]),
    )
    logger.info("privana lane %s %s %d for %s queued again from nonce %d: %s",
                row["signer"], row["kind"], row["id"], row["owner"], row["nonce"], outcome)
    return True


def _sign(db: sqlite3.Connection, evm: EvmClient, signer: str, key: str, counter: int) -> bool:
    """Sign the oldest queued rows at consecutive nonces from `counter` and
    queue their evm txs, all in one transaction: no row is ever signed
    without its tx. Returns whether it signed any."""
    settings = load_settings()
    now = int(time.time())
    signed = []
    with db_transaction(db):
        for row in _lane_rows(db, signer, QUEUED, SAPPHIRE_BATCH_SIZE):
            nonce = counter + len(signed)
            payload = json.loads(row["payload"])
            signature = sign_transfer(key, evm.chain_id, settings.accounting_contract_address,
                                      payload["to"], payload["token"], payload["amount"], nonce)
            to, data = _call(settings, row["kind"], payload, nonce, signature)
            evm_owner = f"privana:{row['id']}:{row['attempts'] + 1}"
            if not db.execute(
                "UPDATE privana_transactions SET status = ?, nonce = ?, signature = ?, evm_owner = ?, "
                "attempts = attempts + 1, updated_at = ? WHERE id = ? AND status = ?",
                (SENT, nonce, signature, evm_owner, now, row["id"], QUEUED),
            ).rowcount:
                continue
            # Only the first can be simulated: the rest need the ones before them landed.
            db.execute(
                "INSERT INTO evm_transactions (chain_id, address, owner, to_address, data, value, gas, "
                "simulate, created_at, updated_at) VALUES (?, ?, ?, ?, ?, '0', ?, ?, ?, ?)",
                (evm.chain_id, evm.address.lower(), evm_owner, to, data, GAS_LIMITS[row["kind"]],
                 int(not signed), now, now),
            )
            signed.append((row, nonce, evm_owner))
    for row, nonce, evm_owner in signed:
        logger.info("privana lane %s signed %s %d for %s at nonce %d as %s",
                    signer, row["kind"], row["id"], row["owner"], nonce, evm_owner)
    return bool(signed)


def _call(settings: Settings, kind: str, payload: dict, nonce: int, signature: str) -> tuple[str, str]:
    """The contract and calldata of a row signed at `nonce`."""
    to = Web3.to_checksum_address(payload["to"])
    token, amount, signature = HexBytes(payload["token"]), payload["amount"], HexBytes(signature)
    if kind == TRANSFER:
        data = _ACCOUNTING.encode_abi("transferBalance", args=[to, token, amount, nonce, signature])
        return settings.accounting_contract_address, data
    leg = payload["input"]
    data = _SWAP_MANAGER.encode_abi("swap", args=[
        to, HexBytes(leg["token"]), leg["amount"], leg["nonce"], HexBytes(leg["signature"]),
        token, amount, nonce, signature,
    ])
    return settings.swap_manager_contract_address, data


def _lane_rows(db: sqlite3.Connection, signer: str, status: str, limit: int = -1) -> list[dict]:
    order = "nonce" if status == SENT else "id"
    return [dict(r) for r in db.execute(
        "SELECT * FROM privana_transactions WHERE signer = ? AND kind != ? AND status = ? "
        f"ORDER BY {order} LIMIT ?",
        (signer, WITHDRAW, status, limit),
    )]


def _next_after_landed(db: sqlite3.Connection, signer: str, lane: str) -> int:
    """The nonce after the highest one `signer` landed on `lane`'s counter."""
    kinds = "kind = ?" if lane == WITHDRAW else "kind != ?"
    top = db.execute(
        f"SELECT MAX(nonce) FROM privana_transactions WHERE signer = ? AND status = ? AND {kinds}",
        (signer, LANDED, WITHDRAW),
    ).fetchone()[0]
    return 0 if top is None else top + 1


def _finish(db: sqlite3.Connection, row: dict, status: str, error: Optional[str] = None) -> None:
    db_write(db, "UPDATE privana_transactions SET status = ?, error = ?, updated_at = ? WHERE id = ?",
             (status, error, int(time.time()), row["id"]))
    log = logger.error if status == FAILED else logger.info
    log("privana lane %s %s %d for %s %s at nonce %s%s", row["signer"], row["kind"], row["id"], row["owner"],
        status, row["nonce"], f": {error}" if error else "")


def _transfer_keys() -> dict[str, str]:
    """Address -> secret key of each account whose transfer lane this service
    runs: the LP, which has a Sapphire sender."""
    key = load_settings().liquidity_provider_secret_key
    return {Account.from_key(key).address.lower(): key} if key else {}


class WithdrawLane:
    """The one writer of `signer`'s withdrawal nonce. The Privana API takes
    only the current nonce, so one withdrawal is in flight at a time. It is
    settled by the nonce on chain, never by the reply: replies get cut while
    the withdrawal still lands."""

    def __init__(self, signer: str, secret_key: str, client: Callable[[], Awaitable[PrivanaClient]]) -> None:
        self.signer = signer
        self._key = secret_key
        self._client = client

    async def run(self) -> None:
        while True:
            try:
                await self.run_round()
            except Exception as exc:
                logger.warning("privana withdraw lane %s round failed: %s", self.signer, exc)
            await asyncio.sleep(WITHDRAW_POLL_SEC)

    async def run_round(self) -> None:
        row = get_db().execute(
            "SELECT * FROM privana_transactions WHERE signer = ? AND kind = ? AND status IN (?, ?) "
            "ORDER BY status = ? DESC, id LIMIT 1",
            (self.signer, WITHDRAW, QUEUED, SENT, SENT),
        ).fetchone()
        if row is None:
            return
        row = dict(row)
        counter = await self._counter()
        if row["status"] == QUEUED:
            row = self._sign(row, counter)
        elif counter > row["nonce"]:
            _finish(get_db(), row, LANDED)
            return
        elif time.time() - row["updated_at"] < WITHDRAW_RESEND_SEC:
            return
        elif row["attempts"] >= WITHDRAW_MAX_SENDS:
            logger.error("privana withdraw lane %s withdrawal %d for %s not landed at nonce %d after %d sends; "
                         "manual recovery required", self.signer, row["id"], row["owner"], row["nonce"],
                         row["attempts"])
            db_write(get_db(), "UPDATE privana_transactions SET updated_at = ? WHERE id = ?",
                     (int(time.time()), row["id"]))
            return
        else:
            logger.warning("privana withdraw lane %s withdrawal %d for %s not landed at nonce %d; sending it again",
                           self.signer, row["id"], row["owner"], row["nonce"])
            db_write(get_db(), "UPDATE privana_transactions SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
                     (int(time.time()), row["id"]))
            row = {**row, "attempts": row["attempts"] + 1}
        await self._send(row)

    async def _counter(self) -> int:
        # A node behind the others can report a count below a nonce already
        # landed here: take the higher.
        read = await asyncio.to_thread(get_accounting_client().withdrawal_nonce, self.signer)
        return max(read, _next_after_landed(get_db(), self.signer, WITHDRAW))

    def _sign(self, row: dict, nonce: int) -> dict:
        """Saved before it is sent: a restart sends the same request again
        instead of signing a second one."""
        settings = load_settings()
        payload = json.loads(row["payload"])
        signature = sign_withdrawal(self._key, settings.accounting_chain_id, settings.accounting_contract_address,
                                    payload["token"], payload["amount"], nonce)
        db_write(
            get_db(),
            "UPDATE privana_transactions SET status = ?, nonce = ?, signature = ?, attempts = attempts + 1, "
            "updated_at = ? WHERE id = ?",
            (SENT, nonce, signature, int(time.time()), row["id"]),
        )
        logger.info("privana withdraw lane %s signed withdrawal %d for %s at nonce %d",
                    self.signer, row["id"], row["owner"], nonce)
        return {**row, "status": SENT, "nonce": nonce, "signature": signature, "attempts": row["attempts"] + 1}

    async def _send(self, row: dict) -> None:
        payload = json.loads(row["payload"])
        request = WithdrawalRequest(token_id=payload["token"], amount=payload["amount"], nonce=row["nonce"],
                                    signature=row["signature"])
        try:
            reply = await (await self._client()).request_withdrawal(request)
        except NetworkError as exc:
            logger.warning("privana withdraw lane %s withdrawal %d at nonce %d got no reply (%s); "
                           "the nonce on chain settles it", self.signer, row["id"], row["nonce"], exc)
            return
        except AccountingApiError as exc:
            if exc.status_code >= 500 or exc.status_code == API_CONFLICT:
                logger.warning("privana withdraw lane %s withdrawal %d at nonce %d: %s; "
                               "the nonce on chain settles it", self.signer, row["id"], row["nonce"], exc)
                return
            refusal = f"refused: {exc}"
        else:
            if reply.status in ACCEPTED_SUBMISSION_STATUSES:
                logger.info("privana withdraw lane %s withdrawal %d at nonce %d accepted as %s (%s)",
                            self.signer, row["id"], row["nonce"], reply.submission_id, reply.status)
                return
            refusal = f"refused as {reply.status}: {reply.detail}"
        # An earlier send of the same request may have landed meanwhile, or still land.
        if await self._counter() > row["nonce"]:
            _finish(get_db(), row, LANDED)
        elif row["attempts"] > 1:
            logger.warning("privana withdraw lane %s resend %d of withdrawal %d at nonce %d %s; an earlier send may "
                           "still land", self.signer, row["attempts"], row["id"], row["nonce"], refusal)
        else:
            _finish(get_db(), row, FAILED, error=refusal)


def lane_health() -> dict[str, int]:
    """Signer -> how long its oldest unsettled lane row has waited."""
    now = int(time.time())
    return {r["signer"]: now - r["since"] for r in get_db().execute(
        "SELECT signer, MIN(created_at) AS since FROM privana_transactions WHERE status IN (?, ?) GROUP BY signer",
        (QUEUED, SENT),
    )}


def _withdraw_lanes() -> dict[str, tuple[str, Callable[[], Awaitable[PrivanaClient]]]]:
    """Signer -> its secret key and a Privana client acting as it: the LP."""
    settings = load_settings()
    if not settings.liquidity_provider_secret_key:
        return {}
    return {settings.liquidity_provider_address.lower(): (settings.liquidity_provider_secret_key,
                                                          get_swap_lp_privana_client)}


_withdraw_tasks: list[asyncio.Task] = []


def start_withdraw_lanes() -> None:
    for signer, (key, client) in _withdraw_lanes().items():
        lane = WithdrawLane(signer, key, client)
        _withdraw_tasks.append(asyncio.create_task(lane.run(), name=f"privana-withdraw-{signer}"))


async def stop_withdraw_lanes() -> None:
    for task in _withdraw_tasks:
        task.cancel()
    await asyncio.gather(*_withdraw_tasks, return_exceptions=True)
    _withdraw_tasks.clear()
