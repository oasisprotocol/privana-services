import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.settings import Settings


ASSET_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TOKEN_ID = "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514"
POOL_ADDRESS = "0xd8991364507FAfC256EafF950d28618735753476"
LP_PRIVATE_KEY = "0x7b07a59f24f1900ec4e6ac3e521c1acd2cca3518f717abda1dc8bbcbbc344c4e"
ACCOUNTING_CONTRACT = "0xFfB141bF8269E458b074A274bE6E8F971f08A401"
DEPOSIT_ADDRESS_BASE = "0x1d5D19e0e68001624323f63c60479BD3AeE7E029"


@dataclass
class _PendingWithdrawal:
    index: int
    user_address: str = POOL_ADDRESS
    token_id: str = TOKEN_ID
    amount: int = 0
    status: str = "pending"
    created_at: int = 0


@dataclass
class _PendingWithdrawals:
    user_address: str
    withdrawals: list[_PendingWithdrawal]


@dataclass
class _TransferNonce:
    user_address: str
    nonce: int


@dataclass
class _TxSubmission:
    submission_id: str
    status: str = "pending"


@dataclass
class _WithdrawalInfo:
    index: int
    status: str
    transaction_hash: str | None = None


@dataclass
class _Balance:
    user_address: str
    token_id: str
    balance: int
    token_symbol: str = "USDC"
    chain_id: int = 84532


@dataclass
class _TransactionData:
    to: str = "0x0"
    value: int = 0
    data: str = "0x"
    chain_id: int = 84532


@dataclass
class _DepositQuote:
    user_address: str
    token_id: str
    amount: int
    deposit_address: str
    transaction: _TransactionData
    instructions: str = ""


@dataclass
class _IncludeDepositResponse:
    submission_id: str
    status: str = "pending"


def _strategy_settings() -> Settings:
    return Settings(
        liquidity_provider_private_key=LP_PRIVATE_KEY,
        liquidity_provider_address=POOL_ADDRESS,
        accounting_contract_address=ACCOUNTING_CONTRACT,
        accounting_chain_id=23295,
    )


@pytest.fixture
def aave_client():
    return MagicMock()


@pytest.fixture
def flexvaults():
    client = MagicMock()
    client.get_pending_withdrawals = AsyncMock(
        return_value=_PendingWithdrawals(user_address=POOL_ADDRESS, withdrawals=[]),
    )
    client.get_transfer_nonce = AsyncMock(
        return_value=_TransferNonce(user_address=POOL_ADDRESS, nonce=7),
    )
    client.request_withdrawal = AsyncMock(
        return_value=_TxSubmission(submission_id="sub-1", status="pending"),
    )
    client.get_withdrawal_info = AsyncMock(
        return_value=_WithdrawalInfo(index=1, status="completed", transaction_hash="0xabc"),
    )
    client.get_deposit_quote = AsyncMock(
        return_value=_DepositQuote(
            user_address=POOL_ADDRESS,
            token_id=TOKEN_ID,
            amount=1_000_000,
            deposit_address=DEPOSIT_ADDRESS_BASE,
            transaction=_TransactionData(),
        ),
    )
    client.include_deposit = AsyncMock(
        return_value=_IncludeDepositResponse(submission_id="dep-1"),
    )
    client.get_balance = AsyncMock(
        return_value=_Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=0),
    )
    return client


@pytest.fixture
def strategy(aave_client, flexvaults):
    from src.services.earn.strategies.aave import AaveStrategy

    with patch("src.services.earn.strategies.aave.load_settings", return_value=_strategy_settings()):
        return AaveStrategy(
            client=aave_client,
            asset_address=ASSET_ADDRESS,
            token_id=TOKEN_ID,
            flexvaults_client=flexvaults,
            poll_interval_sec=0,
        )


def test_name(strategy) -> None:
    assert strategy.name == "aave-v3"


def test_asset_address_is_retained(strategy) -> None:
    assert strategy.asset_address == ASSET_ADDRESS


def test_token_id_is_retained(strategy) -> None:
    assert strategy.token_id == TOKEN_ID


def test_pool_address_defaults_to_lp_address(strategy) -> None:
    assert strategy.pool_address == POOL_ADDRESS


def test_unsupported_chain_id_rejected(aave_client, flexvaults) -> None:
    from src.services.earn.strategies.aave import AaveStrategy

    bogus = Settings(
        liquidity_provider_private_key=LP_PRIVATE_KEY,
        liquidity_provider_address=POOL_ADDRESS,
        accounting_contract_address=ACCOUNTING_CONTRACT,
        accounting_chain_id=1,
    )
    with patch("src.services.earn.strategies.aave.load_settings", return_value=bogus):
        with pytest.raises(ValueError, match="unsupported accounting chain_id"):
            AaveStrategy(
                client=aave_client,
                asset_address=ASSET_ADDRESS,
                token_id=TOKEN_ID,
                flexvaults_client=flexvaults,
            )


