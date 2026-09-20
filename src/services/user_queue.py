"""Cross-queue ordering for one user's accounting nonce.

A swap and an earn deposit both spend the same per-user transfer nonce, and
the two queues claim independently. Without a shared view, an operation signed
with nonce N and one signed with N+1 can be submitted in the opposite order,
and the later nonce then fails for good. Each worker asks here before it
claims.
"""
from src.core.db import get_db

# Statuses that mean the operation has been taken off the queue and may already
# have spent the user's nonce.
_IN_FLIGHT = ("executing", "pending")


def users_with_inflight_work() -> set[str]:
    db = get_db()
    placeholders = ", ".join("?" * len(_IN_FLIGHT))
    rows = db.execute(
        f"SELECT user_address FROM swaps WHERE status IN ({placeholders}) "
        f"UNION SELECT user_address FROM earn_transactions WHERE status IN ({placeholders})",
        (*_IN_FLIGHT, *_IN_FLIGHT),
    ).fetchall()
    return {row["user_address"].lower() for row in rows if row["user_address"]}
