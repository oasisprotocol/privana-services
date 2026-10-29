"""The swap rows every stage reads and moves on. A swap is at one step, and
only that stage's worker moves it, so nothing locks. Every write is found by
its owner, so a restart sends nothing twice and loses nothing."""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from hexbytes import HexBytes
from web3 import Web3

from src.core.abi import load_abi
from src.core.config import load_settings
from src.core.db import db_write, get_db
from src.core.validation import scrub_urls
from src.models.swap import SwapStatus, SwapStep, SwapVenue
from src.services.swap.failure import log_swap_failure
from src.services.user_queue import users_with_inflight_work

logger = logging.getLogger(__name__)

# The most swaps a stage handles in one round, oldest first: the target
# load is 100 concurrent swaps.
ROUND_LIMIT = 100
_ACCOUNTING = Web3().eth.contract(abi=load_abi("Accounting"))


def owner(swap_id: str, kind: str) -> str:
    """The batcher or lane owner of a swap's `kind` of write."""
    return f"swap:{swap_id}:{kind}"


def input_transfer_call(swap: dict) -> dict:
    """`Accounting.transferBalance` of the user's signed input to the LP."""
    settings = load_settings()
    data = _ACCOUNTING.encode_abi("transferBalance", args=[
        Web3.to_checksum_address(settings.liquidity_provider_address), HexBytes(swap["from_token_id"]),
        int(swap["from_amount"]), int(swap["input_nonce"]), HexBytes(swap["input_signature"]),
    ])
    return {"to": settings.accounting_contract_address, "data": data, "value": 0}


def claim(venue: SwapVenue, step: SwapStep, limit: int) -> None:
    """Move up to `limit` of the oldest scheduled swaps of `venue` to `step`.
    A user's swaps and earn deposits spend one transfer nonce, so a swap is
    taken only when it is its user's oldest and nothing of theirs is in
    flight."""
    if limit <= 0:
        return
    users = users_with_inflight_work()
    rows = get_db().execute(
        "SELECT * FROM swaps WHERE status = ? ORDER BY created_at, rowid LIMIT ?",
        (SwapStatus.SCHEDULED.value, ROUND_LIMIT),
    ).fetchall()
    for swap in map(dict, rows):
        user = swap["user_address"].lower()
        if user in users:
            continue
        users.add(user)
        if swap["venue"] != venue.value:
            continue
        advance(swap, step, SwapStatus.EXECUTING)
        limit -= 1
        if not limit:
            return


def swaps_at(*steps: SwapStep, limit: int = ROUND_LIMIT) -> list[dict]:
    """The oldest swaps at `steps`; every one of them when `limit` is -1."""
    placeholders = ", ".join("?" * len(steps))
    return [dict(row) for row in get_db().execute(
        f"SELECT * FROM swaps WHERE status IN (?, ?) AND step IN ({placeholders}) ORDER BY created_at LIMIT ?",
        (SwapStatus.EXECUTING.value, SwapStatus.REFUNDING.value, *(s.value for s in steps), limit),
    )]


async def handle_each(swaps: list[dict], handle: Callable[[dict], Awaitable[None]]) -> None:
    """Handle the swaps concurrently, so a chain whose RPC hangs holds only
    its own swaps for the round."""
    async def one(swap: dict) -> None:
        try:
            await handle(swap)
        except Exception as exc:
            # An RPC or API error: the next round tries again.
            logger.warning("swap %s at %s not handled this round: %s", swap["id"], swap["step"],
                           scrub_urls(repr(exc)))

    await asyncio.gather(*(one(swap) for swap in swaps))


def waited(swap: dict, seconds: int) -> bool:
    return int(time.time()) - swap["updated_at"] >= seconds


def advance(swap: dict, step: SwapStep, status: Optional[SwapStatus] = None, **fields) -> None:
    status = status or SwapStatus(swap["status"])
    _update(swap["id"], status=status.value, step=step.value, **fields)
    logger.info("swap %s %s %s -> %s %s", swap["id"], swap["status"], swap["step"], status.value, step.value)


def finish(swap: dict, status: SwapStatus, **fields) -> None:
    """Completed or refunded: the step stays the last one it was at."""
    _update(swap["id"], status=status.value, **fields)
    logger.info("swap %s %s at %s", swap["id"], status.value, swap["step"])


def fail(swap: dict, outcome: SwapStep, error: str, exc: Optional[BaseException] = None) -> None:
    """Fail the swap for good. PARKED when the user's funds may have moved and
    a person has to settle it; NO_FUNDS_MOVED only when they provably did not."""
    log_swap_failure(swap["id"], f"failed ({outcome.value}) at {swap['step'] or swap['status']}: {error}", exc)
    if outcome == SwapStep.PARKED:
        error = f"{error}; manual recovery required"
    _update(swap["id"], status=SwapStatus.FAILED.value, step=outcome.value, error=error)


def _update(swap_id: str, **fields) -> None:
    fields["updated_at"] = int(time.time())
    assignments = ", ".join(f"{key} = ?" for key in fields)
    db_write(get_db(), f"UPDATE swaps SET {assignments} WHERE id = ?", (*fields.values(), swap_id))
