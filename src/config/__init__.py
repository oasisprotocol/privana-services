import logging
import os
from typing import Optional

from src.models.types import Settings

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
            vault_evm_address=os.getenv("VAULT_EVM_ADDRESS", _defaults.vault_evm_address),
            service_address=os.getenv("SERVICE_ADDRESS", _defaults.service_address),
            quote_ttl=_get_int("QUOTE_TTL", _defaults.quote_ttl),
            fee_bps=_get_int("FEE_BPS", _defaults.fee_bps),
            swap_poll_interval=_get_int("SWAP_POLL_INTERVAL", _defaults.swap_poll_interval),
            same_chain_timeout=_get_int("SAME_CHAIN_TIMEOUT", _defaults.same_chain_timeout),
            cross_chain_timeout=_get_int("CROSS_CHAIN_TIMEOUT", _defaults.cross_chain_timeout),
        )
    return _settings


__all__ = ["load_settings"]