@pytest.mark.asyncio
async def test_get_apy_bps_delegates_to_client(strategy, aave_client) -> None:
    aave_client.get_supply_apy_bps.return_value = 487

    assert await strategy.get_apy_bps() == 487
    aave_client.get_supply_apy_bps.assert_called_once_with(ASSET_ADDRESS)


@pytest.mark.asyncio
async def test_deposit_to_earn_bridges_then_supplies(strategy, aave_client, flexvaults) -> None:
    flexvaults.get_pending_withdrawals.side_effect = [
        _PendingWithdrawals(user_address=POOL_ADDRESS, withdrawals=[]),
        _PendingWithdrawals(
            user_address=POOL_ADDRESS,
            withdrawals=[_PendingWithdrawal(index=42, amount=1_000_000)],
        ),
        _PendingWithdrawals(user_address=POOL_ADDRESS, withdrawals=[]),
    ]
    aave_client.get_allowance.return_value = 0
    aave_client.supply.return_value = "0xsupply"

    await strategy.deposit_to_earn(1_000_000)

    flexvaults.get_transfer_nonce.assert_awaited_once_with(POOL_ADDRESS)
    flexvaults.request_withdrawal.assert_awaited_once()
    sent_request = flexvaults.request_withdrawal.await_args.args[0]
    assert sent_request.user_address == POOL_ADDRESS
    assert sent_request.token_id == TOKEN_ID
    assert sent_request.amount == 1_000_000
    assert sent_request.nonce == 7
    assert sent_request.signature.startswith("0x")

    flexvaults.get_withdrawal_info.assert_awaited_once_with(42)
    aave_client.approve_pool.assert_called_once_with(ASSET_ADDRESS, 1_000_000)
    aave_client.supply.assert_called_once_with(ASSET_ADDRESS, 1_000_000)


@pytest.mark.asyncio
async def test_deposit_to_earn_skips_approve_when_allowance_sufficient(
    strategy, aave_client, flexvaults
) -> None:
    flexvaults.get_pending_withdrawals.side_effect = [
        _PendingWithdrawals(user_address=POOL_ADDRESS, withdrawals=[]),
        _PendingWithdrawals(
            user_address=POOL_ADDRESS,
            withdrawals=[_PendingWithdrawal(index=10, amount=500_000)],
        ),
        _PendingWithdrawals(user_address=POOL_ADDRESS, withdrawals=[]),
    ]
    aave_client.get_allowance.return_value = 10_000_000_000
    aave_client.supply.return_value = "0xsupply"

    await strategy.deposit_to_earn(500_000)

    aave_client.approve_pool.assert_not_called()
    aave_client.supply.assert_called_once_with(ASSET_ADDRESS, 500_000)


@pytest.mark.asyncio
async def test_deposit_to_earn_rejects_non_positive_amount(strategy, aave_client, flexvaults) -> None:
    with pytest.raises(ValueError, match="positive amount"):
        await strategy.deposit_to_earn(0)
    with pytest.raises(ValueError, match="positive amount"):
        await strategy.deposit_to_earn(-1)

    flexvaults.request_withdrawal.assert_not_called()
    aave_client.supply.assert_not_called()


@pytest.mark.asyncio
async def test_deposit_to_earn_propagates_request_failure(strategy, aave_client, flexvaults) -> None:
    flexvaults.request_withdrawal.side_effect = RuntimeError("rejected by accounting")

    with pytest.raises(RuntimeError, match="rejected by accounting"):
        await strategy.deposit_to_earn(1_000_000)

    aave_client.supply.assert_not_called()


@pytest.mark.asyncio
async def test_bridge_raises_on_terminal_failed_status(strategy, aave_client, flexvaults) -> None:
    flexvaults.get_pending_withdrawals.side_effect = [
        _PendingWithdrawals(user_address=POOL_ADDRESS, withdrawals=[]),
        _PendingWithdrawals(
            user_address=POOL_ADDRESS,
            withdrawals=[_PendingWithdrawal(index=1, amount=1_000_000)],
        ),
        _PendingWithdrawals(user_address=POOL_ADDRESS, withdrawals=[]),
    ]
    flexvaults.get_withdrawal_info.return_value = _WithdrawalInfo(
        index=1, status="failed", transaction_hash=None,
    )

    with pytest.raises(RuntimeError, match="status=failed"):
        await strategy.deposit_to_earn(1_000_000)

    aave_client.supply.assert_not_called()


@pytest.mark.asyncio
async def test_bridge_raises_when_pending_terminal_status(strategy, aave_client, flexvaults) -> None:
    flexvaults.get_pending_withdrawals.side_effect = [
        _PendingWithdrawals(user_address=POOL_ADDRESS, withdrawals=[]),
        _PendingWithdrawals(
            user_address=POOL_ADDRESS,
            withdrawals=[_PendingWithdrawal(index=2, amount=1_000_000, status="rejected")],
        ),
    ]

    with pytest.raises(RuntimeError, match="terminal status=rejected"):
        await strategy.deposit_to_earn(1_000_000)


