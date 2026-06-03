import asyncio
from dataclasses import dataclass, replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import load_settings
from src.models.settings import Settings


ASSET_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TOKEN_ID = "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514"
POOL_ADDRESS = "0xd8991364507FAfC256EafF950d28618735753476"
LP_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ACCOUNTING_CONTRACT = "0xad3C76e4E621C0cfF7540479Ee9B0A945723A642"
DEPOSIT_ADDRESS_BASE = "0x1d5D19e0e68001624323f63c60479BD3AeE7E029"
ISSUANCE_VAULT = "0x8978e327FE7C72Fa4eaF4649C23147E279ae1470"
REDEMPTION_VAULT = "0x2a8c22E3b10036f3AEF5875d04f8441d4188b656"


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
    chain_id: int = 8453


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


def _strategy_settings(
    slippage_bps: int = 50,
    heartbeat_sec: int = 86400,
    apy_bps: int = 350,
) -> Settings:
    return replace(
        load_settings(),
        liquidity_provider_secret_key=LP_PRIVATE_KEY,
        liquidity_provider_address=POOL_ADDRESS,
        accounting_contract_address=ACCOUNTING_CONTRACT,
        accounting_chain_id=23295,
        midas_default_slippage_bps=slippage_bps,
        midas_oracle_heartbeat_sec=heartbeat_sec,
        midas_apy_bps=apy_bps,
    )


@pytest.fixture
def midas_client():
    client = MagicMock()
    client.issuance_vault_address = ISSUANCE_VAULT
    client.redemption_vault_address = REDEMPTION_VAULT
    client.w3.eth.chain_id = 8453
    client.get_allowance.return_value = 0
    client.approve.return_value = "0xapprove"
    client.deposit_instant.return_value = "0xdeposit"
    client.redeem_instant.return_value = "0xredeem"
    client.transfer_erc20.return_value = "0xtransfer"
    client.get_erc20_balance.return_value = 0
    client.get_oracle_answer.return_value = 10**18
    client.get_oracle_decimals.return_value = 18
    client.get_oracle_round.return_value = (10**18, 1_700_086_400)
    client.get_mtbill_balance.return_value = 0
    client.is_issuance_paused.return_value = False
    client.is_redemption_paused.return_value = False
    client.get_redemption_instant_fee_bps.return_value = 0
    return client


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
def strategy(midas_client, privana):
    from src.services.earn.strategies.midas import MidasStrategy

    with patch("src.services.earn.strategies.midas.load_settings", return_value=_strategy_settings()):
        return MidasStrategy(
            client=midas_client,
            asset_address=ASSET_ADDRESS,
            token_id=TOKEN_ID,
            privana_client=privana,
            poll_interval_sec=0,
        )


def test_name(strategy) -> None:
    assert strategy.name == "midas-mtbill"


def test_asset_address_is_retained(strategy) -> None:
    assert strategy.asset_address == ASSET_ADDRESS


def test_token_id_is_retained(strategy) -> None:
    assert strategy.token_id == TOKEN_ID


def test_pool_address_defaults_to_lp_address(strategy) -> None:
    assert strategy.pool_address == POOL_ADDRESS


def test_unsupported_chain_id_rejected(midas_client, privana) -> None:
    from src.services.earn.strategies.midas import MidasStrategy

    bogus = replace(
        load_settings(),
        liquidity_provider_secret_key=LP_PRIVATE_KEY,
        liquidity_provider_address=POOL_ADDRESS,
        accounting_contract_address=ACCOUNTING_CONTRACT,
        accounting_chain_id=1,
    )
    with patch("src.services.earn.strategies.midas.load_settings", return_value=bogus):
        with pytest.raises(ValueError, match="unsupported accounting chain_id"):
            MidasStrategy(
                client=midas_client,
                asset_address=ASSET_ADDRESS,
                token_id=TOKEN_ID,
                privana_client=privana,
            )


@pytest.mark.asyncio
async def test_get_apy_bps_returns_configured_default(strategy) -> None:
    assert await strategy.get_apy_bps() == 350


@pytest.mark.asyncio
async def test_get_apy_bps_reads_from_settings(midas_client, privana) -> None:
    from src.services.earn.strategies.midas import MidasStrategy

    with patch(
        "src.services.earn.strategies.midas.load_settings",
        return_value=_strategy_settings(apy_bps=525),
    ):
        s = MidasStrategy(
            client=midas_client,
            asset_address=ASSET_ADDRESS,
            token_id=TOKEN_ID,
            privana_client=privana,
            poll_interval_sec=0,
        )

    assert await s.get_apy_bps() == 525


