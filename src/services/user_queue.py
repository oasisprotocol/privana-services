"""Cross-queue ordering for one user's accounting nonce.

A swap and an earn deposit both spend the same per-user transfer nonce, and
the two queues claim independently. Without a shared view, an operation signed
with nonce N and one signed with N+1 can be submitted in the opposite order,
and the later nonce then fails for good. Each worker asks here before it
claims.
"""
from src.core.db import get_db

# Taken off the queue and possibly already holding the user's nonce. Scheduled
# rows are not here: nothing has been signed against a nonce yet. The two
# tables do not share a vocabulary — swaps dropped 'pending' when they moved
# to the queue, earn still uses it for a row mid-execution.
_SWAP_IN_FLIGHT = ("executing",)
_EARN_IN_FLIGHT = ("executing", "pending")


def users_with_inflight_work() -> set[str]:
    db = get_db()
    rows = db.execute(
        f"SELECT user_address FROM swaps "
        f"WHERE status IN ({', '.join('?' * len(_SWAP_IN_FLIGHT))}) "
        f"UNION SELECT user_address FROM earn_transactions "
        f"WHERE status IN ({', '.join('?' * len(_EARN_IN_FLIGHT))})",
        (*_SWAP_IN_FLIGHT, *_EARN_IN_FLIGHT),
    ).fetchall()
    return {row["user_address"].lower() for row in rows if row["user_address"]}

# A request holds its input nonce from the moment it is queued until it lands.
_SWAP_HOLDS_NONCE = ("scheduled", *_SWAP_IN_FLIGHT)
_EARN_HOLDS_NONCE = ("scheduled", *_EARN_IN_FLIGHT)


class OperationPendingError(Exception):
    """Raised when a queued request still holds the submitted nonce."""

    def __init__(self, operation_type: str, operation_id: str) -> None:
        super().__init__(
            "A previous operation is still pending; wait for it to finish before submitting another"
        )
        self.operation_type = operation_type
        self.operation_id = operation_id

    def payload(self) -> dict:
        return {
            "detail": str(self),
            "pending_operation_type": self.operation_type,
            "pending_operation_id": self.operation_id,
        }


def assert_nonce_free(user_address: str, input_nonce: int) -> None:
    """Refuse a request whose transfer nonce a queued swap or earn deposit already holds."""
    user = user_address.lower()
    # Bound as text on both sides: swaps.input_nonce is TEXT, and a uint256 does
    # not fit SQLite's 64-bit integer binding. earn_transactions.input_nonce is
    # INTEGER and still matches through SQLite's column affinity conversion.
    nonce = str(input_nonce)
    row = get_db().execute(
        f"SELECT 'swap' AS kind, id FROM swaps "
        f"WHERE user_address = ? AND input_nonce = ? "
        f"AND status IN ({', '.join('?' * len(_SWAP_HOLDS_NONCE))}) "
        f"UNION ALL "
        f"SELECT 'earn_deposit' AS kind, id FROM earn_transactions "
        f"WHERE user_address = ? AND operation = 'deposit' AND input_nonce = ? "
        f"AND status IN ({', '.join('?' * len(_EARN_HOLDS_NONCE))}) "
        f"LIMIT 1",
        (user, nonce, *_SWAP_HOLDS_NONCE, user, nonce, *_EARN_HOLDS_NONCE),
    ).fetchone()
    if row is not None:
        raise OperationPendingError(row["kind"], row["id"])
