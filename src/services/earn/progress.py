"""Stages an earn operation passes through while the worker executes it.

The queue row only knows scheduled, executing and a terminal status, which
leaves a user staring at "pending" for the ten minutes a Midas exit spends on
Ethereum finality. Each step the service or a strategy takes reports itself
here, and the row keeps the timeline so the operations feed can show it.

Reporting is bound to the operation being executed through a context variable,
so strategies stay unaware of which row they serve, and work that runs outside
the worker (the idle sweep) reports nothing. Status changes land in the same
timeline via ``record_status``, so the row's whole life is in one column, and
every change is logged because the DB itself stays private.
"""
from __future__ import annotations

import json
import logging
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from src.core.db import db_write, get_db

logger = logging.getLogger(__name__)

RECORDING = "recording"
BRIDGING = "bridging"
DEPLOYING = "deploying"
RECLAIMING = "reclaiming"
RETURNING = "returning"
FINALITY = "finality"
PAYING_OUT = "paying_out"
AWAITING_LIQUIDITY = "awaiting_liquidity"

_current: ContextVar[Optional[str]] = ContextVar("earn_operation", default=None)

_FINALITY = re.compile(r"finality:\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)


@contextmanager
def tracking(tx_id: str) -> Iterator[None]:
    token = _current.set(tx_id)
    try:
        yield
    finally:
        _current.reset(token)


def update(stage: str, detail: Optional[dict] = None) -> None:
    """Record that the current operation reached `stage`.

    A repeated stage only refreshes its detail, so a finality poll updates one
    entry instead of adding one per check. Bookkeeping only: a failure here
    must never fail the operation it describes.
    """
    tx_id = _current.get()
    if tx_id is None:
        return
    if _append(tx_id, stage, detail):
        logger.info("earn %s reached stage %s", tx_id, stage)


def record_status(tx_id: str, old: Optional[str], new: str) -> None:
    """A status change belongs in the same timeline as the stages, and in the
    log: the DB is not readable in production, so the log line is the only
    trace a debugger gets."""
    logger.info("earn %s status changed from %s to %s", tx_id, old, new)
    _append(tx_id, "status", {"from": old, "to": new})


def _append(tx_id: str, stage: str, detail: Optional[dict]) -> bool:
    try:
        row = get_db().execute(
            "SELECT history FROM earn_transactions WHERE id = ?", (tx_id,)
        ).fetchone()
        if row is None:
            return False
        entries = json.loads(row["history"]) if row["history"] else []
        if entries and entries[-1]["stage"] == stage and stage != "status":
            if detail is None or entries[-1].get("detail") == detail:
                return False
            entries[-1]["detail"] = detail
        else:
            entries.append({"stage": stage, "at": int(time.time()), "detail": detail})
        db_write(
            get_db(),
            "UPDATE earn_transactions SET history = ? WHERE id = ?",
            (json.dumps(entries), tx_id),
        )
        return True
    except Exception:
        logger.warning("Could not record %s for %s", stage, tx_id, exc_info=True)
        return False


def update_finality(error: object) -> None:
    """Accounting refuses to credit a transfer until it has enough
    confirmations and says how many it has in the refusal. Surface that count
    as the finality stage's progress."""
    match = _FINALITY.search(str(error))
    if match:
        update(
            FINALITY,
            {"confirmations": int(match.group(1)), "required": int(match.group(2))},
        )
