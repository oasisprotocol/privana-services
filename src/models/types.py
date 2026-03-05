from dataclasses import dataclass


@dataclass
class Settings:
    api_host: str = "0.0.0.0"
    api_port: int = 8001
    log_level: str = "INFO"
    environment: str = "development"

    accounting_api_base_url: str = "http://localhost:8000"

    lifi_api_key: str = ""
    lifi_api_url: str = "https://li.quest/v1"
    lifi_integrator: str = "flexvaults"

    vault_evm_address: str = "0x0000000000000000000000000000000000000000"
    service_address: str = "0x0000000000000000000000000000000000000000"

    fee_bps: int = 10
    swap_poll_interval: int = 5
    same_chain_timeout: int = 300
    cross_chain_timeout: int = 1800