class TestConvertUsdcToMtbillAmount:
    """Defines the contract for MidasStrategy.convert_usdc_to_mtbill_amount.

    Each case is hand-verifiable. The formula must produce these exact
    integer outputs for the given (usdc, price, decimals) inputs.
    """

    @pytest.mark.parametrize(
        "usdc_amount, oracle_price, oracle_decimals, expected_mtbill",
        [
            # ─── Price = $1.00 per mTBILL at varying oracle decimal precisions ───
            (1_000_000, 10**18, 18, 10**18),                  # 1 USDC -> 1 mTBILL @ 18d
            (1_000_000, 10**12, 12, 10**18),                  # same @ 12d
            (1_000_000, 10**8, 8, 10**18),                    # same @ 8d (Chainlink std)
            (10_000_000, 10**18, 18, 10 * 10**18),            # 10 USDC -> 10 mTBILL
            (6_106_564, 10**18, 18, 6_106_564 * 10**12),      # arbitrary clean scale-up
            # ─── Price = $2.00 per mTBILL (mTBILL > USD) ───
            (2_000_000, 2 * 10**18, 18, 10**18),              # 2 USDC -> 1 mTBILL
            (1_000_000, 2 * 10**18, 18, 5 * 10**17),          # 1 USDC -> 0.5 mTBILL
            # ─── Price = $0.50 per mTBILL (mTBILL < USD; hypothetical) ───
            (1_000_000, 5 * 10**17, 18, 2 * 10**18),          # 1 USDC -> 2 mTBILL
            # ─── Boundary: zero in, zero out ───
            (0, 10**18, 18, 0),
            (0, 5 * 10**17, 18, 0),
            # ─── Integer division rounds toward zero (Python //) ───
            (1, 3 * 10**18, 18, 333_333_333_333),             # 1e30 / 3e18 = 3.33...e11 -> floor
        ],
        ids=[
            "price_1.00_decimals_18",
            "price_1.00_decimals_12",
            "price_1.00_decimals_8",
            "price_1.00_amount_10",
            "price_1.00_amount_6.106564",
            "price_2.00_amount_2_to_1mtbill",
            "price_2.00_amount_1_to_half_mtbill",
            "price_0.50_amount_1_to_2mtbill",
            "zero_usdc_at_1.00",
            "zero_usdc_at_0.50",
            "integer_division_rounds_down",
        ],
    )
    def test_convert(self, usdc_amount, oracle_price, oracle_decimals, expected_mtbill):
        from src.services.earn.strategies.midas import MidasStrategy

        result = MidasStrategy.convert_usdc_to_mtbill_amount(
            usdc_amount, oracle_price, oracle_decimals,
        )
        assert result == expected_mtbill, (
            f"convert_usdc_to_mtbill_amount({usdc_amount}, {oracle_price}, {oracle_decimals}) "
            f"returned {result}, expected {expected_mtbill}"
        )


class TestConvertMtbillToUsdcAmount:
    """Mirror cases for convert_mtbill_to_usdc_amount (which is fully
    implemented by the strategy). Validates the AUM-read direction.
    """

    @pytest.mark.parametrize(
        "mtbill_amount, oracle_price, oracle_decimals, expected_usdc",
        [
            (10**18, 10**18, 18, 1_000_000),                  # 1 mTBILL @ $1 -> 1 USDC
            (10**18, 10**12, 12, 1_000_000),
            (10**18, 10**8, 8, 1_000_000),
            (10**18, 2 * 10**18, 18, 2_000_000),              # 1 mTBILL @ $2 -> 2 USDC
            (5 * 10**17, 2 * 10**18, 18, 1_000_000),          # 0.5 mTBILL @ $2 -> 1 USDC
            (10**18, 5 * 10**17, 18, 500_000),                # 1 mTBILL @ $0.50 -> 0.5 USDC
            (0, 10**18, 18, 0),
        ],
    )
    def test_convert(self, mtbill_amount, oracle_price, oracle_decimals, expected_usdc):
        from src.services.earn.strategies.midas import MidasStrategy

        result = MidasStrategy.convert_mtbill_to_usdc_amount(
            mtbill_amount, oracle_price, oracle_decimals,
        )
        assert result == expected_usdc


@pytest.mark.parametrize(
    "usdc_amount, oracle_price, oracle_decimals",
    [
        (1_000_000, 10**18, 18),
        (6_106_564, 10**18, 18),
        (1_000_000, 2 * 10**18, 18),
        (1_000_000, 10**8, 8),
    ],
)
def test_round_trip_convert_within_one_unit(usdc_amount, oracle_price, oracle_decimals):
    """convert_mtbill_to_usdc_amount(convert_usdc_to_mtbill_amount(x)) == x
    modulo integer-division rounding. Ensures the two helpers are inverses.
    """
    from src.services.earn.strategies.midas import MidasStrategy

    mtbill = MidasStrategy.convert_usdc_to_mtbill_amount(usdc_amount, oracle_price, oracle_decimals)
    recovered = MidasStrategy.convert_mtbill_to_usdc_amount(mtbill, oracle_price, oracle_decimals)
    assert abs(recovered - usdc_amount) <= 1


