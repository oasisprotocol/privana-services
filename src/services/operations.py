from __future__ import annotations

import uuid
from typing import Optional

from src.core.db import get_db
from src.models.operations import UnsettledOperation

# "canceled" is part of the read contract even though current writers only
# produce pending, completed, failed, and undeployed rows. "undeployed" is
# unsettled by design: the shares exist but the funds still need an operator
# to redeploy them into the strategy.
UNSETTLED_STATUSES = ("pending", "scheduled", "executing", "refunding", "failed", "canceled", "undeployed")


_SWAP_SELECT = """
    SELECT
        id AS operation_id,
        'swap' AS operation_type,
        status,
        created_at,
        updated_at,
        swap_tx_hash AS tx_hash,
        error,
        quote_id,
        from_token_id,
        to_token_id,
        from_amount,
        to_amount_estimate,
        to_amount_actual,
        NULL AS pool_id,
        NULL AS token_id,
        NULL AS amount,
        input_nonce AS nonce,
        NULL AS stages
    FROM swaps
    WHERE user_address = ?"""

_EARN_SELECT = """
    SELECT
        id AS operation_id,
        'earn_' || operation AS operation_type,
        status,
        created_at,
        updated_at,
        tx_hash,
        error,
        NULL AS quote_id,
        NULL AS from_token_id,
        NULL AS to_token_id,
        NULL AS from_amount,
        NULL AS to_amount_estimate,
        NULL AS to_amount_actual,
        pool_id,
        token_id,
        amount,
        CAST(input_nonce AS TEXT) AS nonce,
        stages
    FROM earn_transactions
    WHERE user_address = ?"""


def _union(extra_where: str, order_by: str) -> str:
    # Placeholders are generated from module constants, never from input,
    # so the values stay bound.
    return (
        f"SELECT * FROM ({_SWAP_SELECT}{extra_where} UNION ALL {_EARN_SELECT}{extra_where}) "
        f"ORDER BY {order_by} LIMIT ?"
    )


def list_unsettled_operations(user_address: str, limit: int) -> list[UnsettledOperation]:
    status_placeholders = ", ".join("?" * len(UNSETTLED_STATUSES))
    user = user_address.lower()
    params = (user, *UNSETTLED_STATUSES, user, *UNSETTLED_STATUSES, limit)
    rows = (
        get_db()
        .execute(
            _union(
                f" AND status IN ({status_placeholders})",
                "updated_at DESC, created_at DESC, operation_id DESC",
            ),
            params,
        )
        .fetchall()
    )
    return [UnsettledOperation(**dict(row)) for row in rows]


# Cursor over (created_at, operation_id), newest first. Both are immutable once
# a row exists, so a page boundary stays put while rows settle underneath it.
def encode_cursor(created_at: int, operation_id: str) -> str:
    return f"{created_at}:{operation_id}"


def decode_cursor(cursor: str) -> tuple[int, str]:
    created_at, sep, operation_id = cursor.partition(":")
    if not sep or not created_at.isdigit():
        raise ValueError("Invalid cursor")
    try:
        # Operation ids are uuid4 on both tables; anything else is not a cursor we issued.
        uuid.UUID(operation_id)
    except ValueError as exc:
        raise ValueError("Invalid cursor") from exc
    return int(created_at), operation_id


def list_operations(
    user_address: str, limit: int, before: Optional[str] = None
) -> tuple[list[UnsettledOperation], Optional[str]]:
    """Every swap and earn operation of a user, in any status, newest first."""
    user = user_address.lower()
    if before is None:
        extra_where = ""
        page_params: tuple = (user,)
    else:
        created_at, operation_id = decode_cursor(before)
        extra_where = " AND (created_at < ? OR (created_at = ? AND id < ?))"
        page_params = (user, created_at, created_at, operation_id)
    rows = (
        get_db()
        .execute(
            _union(extra_where, "created_at DESC, operation_id DESC"),
            (*page_params, *page_params, limit),
        )
        .fetchall()
    )
    operations = [UnsettledOperation(**dict(row)) for row in rows]
    next_cursor = (
        encode_cursor(operations[-1].created_at, operations[-1].operation_id)
        if len(operations) == limit
        else None
    )
    return operations, next_cursor
