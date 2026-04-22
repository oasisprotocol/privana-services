import logging
from unittest.mock import MagicMock

import pytest

from src.services.earn.strategies.aave import AaveStrategy


ASSET_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


@pytest.fixture
def aave_client():
    return MagicMock()


@pytest.fixture
def strategy(aave_client) -> AaveStrategy:
    return AaveStrategy(client=aave_client, asset_address=ASSET_ADDRESS)


def test_name(strategy: AaveStrategy) -> None:
    assert strategy.name == "aave-v3"


def test_asset_address_is_retained(strategy: AaveStrategy) -> None:
    assert strategy.asset_address == ASSET_ADDRESS


@pytest.mark.asyncio
async def test_get_apy_bps_delegates_to_client(strategy: AaveStrategy, aave_client) -> None:
    aave_client.get_supply_apy_bps.return_value = 487

    assert await strategy.get_apy_bps() == 487
    aave_client.get_supply_apy_bps.assert_called_once_with(ASSET_ADDRESS)


@pytest.mark.asyncio
async def test_deposit_to_earn_supplies_with_existing_allowance(
    strategy: AaveStrategy, aave_client
) -> None:
    aave_client.get_allowance.return_value = 10_000_000
    aave_client.supply.return_value = "0xabc"

    await strategy.deposit_to_earn(1_000_000)

    aave_client.get_allowance.assert_called_once_with(ASSET_ADDRESS)
    aave_client.approve_pool.assert_not_called()
    aave_client.supply.assert_called_once_with(ASSET_ADDRESS, 1_000_000)


@pytest.mark.asyncio
async def test_deposit_to_earn_tops_up_allowance_when_insufficient(
    strategy: AaveStrategy, aave_client
) -> None:
    aave_client.get_allowance.return_value = 500_000
    aave_client.supply.return_value = "0xabc"

    await strategy.deposit_to_earn(1_000_000)

    aave_client.approve_pool.assert_called_once_with(ASSET_ADDRESS, 1_000_000)
    aave_client.supply.assert_called_once_with(ASSET_ADDRESS, 1_000_000)


@pytest.mark.asyncio
async def test_deposit_to_earn_rejects_non_positive_amount(
    strategy: AaveStrategy, aave_client
) -> None:
    with pytest.raises(ValueError, match="positive amount"):
        await strategy.deposit_to_earn(0)

    aave_client.supply.assert_not_called()
    aave_client.approve_pool.assert_not_called()


@pytest.mark.asyncio
async def test_withdraw_from_earn_is_noop(strategy: AaveStrategy, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert await strategy.withdraw_from_earn(500_000) is None

    assert any("withdraw_from_earn: not implemented" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_pending_yield_is_zero(strategy: AaveStrategy) -> None:
    assert await strategy.pending_yield() == 0


@pytest.mark.asyncio
async def test_total_assets_reads_aToken_balance(strategy: AaveStrategy, aave_client) -> None:
    aave_client.account_address = "0xd8991364507FAfC256EafF950d28618735753476"
    aave_client.get_aToken_balance.return_value = 42_000_000

    assert await strategy.total_assets() == 42_000_000
    aave_client.get_aToken_balance.assert_called_once_with(
        ASSET_ADDRESS, "0xd8991364507FAfC256EafF950d28618735753476",
    )


@pytest.mark.asyncio
async def test_is_healthy_true_when_rate_read_succeeds(strategy: AaveStrategy, aave_client) -> None:
    aave_client.get_supply_apy_bps.return_value = 500

    assert await strategy.is_healthy() is True


@pytest.mark.asyncio
async def test_is_healthy_false_when_rate_read_raises(strategy: AaveStrategy, aave_client) -> None:
    aave_client.get_supply_apy_bps.side_effect = RuntimeError("rpc down")

    assert await strategy.is_healthy() is False