@pytest.mark.asyncio
async def test_total_assets_zero_when_no_mtbill(strategy, midas_client) -> None:
    midas_client.get_mtbill_balance.return_value = 0

    assert await strategy.total_assets() == 0
    midas_client.get_oracle_answer.assert_not_called()


@pytest.mark.asyncio
async def test_total_assets_converts_via_oracle(strategy, midas_client) -> None:
    midas_client.get_mtbill_balance.return_value = 2 * 10**18
    midas_client.get_oracle_answer.return_value = 2 * 10**18
    midas_client.get_oracle_decimals.return_value = 18

    assert await strategy.total_assets() == 4_000_000


@pytest.mark.asyncio
async def test_is_healthy_returns_true_for_fresh_unpaused(strategy, midas_client) -> None:
    import time as _time

    midas_client.is_issuance_paused.return_value = False
    midas_client.is_redemption_paused.return_value = False
    midas_client.get_oracle_round.return_value = (10**18, int(_time.time()))

    assert await strategy.is_healthy() is True


@pytest.mark.asyncio
async def test_is_healthy_false_when_issuance_paused(strategy, midas_client) -> None:
    midas_client.is_issuance_paused.return_value = True

    assert await strategy.is_healthy() is False


@pytest.mark.asyncio
async def test_is_healthy_false_when_redemption_paused(strategy, midas_client) -> None:
    midas_client.is_issuance_paused.return_value = False
    midas_client.is_redemption_paused.return_value = True

    assert await strategy.is_healthy() is False


@pytest.mark.asyncio
async def test_is_healthy_false_when_oracle_stale(strategy, midas_client) -> None:
    import time as _time

    midas_client.is_issuance_paused.return_value = False
    midas_client.is_redemption_paused.return_value = False
    midas_client.get_oracle_round.return_value = (10**18, int(_time.time()) - 10**7)

    assert await strategy.is_healthy() is False


@pytest.mark.asyncio
async def test_is_healthy_false_on_rpc_error(strategy, midas_client) -> None:
    midas_client.is_issuance_paused.side_effect = RuntimeError("rpc dead")

    assert await strategy.is_healthy() is False


@pytest.mark.asyncio
async def test_deposit_to_earn_rejects_non_positive_amount(strategy) -> None:
    with pytest.raises(ValueError, match="positive amount"):
        await strategy.deposit_to_earn(0)
    with pytest.raises(ValueError, match="positive amount"):
        await strategy.deposit_to_earn(-1)


@pytest.mark.asyncio
async def test_deposit_to_earn_bridges_approves_and_mints(
    strategy, midas_client, privana,
) -> None:
    privana.get_pending_withdrawals.side_effect = [
        _PendingWithdrawalsResponse(user_address=POOL_ADDRESS, pending_withdrawals=[]),
        _PendingWithdrawalsResponse(
            user_address=POOL_ADDRESS,
            pending_withdrawals=[_PendingWithdrawal(index=42, amount=1_000_000)],
        ),
    ]
    privana.get_withdrawal_info.return_value = _WithdrawalInfo(
        index=42, resolved=True, tx_identifier="0xresolved",
    )
    midas_client.get_allowance.return_value = 0
    midas_client.get_oracle_answer.return_value = 10**18
    midas_client.get_oracle_decimals.return_value = 18

    await strategy.deposit_to_earn(1_000_000)

    midas_client.approve.assert_called_once()
    approve_args = midas_client.approve.call_args.args
    assert approve_args[0] == ASSET_ADDRESS
    assert approve_args[1] == ISSUANCE_VAULT
    assert approve_args[2] == 1_000_000

    midas_client.deposit_instant.assert_called_once()
    deposit_args = midas_client.deposit_instant.call_args.args
    assert deposit_args[0] == ASSET_ADDRESS
    assert deposit_args[1] == 1_000_000
    # min_receive = expected_mtbill * (10000 - 50) / 10000 (default slippage 50 bps)
    # At price=1.0 with decimals=18, expected_mtbill = 10**18 for 1 USDC.
    # min_receive = 10**18 * 9950 / 10000 = 995_000_000_000_000_000
    assert deposit_args[2] == 995_000_000_000_000_000


