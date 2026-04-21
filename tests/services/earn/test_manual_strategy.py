import pytest

from src.services.earn.strategies.manual import ManualStrategy


@pytest.fixture
def strategy() -> ManualStrategy:
    return ManualStrategy()


def test_name(strategy: ManualStrategy) -> None:
    assert strategy.name == "manual"


@pytest.mark.asyncio
async def test_apy_is_zero(strategy: ManualStrategy) -> None:
    assert await strategy.get_apy_bps() == 0


@pytest.mark.asyncio
async def test_deposit_to_earn_is_noop(strategy: ManualStrategy) -> None:
    assert await strategy.deposit_to_earn(1_000_000) is None


@pytest.mark.asyncio
async def test_withdraw_from_earn_is_noop(strategy: ManualStrategy) -> None:
    assert await strategy.withdraw_from_earn(1_000_000) is None


@pytest.mark.asyncio
async def test_pending_yield_is_zero(strategy: ManualStrategy) -> None:
    assert await strategy.pending_yield() == 0
