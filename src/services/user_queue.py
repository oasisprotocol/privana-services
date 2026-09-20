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
