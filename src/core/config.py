import logging
import os
from typing import Optional

from dotenv import load_dotenv
from eth_account import Account

from src.models.settings import Settings

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

_settings: Optional[Settings] = None


def _get_int(name: str) -> int:
    value = os.getenv(name)
    if value is None:
        raise ValueError(f"Environment variable {name} is required")
    try:
        return int(value, 0)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def _get_base_rpc_url() -> Optional[str]:
    """Resolve the RPC for the Base chain this deployment operates on.

    The setting used to be called ``BASE_SEPOLIA_RPC_URL`` back when that chain
    was the only Base we ran against. It is really "our Base chain" — Sepolia on
    testnet, mainnet on mainnet — and reading a mainnet Aave pool through a
    variable named "sepolia" is how a deploy ends up querying the wrong chain
    and seeing every reserve as unlisted. ``BASE_RPC_URL`` is the name now.

    The old name is still honored so existing deployments don't break on the
    rename, with a warning so it doesn't stay that way forever. Distinct from
    ``BASE_MAINNET_RPC_URL``, which is always Base mainnet because Midas only
    exists there; on a mainnet deploy the two legitimately point at the same
    endpoint.
    """
    url = os.getenv("BASE_RPC_URL")
    if url:
        return url

    legacy = os.getenv("BASE_SEPOLIA_RPC_URL")
    if legacy:
        logger.warning(
            "BASE_SEPOLIA_RPC_URL is deprecated; rename it to BASE_RPC_URL. "
            "It names the Base chain this deployment operates on, which is not "
            "Sepolia outside testnet."
        )
    return legacy


def load_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        lp_secret_key = os.getenv("LIQUIDITY_PROVIDER_SECRET_KEY")
        lp_address = Account.from_key(lp_secret_key).address if lp_secret_key else ""
        _settings = Settings(
            api_host=os.getenv("API_HOST"),
            api_port=_get_int("API_PORT"),
            log_level=os.getenv("LOG_LEVEL"),
            environment=os.getenv("ENVIRONMENT"),
            privana_api_base_url=os.getenv("PRIVANA_API_BASE_URL"),
            lifi_api_key=os.getenv("LIFI_API_KEY"),
            lifi_api_url=os.getenv("LIFI_API_URL"),
            lifi_integrator=os.getenv("LIFI_INTEGRATOR"),
            liquidity_provider_secret_key=lp_secret_key,
            liquidity_provider_address=lp_address,
            accounting_contract_address=os.getenv("ACCOUNTING_CONTRACT_ADDRESS"),
            accounting_chain_id=_get_int("ACCOUNTING_CHAIN_ID"),
            swap_manager_contract_address=os.getenv("SWAP_MANAGER_CONTRACT_ADDRESS"),
            earn_manager_contract_address=os.getenv("EARN_MANAGER_CONTRACT_ADDRESS"),
            sapphire_rpc_url=os.getenv("SAPPHIRE_RPC_URL"),
            quote_ttl=_get_int("QUOTE_TTL"),
            fee_bps=_get_int("FEE_BPS"),
            fee_policies_json=os.getenv("FEE_POLICIES_JSON", ""),
            max_swap_amount_usd=_get_int("MAX_SWAP_AMOUNT_USD"),
            lifi_token_map=os.getenv("LIFI_TOKEN_MAP"),
            base_rpc_url=_get_base_rpc_url(),
            base_mainnet_rpc_url=os.getenv("BASE_MAINNET_RPC_URL"),
            aave_pool_address=os.getenv("AAVE_POOL_ADDRESS"),
            aave_pool_assets=os.getenv("AAVE_POOL_ASSETS"),
            midas_issuance_vault_address=os.getenv("MIDAS_ISSUANCE_VAULT_ADDRESS"),
            midas_redemption_vault_address=os.getenv("MIDAS_REDEMPTION_VAULT_ADDRESS"),
            midas_mtbill_token_address=os.getenv("MIDAS_MTBILL_TOKEN_ADDRESS"),
            midas_oracle_address=os.getenv("MIDAS_ORACLE_ADDRESS"),
            midas_default_slippage_bps=_get_int("MIDAS_DEFAULT_SLIPPAGE_BPS"),
            midas_oracle_heartbeat_sec=_get_int("MIDAS_ORACLE_HEARTBEAT_SEC"),
            midas_apy_bps=_get_int("MIDAS_APY_BPS"),
            midas_pool_assets=os.getenv("MIDAS_POOL_ASSETS"),
            defillama_pool_ids=os.getenv("DEFILLAMA_POOL_IDS", ""),
            coingecko_token_ids=os.getenv("COINGECKO_TOKEN_IDS", ""),
            lifi_execution_enabled=os.getenv("LIFI_EXECUTION_ENABLED", "false").lower() == "true",
            lifi_max_swap_amount_usd=int(os.getenv("LIFI_MAX_SWAP_AMOUNT_USD", "0")),
        )
    return _settings


__all__ = ["load_settings"]
