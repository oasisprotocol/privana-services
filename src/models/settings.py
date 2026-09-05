from dataclasses import dataclass


@dataclass
class Settings:
    api_host: str
    api_port: int
    log_level: str
    environment: str

    privana_api_base_url: str

    lifi_api_key: str
    lifi_api_url: str
    lifi_integrator: str

    liquidity_provider_secret_key: str
    liquidity_provider_address: str
    accounting_contract_address: str
    accounting_chain_id: int
    swap_manager_contract_address: str
    earn_manager_contract_address: str
    sapphire_rpc_url: str

    quote_ttl: int
    fee_bps: int
    fee_policies_json: str
    max_swap_amount_usd: int
    lifi_token_map: str

    base_rpc_url: str
    base_mainnet_rpc_url: str
    aave_pool_address: str
    aave_pool_assets: str
    midas_issuance_vault_address: str
    midas_redemption_vault_address: str
    midas_mtbill_token_address: str
    midas_oracle_address: str
    midas_default_slippage_bps: int
    midas_oracle_heartbeat_sec: int
    midas_apy_bps: int
    midas_pool_assets: str

    # Pool id -> DefiLlama pool UUID, for strategies whose APY history we source
    # from DefiLlama. Pools left out simply have no history.
    defillama_pool_ids: str

    coingecko_token_ids: str

    lifi_execution_enabled: bool = False
    lifi_max_swap_amount_usd: int = 0

    pool_admin_secret_key: str = ""
