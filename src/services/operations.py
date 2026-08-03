from __future__ import annotations

from src.core.db import get_db
from src.models.operations import UnsettledOperation

# "canceled" is part of the read contract even though current writers only
# produce pending, completed, failed, and undeployed rows. "undeployed" is
# unsettled by design: the shares exist but the funds still need an operator
# to redeploy them into the strategy.
UNSETTLED_STATUSES = ("pending", "failed", "canceled", "undeployed")


def list_unsettled_operations(user_address: str, limit: int) -> list[UnsettledOperation]:
    db = get_db()
    params = (
        user_address.lower(),
        *UNSETTLED_STATUSES,
        user_address.lower(),
        *UNSETTLED_STATUSES,
        limit,
    )
    # Placeholders are generated from the module constant, never from input,
    # so the values stay bound.
    status_placeholders = ", ".join("?" * len(UNSETTLED_STATUSES))
    rows = db.execute(
        f"""
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
            WHERE user_address = ? AND status IN ({status_placeholders})

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
            WHERE user_address = ? AND status IN ({status_placeholders})
        )
        ORDER BY updated_at DESC, created_at DESC, operation_id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [UnsettledOperation(**dict(row)) for row in rows]
