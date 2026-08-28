import asyncio
import time
from dataclasses import dataclass, replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients.defillama import ChartPoint
from src.core.config import load_settings
from src.models.settings import Settings

ASSET_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TOKEN_ID = "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514"
POOL_ADDRESS = "0xd8991364507FAfC256EafF950d28618735753476"
LP_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ACCOUNTING_CONTRACT = "0xad3C76e4E621C0cfF7540479Ee9B0A945723A642"
DEPOSIT_ADDRESS_BASE = "0x1d5D19e0e68001624323f63c60479BD3AeE7E029"


@dataclass
class _PendingWithdrawal:
    index: int
    user_address: str = POOL_ADDRESS
    token_id: str = TOKEN_ID
    amount: int = 0
    block_number: int = 0
    resolved: bool = False
    tx_identifier: str = ""


@dataclass
class _PendingWithdrawalsResponse:
    user_address: str
    pending_withdrawals: list[_PendingWithdrawal]


@dataclass
class _WithdrawalNonce:
    user_address: str
    nonce: int


@dataclass
class _TxSubmission:
    submission_id: str
    status: str = "pending"
    detail: str | None = None


@dataclass
class _WithdrawalInfo:
    index: int
    user_address: str = POOL_ADDRESS
    token_id: str = TOKEN_ID
    amount: int = 0
    block_number: int = 0
    resolved: bool = False
    tx_identifier: str = ""


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
class _DepositAddressResponse:
    deposit_address: str
    chain_type: str = "evm"
    version: int = 0


@dataclass
class _DepositCheckResponse:
    status: str = "pending"
    deposit_id: str | None = None
    amount: str | None = None
    token_address: str | None = None
    detail: str | None = None


def _strategy_settings() -> Settings:
    return replace(
        load_settings(),
        liquidity_provider_secret_key=LP_PRIVATE_KEY,
        liquidity_provider_address=POOL_ADDRESS,
        accounting_contract_address=ACCOUNTING_CONTRACT,
        accounting_chain_id=23295,
    )


@pytest.fixture
def aave_client():
    return MagicMock()


@pytest.fixture
def privana():
    client = MagicMock()
    client.get_pending_withdrawals = AsyncMock(
        return_value=_PendingWithdrawalsResponse(user_address=POOL_ADDRESS, pending_withdrawals=[]),
    )
    client.get_withdrawal_nonce = AsyncMock(
        return_value=_WithdrawalNonce(user_address=POOL_ADDRESS, nonce=7),
    )
    client.request_withdrawal = AsyncMock(
        return_value=_TxSubmission(submission_id="sub-1", status="pending"),
    )
    client.get_withdrawal_info = AsyncMock(
        return_value=_WithdrawalInfo(index=1, resolved=True, tx_identifier="0xabc"),
    )
    client.get_deposit_address = AsyncMock(
        return_value=_DepositAddressResponse(deposit_address=DEPOSIT_ADDRESS_BASE),
    )
    client.check_deposit = AsyncMock(
        return_value=_DepositCheckResponse(status="pending", deposit_id="dep-1"),
    )
    client.get_balance = AsyncMock(
        return_value=_Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=0),
    )
    return client


@pytest.fixture
def strategy(aave_client, privana):
    from src.services.earn.strategies.aave import AaveStrategy

    with patch(
        "src.services.earn.strategies.aave.load_settings", return_value=_strategy_settings()
    ):
        return AaveStrategy(
            client=aave_client,
            asset_address=ASSET_ADDRESS,
            token_id=TOKEN_ID,
            privana_client=privana,
            poll_interval_sec=0,
        )


def test_name(strategy) -> None:
    assert strategy.name == "aave-v3"


async def test_idle_assets_reports_the_pool_accounting_balance(strategy) -> None:
    # Funds credited to the pool but not yet in Aave still back minted shares,
    # so they must count toward AUM (EA-Products C-0017).
    with patch.object(strategy, "_read_pool_balance", AsyncMock(return_value=4200)):
        assert await strategy.idle_assets() == 4200


def test_asset_address_is_retained(strategy) -> None:
    assert strategy.asset_address == ASSET_ADDRESS


def test_token_id_is_retained(strategy) -> None:
    assert strategy.token_id == TOKEN_ID


def test_pool_address_defaults_to_lp_address(strategy) -> None:
    assert strategy.pool_address == POOL_ADDRESS


