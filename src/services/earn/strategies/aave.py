import logging

from src.clients.aave import AaveClient
from src.services.earn.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class AaveStrategy(BaseStrategy):
    """Aave V3 strategy. Supplies to and redeems from Aave V3 on Base.

    The asset address is passed at construction time because pools in our
    accounting system are keyed by tokenId, not by chain-specific ERC-20
    addresses. Whoever instantiates the strategy per pool resolves that
    mapping once.

    Scope: this adapter only handles the Aave-side of the flow (approve +
    supply, withdraw). Moving funds from the flexvaults accounting layer
    on Sapphire to the LP EOA on Base, and back, is the VaultService's
    job. Precondition for deposit_to_earn: the ERC-20 balance is already
    on the LP EOA on Base.

    TODO: once accounting exposes a cross-chain withdrawal primitive, the
    vault service will drive it; MVP assumes admin pre-funds the LP EOA.
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

    async def deposit_to_earn(self, amount: int) -> None:
        """Supply `amount` of the underlying asset to Aave.

        Ensures the pool has sufficient allowance; if not, submits an approve
        tx first. Approval is only refreshed when short; we don't reset to
        zero beforehand because (a) the asset is sitting on our own EOA so
        there's no front-running risk and (b) an extra tx per deposit is
        wasteful.
        """
        if amount <= 0:
            raise ValueError(f"deposit_to_earn requires a positive amount, got {amount}")

        allowance = self._client.get_allowance(self._asset_address)
        if allowance < amount:
            logger.info(
                "AaveStrategy.deposit_to_earn: topping up allowance asset=%s current=%d needed=%d",
                self._asset_address, allowance, amount,
            )
            self._client.approve_pool(self._asset_address, amount)

        tx_hash = self._client.supply(self._asset_address, amount)
        logger.info(
            "AaveStrategy.deposit_to_earn: supplied asset=%s amount=%d tx=%s",
            self._asset_address, amount, tx_hash,
        )

    async def withdraw_from_earn(self, amount: int) -> None:
        """Redeem `amount` of the underlying asset from Aave. Funds return to
        the LP EOA on Base.

        Re-depositing them into the flexvaults accounting layer on Sapphire
        is the VaultService's job. This adapter only handles the Aave side.
        """
        if amount <= 0:
            raise ValueError(f"withdraw_from_earn requires a positive amount, got {amount}")

        tx_hash = self._client.withdraw(self._asset_address, amount)
        logger.info(
            "AaveStrategy.withdraw_from_earn: redeemed asset=%s amount=%d tx=%s",
            self._asset_address, amount, tx_hash,
        )

    async def pending_yield(self) -> int:
        return 0

    async def total_assets(self) -> int:
        """aToken balance held by the LP EOA for this asset, which equals
        principal plus accrued Aave yield.
        TODO: thread the holder address through explicitly once the pool
        address model lands; for now we lean on the client's LP account.
        """
        return self._client.get_aToken_balance(
            self._asset_address, self._client.account_address,
        )

    async def is_healthy(self) -> bool:
        """Treat a successful supply-rate read as a cheap liveness proxy. If
        getReserveData throws, the pool is unreachable or the asset isn't
        listed; either way, don't route deposits here.
        """
        try:
            self._client.get_supply_apy_bps(self._asset_address)
            return True
        except Exception as exc:
            logger.warning(
                "AaveStrategy.is_healthy: probe failed asset=%s err=%s",
                self._asset_address, exc,
            )
            return False
