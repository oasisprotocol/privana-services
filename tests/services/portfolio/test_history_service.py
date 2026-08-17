from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from src.clients.accounting import JwtIdentity
from src.clients.coingecko import PricePoint
from src.models.common import HistoryEntry, TokenInfo
from src.services.earn.value_history import EarnCashflow
from src.services.portfolio.history_service import (
    DAY_SEC,
    FINE_GRAIN_DAYS,
    MAX_HISTORY_DAYS,
    SAMPLE_INTERVAL_SEC,
    _window,
    earn_history,
    portfolio_history,
)

USDC = "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514"
USER = "0xd8991364507FAfC256EafF950d28618735753476"
POOL = "0x" + "33" * 32
NOW = 1786060000 - (1786060000 % DAY_SEC)

IDENTITY = JwtIdentity(siwe_token="0x" + "ee" * 32, address=USER)


def _token_info(decimals: int = 6) -> TokenInfo:
    return TokenInfo(
        token_id=USDC,
        token_type=1,
        token_type_name="ERC20",
        data="0x",
        chain_id=84532,
        chain_name="Base Sepolia",
        token_address="0x8eEDCff0b07609Cfb5e2775dFf21EDbACc30D0df",
        symbol="USDC",
        name="USD Coin",
        decimals=decimals,
    )


def _deposit(timestamp: int, amount: str = "10000000") -> HistoryEntry:
    return HistoryEntry(
        kind="deposit",
        timestamp=timestamp,
        token_id=USDC,
        amount=amount,
        counterparty=None,
        deposit_id="0x11",
        chain_id=84532,
    )


def _accounting(entries, token_info=None):
    client = MagicMock()
    client.get_user_history = AsyncMock(return_value=entries)
    client.get_token_info = AsyncMock(return_value=token_info or _token_info())
    return client


class _StubEarn:
    def __init__(self, value_e8: int = 0):
        self._value_e8 = value_e8

    def earn_value_e8(self, timestamp: int) -> int:
        return self._value_e8


@contextmanager
def _patched(accounting, flows=(), earn_value_e8=0):
    module = "src.services.portfolio.history_service"
    with (
        patch(f"{module}.get_accounting_client", return_value=accounting),
        patch(f"{module}.read_user_earn_cashflows", return_value=list(flows)),
        patch(
            f"{module}.price_series_for_tokens",
            return_value={USDC: [PricePoint(timestamp=NOW - 400 * DAY_SEC, price_e8=10**8)]},
        ),
        patch(
            f"{module}.EarnSeriesValues.load",
            AsyncMock(return_value=_StubEarn(earn_value_e8)),
        ),
        patch(f"{module}.time.time", return_value=NOW),
    ):
        yield


class TestWindow:
    def test_fixed_range_is_anchored_to_now(self):
        grid = _window(earliest_ts=NOW - 400 * DAY_SEC, days=7, now=NOW)

        assert grid[0] == NOW - 7 * DAY_SEC
        assert grid[-1] == NOW

    def test_all_range_starts_at_the_first_event(self):
        grid = _window(earliest_ts=NOW - 3 * DAY_SEC, days=None, now=NOW)

        assert grid[0] == NOW - 3 * DAY_SEC
        assert grid[-1] == NOW

    def test_all_range_is_clamped_to_the_maximum_lookback(self):
        grid = _window(earliest_ts=0, days=None, now=NOW)

        assert grid[0] == NOW - MAX_HISTORY_DAYS * DAY_SEC

    def test_short_range_keeps_the_sampler_resolution(self):
        grid = _window(earliest_ts=NOW - 2 * DAY_SEC, days=None, now=NOW)

        assert grid[1] - grid[0] == SAMPLE_INTERVAL_SEC

    def test_long_range_steps_daily(self):
        grid = _window(earliest_ts=NOW, days=FINE_GRAIN_DAYS + 1, now=NOW)

        assert grid[1] - grid[0] == DAY_SEC
        assert len(grid) == FINE_GRAIN_DAYS + 2

    def test_first_event_in_the_future_yields_no_grid(self):
        assert _window(earliest_ts=NOW + DAY_SEC, days=None, now=NOW) == []


class TestPortfolioHistory:
    async def test_values_replayed_buckets_across_the_window(self):
        accounting = _accounting([_deposit(NOW - 2 * DAY_SEC)])
        with _patched(accounting, earn_value_e8=3 * 10**8):
            series = await portfolio_history(IDENTITY, days=1)

        assert series
        assert all(p.available_e8 == 10 * 10**8 for p in series)
        assert all(p.earn_e8 == 3 * 10**8 for p in series)
        assert all(p.total_e8 == 13 * 10**8 for p in series)
        accounting.get_user_history.assert_awaited_once_with(IDENTITY.siwe_token)

    async def test_user_without_any_activity_gets_an_empty_series(self):
        accounting = _accounting([])
        with _patched(accounting):
            series = await portfolio_history(IDENTITY)

        assert series == []
        accounting.get_token_info.assert_not_awaited()

    async def test_earn_only_user_still_gets_a_series(self):
        accounting = _accounting([])
        flows = [
            EarnCashflow(
                timestamp=NOW - DAY_SEC,
                pool_id=POOL,
                token_id=USDC,
                signed_amount=Decimal("5000000"),
            )
        ]
        with _patched(accounting, flows=flows, earn_value_e8=5 * 10**8):
            series = await portfolio_history(IDENTITY)

        assert series
        assert all(p.available_e8 == 0 for p in series)
        assert all(p.earn_e8 == 5 * 10**8 for p in series)

    async def test_token_the_accounting_service_cannot_describe_is_skipped(self):
        accounting = _accounting([_deposit(NOW - 2 * DAY_SEC)])
        accounting.get_token_info = AsyncMock(side_effect=RuntimeError("token info down"))
        with _patched(accounting):
            series = await portfolio_history(IDENTITY, days=1)

        assert series
        assert all(p.total_e8 == 0 for p in series)

    async def test_long_window_is_sampled_daily(self):
        accounting = _accounting([_deposit(NOW - 200 * DAY_SEC)])
        with _patched(accounting):
            series = await portfolio_history(IDENTITY)

        assert series[1].timestamp - series[0].timestamp == DAY_SEC


class TestEarnHistory:
    async def test_samples_the_earn_value_across_the_window(self):
        accounting = _accounting([])
        flows = [
            EarnCashflow(
                timestamp=NOW - DAY_SEC,
                pool_id=POOL,
                token_id=USDC,
                signed_amount=Decimal("5000000"),
            )
        ]
        with _patched(accounting, flows=flows, earn_value_e8=7 * 10**8):
            series = await earn_history(USER, days=1)

        assert [p.value_e8 for p in series] == [7 * 10**8] * len(series)
        assert series[-1].timestamp == NOW

    async def test_user_without_earn_positions_gets_an_empty_series(self):
        accounting = _accounting([])
        with _patched(accounting):
            assert await earn_history(USER) == []