def test_unsupported_chain_id_rejected(aave_client, privana) -> None:
    from src.services.earn.strategies.aave import AaveStrategy

    bogus = replace(
        load_settings(),
        liquidity_provider_secret_key=LP_PRIVATE_KEY,
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
                privana_client=privana,
            )


@pytest.mark.asyncio
async def test_get_apy_bps_delegates_to_client(strategy, aave_client) -> None:
    aave_client.get_supply_apy_bps.return_value = 487

    assert await strategy.get_apy_bps() == 487
    aave_client.get_supply_apy_bps.assert_called_once_with(ASSET_ADDRESS)


@pytest.mark.asyncio
async def test_deposit_to_earn_bridges_then_supplies(strategy, aave_client, privana) -> None:
    privana.get_pending_withdrawals.side_effect = [
        _PendingWithdrawalsResponse(user_address=POOL_ADDRESS, pending_withdrawals=[]),
        _PendingWithdrawalsResponse(
            user_address=POOL_ADDRESS,
            pending_withdrawals=[_PendingWithdrawal(index=42, amount=1_000_000)],
        ),
    ]
    privana.get_withdrawal_info.return_value = _WithdrawalInfo(
        index=42,
        resolved=True,
        tx_identifier="0xresolved",
    )
    aave_client.get_allowance.return_value = 0
    aave_client.supply.return_value = "0xsupply"

    await strategy.deposit_to_earn(1_000_000)

    privana.get_withdrawal_nonce.assert_awaited_once_with(POOL_ADDRESS)
    privana.request_withdrawal.assert_awaited_once()
    sent_request = privana.request_withdrawal.await_args.args[0]
    assert sent_request.token_id == TOKEN_ID
    assert sent_request.amount == 1_000_000
    assert sent_request.nonce == 7
    assert sent_request.signature.startswith("0x")

    privana.get_withdrawal_info.assert_awaited_with(42)
    aave_client.approve_pool.assert_called_once_with(ASSET_ADDRESS, 1_000_000)
    aave_client.supply.assert_called_once_with(ASSET_ADDRESS, 1_000_000)


@pytest.mark.asyncio
async def test_deposit_to_earn_skips_approve_when_allowance_sufficient(
    strategy, aave_client, privana
) -> None:
    privana.get_pending_withdrawals.side_effect = [
        _PendingWithdrawalsResponse(user_address=POOL_ADDRESS, pending_withdrawals=[]),
        _PendingWithdrawalsResponse(
            user_address=POOL_ADDRESS,
            pending_withdrawals=[_PendingWithdrawal(index=10, amount=500_000)],
        ),
    ]
    privana.get_withdrawal_info.return_value = _WithdrawalInfo(
        index=10,
        resolved=True,
        tx_identifier="0xresolved",
    )
    aave_client.get_allowance.return_value = 10_000_000_000
    aave_client.supply.return_value = "0xsupply"

    await strategy.deposit_to_earn(500_000)

    aave_client.approve_pool.assert_not_called()
    aave_client.supply.assert_called_once_with(ASSET_ADDRESS, 500_000)


@pytest.mark.asyncio
async def test_deposit_to_earn_rejects_non_positive_amount(strategy, aave_client, privana) -> None:
    with pytest.raises(ValueError, match="positive amount"):
        await strategy.deposit_to_earn(0)
    with pytest.raises(ValueError, match="positive amount"):
        await strategy.deposit_to_earn(-1)

    privana.request_withdrawal.assert_not_called()
    aave_client.supply.assert_not_called()


@pytest.mark.asyncio
async def test_deposit_to_earn_propagates_request_failure(strategy, aave_client, privana) -> None:
    privana.request_withdrawal.side_effect = RuntimeError("rejected by accounting")

    with pytest.raises(RuntimeError, match="rejected by accounting"):
        await strategy.deposit_to_earn(1_000_000)

    aave_client.supply.assert_not_called()


