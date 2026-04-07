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
    raw = os.getenv("SUPPORTED_CHAINS", "")
    if raw.strip():
        try:
            chains = json.loads(raw)
            return [{"chain_id": c["chain_id"], "name": c["name"]} for c in chains]
        except (json.JSONDecodeError, KeyError):
            logger.warning("Invalid SUPPORTED_CHAINS JSON, falling back to defaults")

    token_ids = get_supported_token_ids()
    if not token_ids:
        return [{"chain_id": 84532, "name": "Base Sepolia"}]

    return [{"chain_id": 84532, "name": "Base Sepolia"}]
