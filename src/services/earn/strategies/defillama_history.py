from __future__ import annotations

import logging
import time
from typing import Optional

from src.clients.defillama import DefiLlamaClient, get_defillama_client
from src.services.earn.strategies.base import ApyPoint

logger = logging.getLogger(__name__)


async def defillama_apy_history(
    pool_id: Optional[str],
    client: Optional[DefiLlamaClient],
    days: Optional[int],
    *,
    log_label: str,
) -> list[ApyPoint]:
    if not pool_id:
        return []

    client = client or get_defillama_client()
    try:
        raw = await client.get_pool_chart(pool_id)
    except Exception:
        logger.exception(
            "%s: DefiLlama chart failed pool=%s; serving no history",
            log_label, pool_id,
        )
        return []

    cutoff = 0
    if days is not None:
        cutoff = int(time.time()) - days * 86400

    return [
        ApyPoint(timestamp=p.timestamp, apy_bps=p.apy_bps)
        for p in raw
        if p.timestamp >= cutoff
    ]
