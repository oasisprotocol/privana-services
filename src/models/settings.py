from dataclasses import dataclass


@dataclass
class Settings:
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"
    environment: str = "development"

    accounting_api_base_url: str = "https://flexvaults-staging.rofl.build"

    lifi_api_key: str = ""
    lifi_api_url: str = "https://li.quest/v1"
    lifi_integrator: str = "flexvaults"

    liquidity_provider_secret_key: str = ""
    liquidity_provider_address: str = "0x0000000000000000000000000000000000000000"
    accounting_contract_address: str = "0x0000000000000000000000000000000000000000"
    accounting_chain_id: int = 23295
    swap_manager_contract_address: str = "0x0000000000000000000000000000000000000000"
    earn_manager_contract_address: str = "0x0000000000000000000000000000000000000000"
    sapphire_rpc_url: str = "https://testnet.sapphire.oasis.io"

    quote_ttl: int = 60
    fee_bps: int = 10
    max_swap_amount_usd: int = 100_000
    lifi_token_map: str = ""

    admin_api_key: str = ""

    base_sepolia_rpc_url: str = "https://sepolia.base.org"
    aave_pool_address: str = "0x8bAB6d1b75f19e9eD9fCe8b9BD338844fF79aE27"
    aave_pool_assets: str = ""