@pytest.mark.asyncio
async def test_bridge_keeps_polling_while_state_unresolved(
    strategy, aave_client, flexvaults
) -> None:
    flexvaults.get_pending_withdrawals.side_effect = [
        _PendingWithdrawals(user_address=POOL_ADDRESS, withdrawals=[]),
        _PendingWithdrawals(
            user_address=POOL_ADDRESS,
            withdrawals=[_PendingWithdrawal(index=5, amount=1_000_000)],
        ),
        _PendingWithdrawals(
            user_address=POOL_ADDRESS,
            withdrawals=[_PendingWithdrawal(index=5, amount=1_000_000)],
        ),
        _PendingWithdrawals(user_address=POOL_ADDRESS, withdrawals=[]),
    ]
    aave_client.get_allowance.return_value = 10_000_000

    await strategy.deposit_to_earn(1_000_000)

    assert flexvaults.get_pending_withdrawals.await_count == 4
    aave_client.supply.assert_called_once_with(ASSET_ADDRESS, 1_000_000)


@pytest.mark.asyncio
async def test_withdraw_from_earn_redeems_transfers_and_polls_until_credited(
    strategy, aave_client, flexvaults
) -> None:
    aave_client.withdraw.return_value = "0xredeem"
    aave_client.transfer_erc20.return_value = "0xtransfer"
    flexvaults.get_balance.side_effect = [
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=0),
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=0),
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=750_000),
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=1_000_000),
    ]

    await strategy.withdraw_from_earn(1_000_000)

    aave_client.withdraw.assert_called_once_with(ASSET_ADDRESS, 1_000_000, to=POOL_ADDRESS)

    flexvaults.get_deposit_quote.assert_awaited_once()
    quote_request = flexvaults.get_deposit_quote.await_args.args[0]
    assert quote_request.user_address == POOL_ADDRESS
    assert quote_request.token_id == TOKEN_ID
    assert quote_request.amount == 1_000_000

    aave_client.transfer_erc20.assert_called_once_with(
        ASSET_ADDRESS, DEPOSIT_ADDRESS_BASE, 1_000_000,
    )

    flexvaults.include_deposit.assert_awaited_once()
    include_request = flexvaults.include_deposit.await_args.args[0]
    assert include_request.user_address == POOL_ADDRESS
    assert include_request.token_id == TOKEN_ID
    assert include_request.evm_transaction_data == "0xtransfer"

    assert flexvaults.get_balance.await_count >= 3


@pytest.mark.asyncio
async def test_withdraw_from_earn_uses_pre_balance_baseline(
    strategy, aave_client, flexvaults
) -> None:
    """Pre-existing balance shouldn't satisfy the credit check; we must wait
    for an additional `amount` to land.
    """
    aave_client.withdraw.return_value = "0xredeem"
    aave_client.transfer_erc20.return_value = "0xtransfer"
    flexvaults.get_balance.side_effect = [
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=400_000),
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=400_000),
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=900_000),
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=1_400_000),
    ]

    await strategy.withdraw_from_earn(1_000_000)

    assert flexvaults.get_balance.await_count == 4


@pytest.mark.asyncio
async def test_withdraw_from_earn_rejects_non_positive_amount(strategy, aave_client) -> None:
    with pytest.raises(ValueError, match="positive amount"):
        await strategy.withdraw_from_earn(0)
    with pytest.raises(ValueError, match="positive amount"):
        await strategy.withdraw_from_earn(-1)

    aave_client.withdraw.assert_not_called()
    aave_client.transfer_erc20.assert_not_called()


@pytest.mark.asyncio
async def test_withdraw_from_earn_propagates_aave_failure(
    strategy, aave_client, flexvaults
) -> None:
    aave_client.withdraw.side_effect = RuntimeError("aave reverted")

    with pytest.raises(RuntimeError, match="aave reverted"):
        await strategy.withdraw_from_earn(1_000_000)

    aave_client.transfer_erc20.assert_not_called()
    flexvaults.include_deposit.assert_not_called()


@pytest.mark.asyncio
async def test_pending_yield_is_zero(strategy) -> None:
    assert await strategy.pending_yield() == 0


@pytest.mark.asyncio
async def test_total_assets_reads_aToken_balance_for_pool_address(strategy, aave_client) -> None:
    aave_client.get_aToken_balance.return_value = 42_000_000

    assert await strategy.total_assets() == 42_000_000
    aave_client.get_aToken_balance.assert_called_once_with(ASSET_ADDRESS, POOL_ADDRESS)


@pytest.mark.asyncio
async def test_is_healthy_true_when_rate_read_succeeds(strategy, aave_client) -> None:
    aave_client.get_supply_apy_bps.return_value = 500

    assert await strategy.is_healthy() is True


@pytest.mark.asyncio
async def test_is_healthy_false_when_rate_read_raises(strategy, aave_client) -> None:
    aave_client.get_supply_apy_bps.side_effect = RuntimeError("rpc down")

    assert await strategy.is_healthy() is False


_ = asyncio
