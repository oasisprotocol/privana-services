from src.services.earn.strategies.base import BaseStrategy


class ManualStrategy(BaseStrategy):
    """Admin-controlled strategy. Funds stay in the pool, yield is submitted
    manually by admins via the harvest API.

    Reports 0 APY and 0 pending yield because the strategy has no autonomous
    view into yield accrual. The admin supplies the yield amount when calling
    /v1/earn/harvest, which is written on-chain as pool.totalAssets +=
    yieldAmount in EarnManager.harvest(). That on-chain total is the only
    state. Nothing is tracked here.

    total_assets is 0 by contract: a manual pool keeps its AUM inside the
    accounting layer, not in any external protocol, so there's no external
    balance for this strategy to report. The vault reads its own idle balance
    from accounting and adds this (0) on top.
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

    async def total_assets(self) -> int:
        return 0

    async def is_healthy(self) -> bool:
        return True
