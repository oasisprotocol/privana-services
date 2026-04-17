import logging
import os
from typing import Optional

from dotenv import load_dotenv

from src.models.settings import Settings

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

_settings: Optional[Settings] = None
_defaults = Settings()


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value, 0)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def load_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        _settings = Settings(
            api_host=os.getenv("API_HOST", _defaults.api_host),
            api_port=_get_int("API_PORT", _defaults.api_port),
            log_level=os.getenv("LOG_LEVEL", _defaults.log_level),
            environment=os.getenv("ENVIRONMENT", _defaults.environment),
            accounting_api_base_url=os.getenv(
                "ACCOUNTING_API_BASE_URL", _defaults.accounting_api_base_url
            ),
            lifi_api_key=os.getenv("LIFI_API_KEY", _defaults.lifi_api_key),
            lifi_api_url=os.getenv("LIFI_API_URL", _defaults.lifi_api_url),
            lifi_integrator=os.getenv("LIFI_INTEGRATOR", _defaults.lifi_integrator),
            liquidity_provider_private_key=os.getenv(
                "LIQUIDITY_PROVIDER_PRIVATE_KEY", _defaults.liquidity_provider_private_key
            ),
            liquidity_provider_address=os.getenv(
                "LIQUIDITY_PROVIDER_ADDRESS", _defaults.liquidity_provider_address
            ),
            accounting_contract_address=os.getenv(
                "ACCOUNTING_CONTRACT_ADDRESS", _defaults.accounting_contract_address
            ),
            accounting_chain_id=_get_int(
                "ACCOUNTING_CHAIN_ID", _defaults.accounting_chain_id
            ),
            swap_manager_contract_address=os.getenv(
                "SWAP_MANAGER_CONTRACT_ADDRESS", _defaults.swap_manager_contract_address
            ),
            earn_manager_contract_address=os.getenv(
                "EARN_MANAGER_CONTRACT_ADDRESS", _defaults.earn_manager_contract_address
            ),
            sapphire_rpc_url=os.getenv("SAPPHIRE_RPC_URL", _defaults.sapphire_rpc_url),
            quote_ttl=_get_int("QUOTE_TTL", _defaults.quote_ttl),
            fee_bps=_get_int("FEE_BPS", _defaults.fee_bps),
            max_swap_amount_usd=_get_int("MAX_SWAP_AMOUNT_USD", _defaults.max_swap_amount_usd),
            lifi_token_map=os.getenv("LIFI_TOKEN_MAP", _defaults.lifi_token_map),
        )
    return _settings


__all__ = ["load_settings"]
