import json
import logging
import os

logger = logging.getLogger(__name__)

_CHAIN_REGISTRY: dict[int, str] = {
    1: "Ethereum",
    10: "Optimism",
    56: "BNB Chain",
    137: "Polygon",
    8453: "Base",
    42161: "Arbitrum One",
    43114: "Avalanche",
    11155111: "Sepolia",
    84532: "Base Sepolia",
    421614: "Arbitrum Sepolia",
}


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