@pytest.mark.asyncio
async def test_deposit_to_earn_skips_approve_when_allowance_sufficient(
    strategy, midas_client, privana,
) -> None:
    privana.get_pending_withdrawals.side_effect = [
        _PendingWithdrawalsResponse(user_address=POOL_ADDRESS, pending_withdrawals=[]),
        _PendingWithdrawalsResponse(
            user_address=POOL_ADDRESS,
            pending_withdrawals=[_PendingWithdrawal(index=1, amount=500_000)],
        ),
    ]
    privana.get_withdrawal_info.return_value = _WithdrawalInfo(index=1, resolved=True)
    midas_client.get_allowance.return_value = 10**12

    await strategy.deposit_to_earn(500_000)

    midas_client.approve.assert_not_called()
    midas_client.deposit_instant.assert_called_once()


@pytest.mark.asyncio
async def test_withdraw_from_earn_rejects_non_positive_amount(strategy) -> None:
    with pytest.raises(ValueError, match="positive amount"):
        await strategy.withdraw_from_earn(0)


@pytest.mark.asyncio
async def test_withdraw_from_earn_redeems_forwards_and_polls(
    strategy, midas_client, privana,
) -> None:
    privana.get_balance.side_effect = [
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=0),
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=1_002_300),
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=1_002_300),
    ]
    # Realized USDC out (1_002_300) differs from the requested amount: the fee
    # buffer over-redeems, so the LP EOA receives slightly more than target.
    midas_client.get_erc20_balance.side_effect = [0, 1_002_300]
    midas_client.get_oracle_answer.return_value = 10**18
    midas_client.get_oracle_decimals.return_value = 18
    midas_client.get_redemption_instant_fee_bps.return_value = 25

    await strategy.withdraw_from_earn(1_000_000)

    midas_client.redeem_instant.assert_called_once()
    redeem_args = midas_client.redeem_instant.call_args.args
    assert redeem_args[0] == ASSET_ADDRESS
    # baseline_mtbill at price=1.0 is 10**18; with 25bps fee buffer:
    # mtbill_to_redeem = 10**18 * 10025 / 10000 = 1_002_500_000_000_000_000
    assert redeem_args[1] == 1_002_500_000_000_000_000
    # min_receive_usdc = 1_000_000 * 9950 / 10000 = 995_000
    assert redeem_args[2] == 995_000

    # The realized USDC delta is forwarded, not the requested target amount.
    midas_client.transfer_erc20.assert_called_once_with(
        ASSET_ADDRESS, DEPOSIT_ADDRESS_BASE, 1_002_300,
    )
    privana.check_deposit.assert_awaited_once()
    assert privana.check_deposit.await_args.args[0].amount == 1_002_300
    assert privana.get_balance.await_count >= 2


@pytest.mark.asyncio
async def test_withdraw_from_earn_raises_typed_error_on_revert(
    strategy, midas_client, privana,
) -> None:
    from src.services.earn.strategies.midas import MidasInstantUnavailableError

    privana.get_balance.return_value = _Balance(
        user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=0,
    )
    midas_client.redeem_instant.side_effect = RuntimeError("tx reverted (daily limit)")

    with pytest.raises(MidasInstantUnavailableError, match="redeemInstant unavailable"):
        await strategy.withdraw_from_earn(1_000_000)

    midas_client.transfer_erc20.assert_not_called()
    privana.check_deposit.assert_not_called()


@pytest.mark.asyncio
async def test_withdraw_from_earn_raises_when_no_usdc_realized(
    strategy, midas_client, privana,
) -> None:
    from src.services.earn.strategies.midas import MidasInstantUnavailableError

    privana.get_balance.return_value = _Balance(
        user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=0,
    )
    midas_client.get_erc20_balance.side_effect = [500_000, 500_000]

    with pytest.raises(MidasInstantUnavailableError, match="produced no USDC"):
        await strategy.withdraw_from_earn(1_000_000)

    midas_client.transfer_erc20.assert_not_called()
    privana.check_deposit.assert_not_called()


@pytest.mark.asyncio
async def test_withdraw_from_earn_continues_when_check_deposit_errors(
    strategy, midas_client, privana,
) -> None:
    privana.get_balance.side_effect = [
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=0),
        _Balance(user_address=POOL_ADDRESS, token_id=TOKEN_ID, balance=1_000_000),
    ]
    midas_client.get_erc20_balance.side_effect = [0, 1_000_000]
    privana.check_deposit.return_value = _DepositCheckResponse(
        status="error", detail="relay temporarily unreachable",
    )

    await strategy.withdraw_from_earn(1_000_000)

    privana.check_deposit.assert_awaited_once()
    assert privana.get_balance.await_count >= 2


@pytest.mark.asyncio
async def test_bridge_propagates_request_failure(
    strategy, midas_client, privana,
) -> None:
    privana.request_withdrawal.side_effect = RuntimeError("rejected by accounting")

    with pytest.raises(RuntimeError, match="rejected by accounting"):
        await strategy.deposit_to_earn(1_000_000)

    midas_client.deposit_instant.assert_not_called()
