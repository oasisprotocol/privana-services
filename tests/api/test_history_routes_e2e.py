import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from web3 import Web3

from src.clients.accounting import JwtIdentity
from src.clients.coingecko import PricePoint
from src.models.common import HistoryEntry, TokenInfo
from src.services.price_history import DAY_SEC, SAMPLE_INTERVAL_SEC, store_points

FIXTURES = Path(__file__).parent.parent / "services" / "portfolio" / "fixtures"

USDC = "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514"
USER = Web3.to_checksum_address("0xd8991364507FAfC256EafF950d28618735753476")
LAST_EVENT = 1786060000
NOW = LAST_EVENT + DAY_SEC


def _entries() -> list[HistoryEntry]:
    payload = json.loads((FIXTURES / "lifecycle_history.json").read_text())
    return [HistoryEntry(**entry) for entry in payload["history"]]


def _token_info() -> TokenInfo:
    return TokenInfo(
        token_id=USDC,
        token_type=1,
        token_type_name="ERC20",
        data="0x",
        decimals=6,
        symbol="USDC",
    )


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.coingecko_token_ids = json.dumps({USDC: "usd-coin"})
    return settings


def _accounting() -> MagicMock:
    client = MagicMock()
    client.get_user_history = AsyncMock(return_value=_entries())
    client.get_token_info = AsyncMock(return_value=_token_info())
    client.get_jwt_identity = AsyncMock(
        return_value=JwtIdentity(siwe_token="0x" + "ee" * 32, address=USER)
    )
    return client


class TestPortfolioHistoryEndToEnd:
    """Real replay and valuation behind the endpoint; only the accounting HTTP
    reads and the clock are stubbed."""

    async def test_lifecycle_history_becomes_a_priced_series(self, api_client, test_db):
        store_points(
            "usd-coin",
            [PricePoint(timestamp=LAST_EVENT - 30 * DAY_SEC, price_e8=10**8)],
        )
        accounting = _accounting()

        with (
            patch("src.api._auth.get_accounting_client", return_value=accounting),
            patch(
                "src.services.portfolio.history_service.get_accounting_client",
                return_value=accounting,
            ),
            patch(
                "src.services.price_history.load_settings", return_value=_settings()
            ),
            patch(
                "src.services.portfolio.history_service.time.time", return_value=NOW
            ),
        ):
            r = await api_client.get(
                "/v1/portfolio/history",
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.status_code == 200
        points = r.json()["points"]
        assert points

        # The fixture nets out to 5 USDC available and nothing locked, and the
        # WETH leg has no configured price so it drops out of the total.
        assert points[-1]["available_usd"] == "5.00000000"
        assert points[-1]["locked_usd"] == "0.00000000"
        assert points[-1]["earn_usd"] == "0.00000000"
        assert points[-1]["total_usd"] == "5.00000000"

        # The lock opens at 1786010000 and closes at 1786040000, so somewhere
        # in between the locked slice has to be non-zero.
        assert any(p["locked_usd"] != "0.00000000" for p in points)

    async def test_range_window_trims_the_series(self, api_client, test_db):
        store_points(
            "usd-coin",
            [PricePoint(timestamp=LAST_EVENT - 30 * DAY_SEC, price_e8=10**8)],
        )
        accounting = _accounting()

        with (
            patch("src.api._auth.get_accounting_client", return_value=accounting),
            patch(
                "src.services.portfolio.history_service.get_accounting_client",
                return_value=accounting,
            ),
            patch(
                "src.services.price_history.load_settings", return_value=_settings()
            ),
            patch(
                "src.services.portfolio.history_service.time.time", return_value=NOW
            ),
        ):
            windowed = await api_client.get(
                "/v1/portfolio/history?days=1",
                headers={"Authorization": "Bearer user-jwt"},
            )
            everything = await api_client.get(
                "/v1/portfolio/history",
                headers={"Authorization": "Bearer user-jwt"},
            )

        points = windowed.json()["points"]
        # The window opens on the sampling slot at or before now - 1 day.
        assert NOW - DAY_SEC - SAMPLE_INTERVAL_SEC < points[0]["timestamp"]
        assert points[0]["timestamp"] <= NOW - DAY_SEC
        assert len(points) < len(everything.json()["points"])
        # Both ranges end on the same settled balance.
        assert points[-1]["total_usd"] == "5.00000000"
        assert everything.json()["points"][-1]["total_usd"] == "5.00000000"


class TestEarnHistoryEndToEnd:
    async def test_user_without_earn_rows_gets_an_empty_series(self, api_client, test_db):
        accounting = _accounting()

        with patch("src.api._auth.get_accounting_client", return_value=accounting):
            r = await api_client.get(
                "/v1/earn/history",
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.status_code == 200
        assert r.json() == {"points": []}
