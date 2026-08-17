from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from web3 import Web3

from src.clients.accounting import JwtIdentity
from src.services.portfolio.history_service import MAX_HISTORY_DAYS, EarnValueSample
from src.services.portfolio.valuation import PortfolioPoint

USER_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
USER_CHECKSUM = Web3.to_checksum_address(USER_ADDRESS)
SIWE_TOKEN = "0x" + "ee" * 32


def _auth_client(address: str = USER_CHECKSUM) -> MagicMock:
    acct = MagicMock()
    acct.get_jwt_identity = AsyncMock(
        return_value=JwtIdentity(siwe_token=SIWE_TOKEN, address=address)
    )
    return acct


def _portfolio_point(timestamp: int) -> PortfolioPoint:
    return PortfolioPoint(
        timestamp=timestamp,
        total_e8=1_300_000_000,
        available_e8=1_000_000_000,
        locked_e8=200_000_000,
        earn_e8=100_000_000,
    )


class TestPortfolioHistoryRoute:
    async def test_returns_the_series_as_usd_strings(self, api_client):
        history = AsyncMock(return_value=[_portfolio_point(1786000000)])
        with (
            patch("src.api._auth.get_accounting_client", return_value=_auth_client()),
            patch("src.api.portfolio.portfolio_history", history),
        ):
            r = await api_client.get(
                "/v1/portfolio/history",
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.status_code == 200
        assert r.json() == {
            "points": [
                {
                    "timestamp": 1786000000,
                    "total_usd": "13.00000000",
                    "available_usd": "10.00000000",
                    "locked_usd": "2.00000000",
                    "earn_usd": "1.00000000",
                }
            ]
        }

    async def test_passes_the_range_and_caller_through(self, api_client):
        history = AsyncMock(return_value=[])
        with (
            patch("src.api._auth.get_accounting_client", return_value=_auth_client()),
            patch("src.api.portfolio.portfolio_history", history),
        ):
            r = await api_client.get(
                "/v1/portfolio/history?days=30",
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.status_code == 200
        identity, days = history.await_args.args
        assert identity.address == USER_CHECKSUM
        assert identity.siwe_token == SIWE_TOKEN
        assert days == 30

    async def test_user_without_history_gets_an_empty_series(self, api_client):
        with (
            patch("src.api._auth.get_accounting_client", return_value=_auth_client()),
            patch("src.api.portfolio.portfolio_history", AsyncMock(return_value=[])),
        ):
            r = await api_client.get(
                "/v1/portfolio/history",
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.status_code == 200
        assert r.json() == {"points": []}

    async def test_negative_value_keeps_its_sign(self, api_client):
        point = PortfolioPoint(
            timestamp=1786000000,
            total_e8=-50_000_000,
            available_e8=-50_000_000,
            locked_e8=0,
            earn_e8=0,
        )
        with (
            patch("src.api._auth.get_accounting_client", return_value=_auth_client()),
            patch(
                "src.api.portfolio.portfolio_history", AsyncMock(return_value=[point])
            ),
        ):
            r = await api_client.get(
                "/v1/portfolio/history",
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.json()["points"][0]["total_usd"] == "-0.50000000"

    async def test_rejects_missing_auth(self, api_client):
        r = await api_client.get("/v1/portfolio/history")

        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"

    async def test_rejects_siwe_auth(self, api_client):
        r = await api_client.get(
            "/v1/portfolio/history",
            headers={"X-SIWE-Token": SIWE_TOKEN},
        )

        assert r.status_code == 400
        assert r.json()["detail"] == "Use Authorization bearer token; X-SIWE-Token is not accepted"

    async def test_rejects_out_of_range_days(self, api_client):
        for days in (0, MAX_HISTORY_DAYS + 1):
            r = await api_client.get(
                f"/v1/portfolio/history?days={days}",
                headers={"Authorization": "Bearer user-jwt"},
            )

            assert r.status_code == 422

    async def test_maps_accounting_read_failure_to_502(self, api_client):
        failure = AsyncMock(side_effect=httpx.ConnectError("accounting unreachable"))
        with (
            patch("src.api._auth.get_accounting_client", return_value=_auth_client()),
            patch("src.api.portfolio.portfolio_history", failure),
        ):
            r = await api_client.get(
                "/v1/portfolio/history",
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.status_code == 502
        assert r.json()["detail"] == "Failed to read accounting history"

    async def test_maps_unexpected_failure_to_500(self, api_client):
        failure = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch("src.api._auth.get_accounting_client", return_value=_auth_client()),
            patch("src.api.portfolio.portfolio_history", failure),
        ):
            r = await api_client.get(
                "/v1/portfolio/history",
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.status_code == 500
        assert r.json()["detail"] == "Failed to build portfolio history"


class TestEarnHistoryRoute:
    async def test_returns_the_series_as_usd_strings(self, api_client):
        samples = [
            EarnValueSample(timestamp=1786000000, value_e8=250_000_000),
            EarnValueSample(timestamp=1786021600, value_e8=250_500_000),
        ]
        with (
            patch("src.api._auth.get_accounting_client", return_value=_auth_client()),
            patch("src.api.earn.earn_history", AsyncMock(return_value=samples)),
        ):
            r = await api_client.get(
                "/v1/earn/history",
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.status_code == 200
        assert r.json()["points"] == [
            {"timestamp": 1786000000, "value_usd": "2.50000000"},
            {"timestamp": 1786021600, "value_usd": "2.50500000"},
        ]

    async def test_passes_the_range_and_caller_through(self, api_client):
        history = AsyncMock(return_value=[])
        with (
            patch("src.api._auth.get_accounting_client", return_value=_auth_client()),
            patch("src.api.earn.earn_history", history),
        ):
            r = await api_client.get(
                "/v1/earn/history?days=7",
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.status_code == 200
        history.assert_awaited_once_with(USER_CHECKSUM, 7)

    async def test_user_without_positions_gets_an_empty_series(self, api_client):
        with (
            patch("src.api._auth.get_accounting_client", return_value=_auth_client()),
            patch("src.api.earn.earn_history", AsyncMock(return_value=[])),
        ):
            r = await api_client.get(
                "/v1/earn/history",
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.status_code == 200
        assert r.json() == {"points": []}

    async def test_rejects_siwe_auth(self, api_client):
        r = await api_client.get(
            "/v1/earn/history",
            headers={"X-SIWE-Token": SIWE_TOKEN},
        )

        assert r.status_code == 400

    async def test_maps_unexpected_failure_to_500(self, api_client):
        failure = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch("src.api._auth.get_accounting_client", return_value=_auth_client()),
            patch("src.api.earn.earn_history", failure),
        ):
            r = await api_client.get(
                "/v1/earn/history",
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.status_code == 500
        assert r.json()["detail"] == "Failed to build earn history"
