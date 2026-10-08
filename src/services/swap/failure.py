"""Swap failures: the full story for the logs, a short one for the user.

What a user is told comes from the swap's state, never from the wording of a
provider's error. The logs get everything: the provider's error as raised
and the swap's state at that moment, so a failure can be traced from them.
"""
import json
import logging
import re
import traceback
from typing import Optional

from src.core.db import get_db

logger = logging.getLogger(__name__)

REFUNDED = "refunded"
NO_FUNDS_MOVED = "no_funds_moved"
NEEDS_SUPPORT = "needs_support"

# Steps after which the user's input has reached the LP: a LiFi swap that
# fails from here on holds their funds until a refund or a person returns them.
_FUNDS_HELD_STEPS = {"input_transfer", "withdraw", "lifi_execute", "deposit", "credit"}

_STATE_FIELDS = (
    "status", "venue", "step", "quote_id", "user_address", "from_token_id", "to_token_id",
    "from_amount", "to_amount_estimate", "to_amount_actual", "swap_tx_hash",
    "lifi_tx_hash", "deposit_tx_hash", "withdrawal_index", "error",
)
# Provider URLs can carry an API key in their path or query; the host is enough to trace.
_URL = re.compile(r"(https?://[^/\s?#]+)[^\s\"']*")


def swap_failure(swap_id: str, status: str, venue: Optional[str], step: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """The reason code and user message for a swap that did not complete."""
    if status == "refunded":
        return REFUNDED, "This swap did not go through, and your funds were returned."
    if status != "failed":
        return None, None
    if venue == "lifi" and step in _FUNDS_HELD_STEPS:
        return NEEDS_SUPPORT, (
            "This swap could not be completed and your funds were not returned automatically. "
            f"Contact Privana support with swap ID {swap_id} to get them back."
        )
    return NO_FUNDS_MOVED, "This swap did not go through. No funds were moved."


def log_swap_failure(swap_id: str, event: str, exc: Optional[BaseException] = None) -> None:
    """Log a failure with the provider's error and traceback as raised, and the swap's state."""
    row = get_db().execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
    state = {k: row[k] for k in _STATE_FIELDS if row is not None and k in row.keys()}
    error = "".join(traceback.format_exception(exc)) if exc is not None else "no exception"
    logger.error(
        "swap %s %s | state=%s\n%s", swap_id, event, json.dumps(state, default=str),
        _URL.sub(r"\1/…", error),
    )
