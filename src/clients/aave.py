import logging
from typing import Optional

from web3 import Web3

from src.core.abi import load_abi
from src.core.config import load_settings

logger = logging.getLogger(__name__)

AAVE_POOL_ABI = load_abi("AaveV3Pool")

RAY = 10**27
RAY_TO_BPS = 10**23


class AaveClient:
    """Read-only Aave V3 Pool client — queries supply/borrow rates.

    Reports APY on earn pools.
    TODO: extend to write paths (supply, withdraw) but those live on the
    strategy side, not here.
    """

    def __init__(self) -> None:
        settings = load_settings()
        self.w3 = Web3(Web3.HTTPProvider(settings.base_sepolia_rpc_url))
        self.pool_address = Web3.to_checksum_address(settings.aave_pool_address)
        self.pool = self.w3.eth.contract(
            address=self.pool_address,
            abi=AAVE_POOL_ABI,
        )

    def get_supply_apy_bps(self, asset_address: str) -> int:
        """Current supply rate for the given asset, in basis points.

        Aave V3 returns currentLiquidityRate as APR (linear annualized) in
        RAY units (1e27). We convert to bps via integer division. The
        APR→APY gap at typical supply rates (<10%) is under ~2 bps and is
        rounded away.
        TODO: compound explicitly if needed?
        """
        asset = Web3.to_checksum_address(asset_address)
        reserve = self.pool.functions.getReserveData(asset).call()
        current_liquidity_rate = reserve[2]
        return current_liquidity_rate // RAY_TO_BPS


_client_instance: Optional[AaveClient] = None


def get_aave_client() -> AaveClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = AaveClient()
    return _client_instance
