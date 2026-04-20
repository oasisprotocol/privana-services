from src.services.earn.strategies.base import BaseStrategy


class ManualStrategy(BaseStrategy):
    """Admin-controlled strategy — funds stay in the pool, yield is submitted
    manually by admins via the harvest API.

    Reports 0 APY and 0 pending yield because the strategy has no autonomous
    view into yield accrual. The admin supplies the yield amount when calling
    /v1/earn/harvest, which is written on-chain as pool.totalAssets +=
    yieldAmount in EarnManager.harvest(). That on-chain total is the only
    state — nothing is tracked here.
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

    async def pending_yield(self) -> int:
        return 0
