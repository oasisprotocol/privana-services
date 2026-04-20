import logging

from src.clients.aave import AaveClient
from src.services.earn.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class AaveStrategy(BaseStrategy):
    """Aave V3 strategy — live APY, deploy/withdraw are no-ops for now.

    The asset address is passed at construction time because pools in our
    accounting system are keyed by tokenId, not by chain-specific ERC-20
    addresses. Whoever instantiates the strategy per pool resolves that
    mapping once.

    Sprint 4 fills in deploy/withdraw using the accounting-module withdrawal
    + deposit-listener flow. Until then, funds stay in the pool's accounting
    slot and yield is realized via the admin harvest endpoint.
    """

    def __init__(self, client: AaveClient, asset_address: str) -> None:
        self._client = client
        self._asset_address = asset_address

    @property
    def name(self) -> str:
        return "aave-v3"

    @property
    def asset_address(self) -> str:
        return self._asset_address

    async def get_apy_bps(self) -> int:
        return self._client.get_supply_apy_bps(self._asset_address)

    async def deploy(self, amount: int) -> None:
        logger.warning(
            "AaveStrategy.deploy is a no-op (sprint 4): asset=%s amount=%d",
            self._asset_address, amount,
        )

    async def withdraw(self, amount: int) -> None:
        logger.warning(
            "AaveStrategy.withdraw is a no-op (sprint 4): asset=%s amount=%d",
            self._asset_address, amount,
        )

    async def pending_yield(self) -> int:
        return 0
