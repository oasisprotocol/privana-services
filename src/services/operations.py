from __future__ import annotations

from src.core.db import get_db
from src.models.operations import UnsettledOperation

# "canceled" is part of the read contract even though current writers only
# produce pending, completed, and failed rows.
UNSETTLED_STATUSES = ("pending", "failed", "canceled")


def list_unsettled_operations(user_address: str, limit: int) -> list[UnsettledOperation]:
    db = get_db()
    params = (
        user_address.lower(),
        *UNSETTLED_STATUSES,
        user_address.lower(),
        *UNSETTLED_STATUSES,
        limit,
    )
    rows = db.execute(
        """
        SELECT * FROM (
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
                NULL AS amount
            FROM swaps
            WHERE user_address = ? AND status IN (?, ?, ?)

            UNION ALL

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
                amount
            FROM earn_transactions
            WHERE user_address = ? AND status IN (?, ?, ?)
        )
        ORDER BY updated_at DESC, created_at DESC, operation_id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [UnsettledOperation(**dict(row)) for row in rows]
