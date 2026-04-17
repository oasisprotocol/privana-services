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
async def test_deploy_is_noop(strategy: ManualStrategy) -> None:
    assert await strategy.deploy(1_000_000) is None


@pytest.mark.asyncio
async def test_withdraw_is_noop(strategy: ManualStrategy) -> None:
    assert await strategy.withdraw(1_000_000) is None


@pytest.mark.asyncio
async def test_pending_yield_is_zero(strategy: ManualStrategy) -> None:
    assert await strategy.pending_yield() == 0
