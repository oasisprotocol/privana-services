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
_defaults = Settings()


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value, 0)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc


def _read_lp_secret_key(default: str) -> str:
    """Read the LP secret key, accepting the legacy ``..._PRIVATE_KEY`` env
    name with a deprecation warning.

    Naming follows cryptographic convention: ``sk`` = secret, ``pk`` = public.
    The old ``LIQUIDITY_PROVIDER_PRIVATE_KEY`` is ambiguous because ``pk`` can
    mean either, so the canonical env is ``LIQUIDITY_PROVIDER_SECRET_KEY``.
    Both are accepted today to avoid breaking deployments mid-migration.
    """
    sk = os.getenv("LIQUIDITY_PROVIDER_SECRET_KEY")
    legacy = os.getenv("LIQUIDITY_PROVIDER_PRIVATE_KEY")
    if sk and legacy and sk != legacy:
        raise RuntimeError(
            "LIQUIDITY_PROVIDER_SECRET_KEY and LIQUIDITY_PROVIDER_PRIVATE_KEY are "
            "both set with different values. Keep only LIQUIDITY_PROVIDER_SECRET_KEY."
        )
    if sk:
        return sk
    if legacy:
        logger.warning(
            "LIQUIDITY_PROVIDER_PRIVATE_KEY is deprecated; rename to "
            "LIQUIDITY_PROVIDER_SECRET_KEY (sk = secret key, pk = public key)."
        )
        return legacy
    return default


def _derive_lp_address(secret_key: str, default: str) -> str:
    """Single source of truth for the LP address.

    When a secret key is configured, derive the address from it so the
    signing identity and the on-ledger pool/LP identity can never drift.
    A legacy ``LIQUIDITY_PROVIDER_ADDRESS`` env var is still honored as a
    sanity guard: if set, it must match the derived address or startup
    fails fast (same pattern as the chain-id check).
    """
    if not secret_key:
        return os.getenv("LIQUIDITY_PROVIDER_ADDRESS", default)
    derived = Account.from_key(secret_key).address
    legacy = os.getenv("LIQUIDITY_PROVIDER_ADDRESS")
    if legacy and legacy.lower() != derived.lower():
        raise RuntimeError(
            "LIQUIDITY_PROVIDER_ADDRESS does not match LIQUIDITY_PROVIDER_SECRET_KEY: "
            f"env={legacy} derived={derived}. Drop LIQUIDITY_PROVIDER_ADDRESS from your "
            "env, the address is now derived from the key."
        )
    if legacy:
        logger.warning(
            "LIQUIDITY_PROVIDER_ADDRESS is deprecated and now derived from the secret key. "
            "You can remove it from your env."
        )
    return derived


def load_settings(refresh: bool = False) -> Settings:
    global _settings
    if _settings is None or refresh:
        secret_key = _read_lp_secret_key(_defaults.liquidity_provider_secret_key)
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
            liquidity_provider_secret_key=secret_key,
            liquidity_provider_address=_derive_lp_address(
                secret_key, _defaults.liquidity_provider_address
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
            admin_api_key=os.getenv("ADMIN_API_KEY", _defaults.admin_api_key),
            base_sepolia_rpc_url=os.getenv("BASE_SEPOLIA_RPC_URL", _defaults.base_sepolia_rpc_url),
            aave_pool_address=os.getenv("AAVE_POOL_ADDRESS", _defaults.aave_pool_address),
            aave_pool_assets=os.getenv("AAVE_POOL_ASSETS", _defaults.aave_pool_assets),
            base_mainnet_rpc_url=os.getenv(
                "BASE_MAINNET_RPC_URL", _defaults.base_mainnet_rpc_url
            ),
            midas_issuance_vault_address=os.getenv(
                "MIDAS_ISSUANCE_VAULT_ADDRESS", _defaults.midas_issuance_vault_address
            ),
            midas_redemption_vault_address=os.getenv(
                "MIDAS_REDEMPTION_VAULT_ADDRESS", _defaults.midas_redemption_vault_address
            ),
            midas_mtbill_token_address=os.getenv(
                "MIDAS_MTBILL_TOKEN_ADDRESS", _defaults.midas_mtbill_token_address
            ),
            midas_oracle_address=os.getenv(
                "MIDAS_ORACLE_ADDRESS", _defaults.midas_oracle_address
            ),
            midas_default_slippage_bps=_get_int(
                "MIDAS_DEFAULT_SLIPPAGE_BPS", _defaults.midas_default_slippage_bps
            ),
            midas_oracle_heartbeat_sec=_get_int(
                "MIDAS_ORACLE_HEARTBEAT_SEC", _defaults.midas_oracle_heartbeat_sec
            ),
            midas_pool_assets=os.getenv("MIDAS_POOL_ASSETS", _defaults.midas_pool_assets),
        )
    return _settings


__all__ = ["load_settings"]
