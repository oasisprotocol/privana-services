from dataclasses import dataclass, field
from typing import Dict


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
    sapphire_rpc_headers: Dict[str, str]

    quote_ttl: int
    fee_bps: int
    fee_policies_json: str
    max_swap_amount_usd: int
    lifi_token_map: str

    base_rpc_url: str
    ethereum_rpc_url: str
    aave_pool_address: str
    aave_pool_assets: str
    midas_chain_id: int
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
    coingecko_api_key: str = ""

    lifi_execution_enabled: bool = False
    # HyperEVM (chain 999), used to execute external swaps of its tokens.
    hyperevm_rpc_url: str = ""
    lifi_max_swap_amount_usd: int = 0

    pool_admin_secret_key: str = ""

    # Account that holds every earn pool's underlying balance. Must be its own
    # account: whatever address a pool is created with has its whole accounting
    # balance counted as that pool's backing, so sharing it with the swap
    # liquidity provider prices deposits against swap float.
    earn_pool_secret_key: str = ""
    earn_pool_address: str = ""

    # Net shares each pool moved on chain without going through this service
    # (pool id -> signed share count), so its recorded history can still be
    # reconciled against the chain's totalShares.
    earn_unrecorded_shares: Dict[str, int] = field(default_factory=dict)

    # How often each pool's idle funds are netted against waiting withdrawals
    # and moved in or out of its strategy as one amount.
    earn_batch_interval_sec: int = 300
    # Kept on the pool's account to pay withdrawals without touching the
    # strategy: the larger of a fixed floor (token base units) and a share of
    # the pool's assets. No floor by default: one larger than the pool would
    # hold most of it idle, earning nothing.
    earn_buffer_min: int = 0
    earn_buffer_bps: int = 500
    earn_max_pending_per_user: int = 5
