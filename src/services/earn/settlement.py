"""Share metadata derived from public, block-pinned EarnManager state."""

import logging
from decimal import Decimal

from src.services.earn.vault_service import EARN_OP_DEPOSIT

logger = logging.getLogger(__name__)


def read_share_settlement(contract, pool_id: bytes, block: int, operation: str, amount: str) -> dict:
    """Read share metadata from the receipt block and the preceding block.

    RPC failures propagate and can be retried. An asset change inconsistent
    with this cashflow leaves its shares unknown.
    """
    after = contract.functions.pools(pool_id).call(block_identifier=block)
    before = contract.functions.pools(pool_id).call(block_identifier=block - 1)
    expected = int(amount) if operation == EARN_OP_DEPOSIT else -int(amount)
    delta: int | None = int(after[2]) - int(before[2])
    if int(after[3]) - int(before[3]) != expected:
        logger.warning("Earn shares in block %d not attributable to this cashflow", block)
        delta = None
    return {
        "shares_delta": None if delta is None else str(delta),
        "exchange_rate": str(Decimal(int(amount)) / Decimal(abs(delta))) if delta else None,
    }
