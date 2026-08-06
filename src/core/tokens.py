import json
import logging
import os

logger = logging.getLogger(__name__)


def get_supported_token_ids() -> list[str]:
    raw = os.getenv("SUPPORTED_TOKEN_IDS", "")
    if not raw.strip():
        return []
    return [t.strip().lower() for t in raw.split(",") if t.strip()]


def get_supported_chains() -> list[dict]:
    """Chains this deployment advertises, straight from ``SUPPORTED_CHAINS``.

    Missing or malformed config yields an empty list, not a default. The
    fallback here used to be a hardcoded Base Sepolia, which on a mainnet
    deploy would answer /chains with a testnet chain — a wrong answer that
    looks like a working one. Serving nothing is the visible failure.
    """
    raw = os.getenv("SUPPORTED_CHAINS", "")
    if not raw.strip():
        logger.error("SUPPORTED_CHAINS is not set; no chains will be advertised")
        return []

    try:
        chains = json.loads(raw)
        return [{"chain_id": c["chain_id"], "name": c["name"]} for c in chains]
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.exception("Invalid SUPPORTED_CHAINS; no chains will be advertised")
        return []