@pytest.mark.asyncio
async def test_bridge_keeps_polling_until_resolved(strategy, aave_client, privana) -> None:
    privana.get_pending_withdrawals.side_effect = [
        _PendingWithdrawalsResponse(user_address=POOL_ADDRESS, pending_withdrawals=[]),
        _PendingWithdrawalsResponse(
            user_address=POOL_ADDRESS,
            pending_withdrawals=[_PendingWithdrawal(index=5, amount=1_000_000)],
        ),
        _PendingWithdrawalsResponse(
            user_address=POOL_ADDRESS,
            pending_withdrawals=[_PendingWithdrawal(index=5, amount=1_000_000)],
        ),
    ]
    privana.get_withdrawal_info.side_effect = [
        _WithdrawalInfo(index=5, resolved=False),
        _WithdrawalInfo(index=5, resolved=True, tx_identifier="0xfinal"),
    ]
    aave_client.get_allowance.return_value = 10_000_000

    await strategy.deposit_to_earn(1_000_000)

    assert privana.get_withdrawal_info.await_count == 2
    aave_client.supply.assert_called_once_with(ASSET_ADDRESS, 1_000_000)


@pytest.mark.asyncio
async def test_withdraw_from_earn_redeems_transfers_and_polls_until_credited(
    strategy, aave_client, privana
) -> None:
    aave_client.withdraw.return_value = "0xredeem"
    aave_client.transfer_erc20.return_value = "0xtransfer"
    privana.get_balance.side_effect = [
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=0),
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=0),
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=750_000),
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=1_000_000),
    ]

    await strategy.withdraw_from_earn(1_000_000)

    aave_client.withdraw.assert_called_once_with(ASSET_ADDRESS, 1_000_000, to=POOL_ADDRESS)

    privana.get_deposit_address.assert_awaited_once()
    address_request = privana.get_deposit_address.await_args.args[0]
    assert address_request.chain_type == "evm"

    aave_client.transfer_erc20.assert_called_once_with(
        ASSET_ADDRESS,
        DEPOSIT_ADDRESS_BASE,
        1_000_000,
    )

    privana.check_deposit.assert_awaited_once()
    check_request = privana.check_deposit.await_args.args[0]
    assert check_request.chain_id == aave_client.w3.eth.chain_id
    assert check_request.tx_hash == "0xtransfer"
    assert check_request.amount == 1_000_000

    assert privana.get_balance.await_count >= 3


@pytest.mark.asyncio
async def test_withdraw_from_earn_uses_pre_balance_baseline(strategy, aave_client, privana) -> None:
    aave_client.withdraw.return_value = "0xredeem"
    aave_client.transfer_erc20.return_value = "0xtransfer"
    privana.get_balance.side_effect = [
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=400_000),
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=400_000),
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=900_000),
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=1_400_000),
    ]

    await strategy.withdraw_from_earn(1_000_000)

    assert privana.get_balance.await_count == 4


@pytest.mark.asyncio
async def test_withdraw_from_earn_rejects_non_positive_amount(strategy, aave_client) -> None:
    with pytest.raises(ValueError, match="positive amount"):
        await strategy.withdraw_from_earn(0)
    with pytest.raises(ValueError, match="positive amount"):
        await strategy.withdraw_from_earn(-1)

    aave_client.withdraw.assert_not_called()
    aave_client.transfer_erc20.assert_not_called()


@pytest.mark.asyncio
async def test_withdraw_from_earn_propagates_aave_failure(strategy, aave_client, privana) -> None:
    aave_client.withdraw.side_effect = RuntimeError("aave reverted")

    with pytest.raises(RuntimeError, match="aave reverted"):
        await strategy.withdraw_from_earn(1_000_000)

    aave_client.transfer_erc20.assert_not_called()
    privana.check_deposit.assert_not_called()


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


@pytest.mark.asyncio
async def test_is_healthy_false_when_asset_not_listed_on_pool(strategy, aave_client) -> None:
    aave_client.get_supply_apy_bps.side_effect = ValueError(
        "Asset 0xusdc is not a listed reserve on Aave pool 0xpool"
    )

    assert await strategy.is_healthy() is False


@pytest.mark.asyncio
async def test_retry_on_network_error_recovers_from_transient_drop(strategy) -> None:
    from privana.client.errors import NetworkError

    factory = MagicMock(
        side_effect=[
            NetworkError("Server disconnected"),
            NetworkError("Server disconnected again"),
            "ok",
        ]
    )

    async def call() -> str:
        return factory()

    result = await strategy._retry_on_network_error("probe", call)

    assert result == "ok"
    assert factory.call_count == 3


