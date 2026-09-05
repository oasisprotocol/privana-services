from src.services.earn.strategies.base import BaseStrategy


class ManualStrategy(BaseStrategy):
    """No-op strategy used as the registry default for pools without an
    external yield source. All flows are no-ops; the pool's full AUM lives
    in the accounting layer.

    `total_assets` returns 0 by contract because the strategy holds no
    external balance. Callers fall back to the on-chain `pool.totalAssets`
    snapshot for AUM. If an operator ever needs to credit yield to such a
    pool, they call `EarnManager.syncTotalAssets(poolId, newTotal)` directly
    against the contract.
    """

    @property
    def name(self) -> str:
        return "manual"

    async def get_apy_bps(self) -> int:
        return 0

    async def deposit_to_earn(self, amount: int) -> None:
        return None

    async def withdraw_from_earn(self, amount: int) -> None:
        return None

    async def total_assets(self) -> int:
        return 0

    async def idle_assets(self) -> int:
        # A manual pool parks nothing off to one side; its funds are the
        # on-chain balance the vault already counts.
        return 0

    async def is_healthy(self) -> bool:
        return True
