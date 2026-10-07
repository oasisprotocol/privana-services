"""What a user is told about a swap that did not go as planned.

The stored error is for operators: it carries provider errors, tx hashes and
internal step names, and stays in the database and the logs. Users get a
short reason code and a sentence that says what it means for their funds.
"""
from typing import Optional

PRICE_MOVED = "price_moved"
NO_FUNDS_MOVED = "no_funds_moved"
DELAYED = "delayed"
UNDER_REVIEW = "under_review"

_MESSAGES = {
    PRICE_MOVED: "The price moved too far before the swap went through.",
    NO_FUNDS_MOVED: "The swap did not go through. No funds were moved.",
    DELAYED: "The swap is taking longer than usual.",
    UNDER_REVIEW: "This swap needs a manual check by our team.",
}
_REFUNDED_MESSAGE = "The swap did not go through, and your funds were returned."

_IN_PROGRESS = ("scheduled", "executing", "refunding")
_PRICE_SIGNS = ("below floor", "slippage", "return amount is not enough", "insufficient output")
# The input may have reached the LP without the swap knowing, so nothing can
# be said about the funds until someone looks.
_OUTCOME_UNKNOWN = ("manual recovery required", "not confirmed on ledger")


def swap_failure(status: str, error: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """The reason code and user message for a swap, or (None, None) when there
    is nothing to explain."""
    if not error and status != "refunded":
        return None, None
    text = (error or "").lower()
    if any(sign in text for sign in _OUTCOME_UNKNOWN):
        reason = UNDER_REVIEW
    elif status in _IN_PROGRESS:
        reason = DELAYED
    elif any(sign in text for sign in _PRICE_SIGNS):
        reason = PRICE_MOVED
    else:
        reason = NO_FUNDS_MOVED
    if status == "refunded" and reason != UNDER_REVIEW:
        return reason, _REFUNDED_MESSAGE
    return reason, _MESSAGES[reason]