@pytest.mark.asyncio
async def test_bridge_fails_fast_when_request_rejected(strategy, aave_client, privana) -> None:
    privana.request_withdrawal.return_value = _TxSubmission(
        submission_id="sub-x",
        status="rejected",
    )

    with pytest.raises(RuntimeError, match="Withdrawal request rejected.*rejected"):
        await strategy.deposit_to_earn(1_000_000)

    privana.get_withdrawal_info.assert_not_called()
    aave_client.supply.assert_not_called()


@pytest.mark.asyncio
async def test_bridge_survives_transient_network_error_mid_poll(
    strategy, aave_client, privana
) -> None:
    from privana.client.errors import NetworkError

    privana.get_pending_withdrawals.side_effect = [
        _PendingWithdrawalsResponse(user_address=POOL_ADDRESS, pending_withdrawals=[]),
        NetworkError("Server disconnected"),
        _PendingWithdrawalsResponse(
            user_address=POOL_ADDRESS,
            pending_withdrawals=[_PendingWithdrawal(index=99, amount=1_000_000)],
        ),
    ]
    privana.get_withdrawal_info.return_value = _WithdrawalInfo(
        index=99,
        resolved=True,
        tx_identifier="0xresolved",
    )
    aave_client.get_allowance.return_value = 10_000_000

    await strategy.deposit_to_earn(1_000_000)

    assert privana.get_pending_withdrawals.await_count == 3
    aave_client.supply.assert_called_once_with(ASSET_ADDRESS, 1_000_000)


@pytest.mark.asyncio
async def test_bridge_raises_after_max_poll_attempts(aave_client, privana) -> None:
    from src.services.earn.strategies.aave import AaveStrategy

    privana.get_pending_withdrawals = AsyncMock(
        return_value=_PendingWithdrawalsResponse(
            user_address=POOL_ADDRESS,
            pending_withdrawals=[],
        ),
    )

    with patch(
        "src.services.earn.strategies.aave.load_settings", return_value=_strategy_settings()
    ):
        strategy = AaveStrategy(
            client=aave_client,
            asset_address=ASSET_ADDRESS,
            token_id=TOKEN_ID,
            privana_client=privana,
            poll_interval_sec=0,
            max_bridge_poll_attempts=2,
        )

    with pytest.raises(RuntimeError, match="aborting to release lock"):
        await strategy.deposit_to_earn(1_000_000)

    aave_client.supply.assert_not_called()


LLAMA_POOL = "7e0661bf-8cf3-45e6-9424-31916d4c7b84"


def _chart_point(days_ago: int, apy_bps: int) -> ChartPoint:
    # The client already parsed DefiLlama's wire format; the strategy only ever
    # sees points in bps.
    return ChartPoint(timestamp=int(time.time() - days_ago * 86400), apy_bps=apy_bps)


def _history_strategy(aave_client, privana, llama, pool_id=LLAMA_POOL):
    from src.services.earn.strategies.aave import AaveStrategy

    with patch(
        "src.services.earn.strategies.aave.load_settings", return_value=_strategy_settings()
    ):
        return AaveStrategy(
            client=aave_client,
            asset_address=ASSET_ADDRESS,
            token_id=TOKEN_ID,
            privana_client=privana,
            defillama_pool_id=pool_id,
            defillama_client=llama,
        )


@pytest.mark.asyncio
async def test_get_apy_history_is_empty_without_a_configured_pool(strategy) -> None:
    # No DefiLlama pool configured is a normal state, not an error: no chart.
    assert await strategy.get_apy_history() == []


@pytest.mark.asyncio
async def test_get_apy_history_windows_to_recent_days(aave_client, privana) -> None:
    llama = MagicMock()
    llama.get_pool_chart = AsyncMock(
        return_value=[_chart_point(90, 500), _chart_point(10, 400), _chart_point(1, 300)]
    )

    windowed = await _history_strategy(aave_client, privana, llama).get_apy_history(days=30)
    everything = await _history_strategy(aave_client, privana, llama).get_apy_history()

    assert [p.apy_bps for p in windowed] == [400, 300]
    assert [p.apy_bps for p in everything] == [500, 400, 300]


@pytest.mark.asyncio
async def test_get_apy_history_degrades_when_defillama_fails(aave_client, privana) -> None:
    # The chart is decoration on a working pool; a dead third party must not take
    # the pool's endpoint down with it.
    llama = MagicMock()
    llama.get_pool_chart = AsyncMock(side_effect=RuntimeError("llama down"))

    assert await _history_strategy(aave_client, privana, llama).get_apy_history() == []


_ = asyncio
