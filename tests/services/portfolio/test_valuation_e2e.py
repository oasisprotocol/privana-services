import json
from pathlib import Path

from src.clients.coingecko import PricePoint
from src.models.common import HistoryEntry
from src.services.portfolio.reconstruction import replay_history
from src.services.portfolio.valuation import (
    EarnSeriesValues,
    compose_portfolio,
    sample_grid,
    value_buckets,
)
from src.services.price_history import SAMPLE_INTERVAL_SEC

FIXTURES = Path(__file__).parent / "fixtures"

USDC = "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514"
WETH = "0x335b5cccd1e63b2fe79863a0db73fce430e4e66902e2b78424f8662621e29fb7"
USER = "0xd8991364507FAfC256EafF950d28618735753476"
DECIMALS = {USDC: 6, WETH: 18}


def _entries():
    payload = json.loads((FIXTURES / "lifecycle_history.json").read_text())
    return [HistoryEntry(**entry) for entry in payload["history"]]


def _flat_prices(grid, usdc_e8=100_000_000, weth_e8=3_000 * 10**8):
    return {
        USDC: [PricePoint(timestamp=ts, price_e8=usdc_e8) for ts in grid],
        WETH: [PricePoint(timestamp=ts, price_e8=weth_e8) for ts in grid],
    }


class TestLifecycleToPortfolioSeries:
    def test_full_pipeline_produces_a_consistent_series(self):
        series_by_token = replay_history(_entries(), USER)
        grid = sample_grid(1786000000, 1786060000 + SAMPLE_INTERVAL_SEC)
        prices = _flat_prices(grid)

        portfolio = compose_portfolio(
            value_buckets(series_by_token, prices, DECIMALS, grid),
            EarnSeriesValues({}),
        )

        assert len(portfolio) == len(grid)
        assert all(
            p.total_e8 == p.available_e8 + p.locked_e8 + p.earn_e8 for p in portfolio
        )

        final = portfolio[-1]
        assert final.available_e8 == 5 * 10**8 + 7 * 3_000 * 10**8 // 10
        assert final.locked_e8 == 0

    def test_locked_bucket_appears_while_the_lock_is_open(self):
        series_by_token = replay_history(_entries(), USER)
        grid = sample_grid(1786000000, 1786060000 + SAMPLE_INTERVAL_SEC)
        prices = _flat_prices(grid)

        portfolio = compose_portfolio(
            value_buckets(series_by_token, prices, DECIMALS, grid),
            EarnSeriesValues({}),
        )

        locked_values = {p.timestamp: p.locked_e8 for p in portfolio}
        lock_open_ts = 1786010000 - (1786010000 % SAMPLE_INTERVAL_SEC) + SAMPLE_INTERVAL_SEC
        lock_closed_ts = 1786040000 - (1786040000 % SAMPLE_INTERVAL_SEC) + SAMPLE_INTERVAL_SEC

        assert locked_values[lock_open_ts] == 4 * 10**8
        assert locked_values[lock_closed_ts] == 0
