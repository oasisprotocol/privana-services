from unittest.mock import MagicMock, patch

import pytest
from web3 import Web3

import src.clients.sapphire as sapphire_module

LP_KEY = "0x" + "11" * 32
ADMIN_KEY = "0x" + "22" * 32


def _settings(pool_admin_key: str = "") -> MagicMock:
    settings = MagicMock()
    settings.liquidity_provider_secret_key = LP_KEY
    settings.pool_admin_secret_key = pool_admin_key
    return settings


@pytest.fixture(autouse=True)
def reset_singletons():
    sapphire_module._client_instance = None
    sapphire_module._pool_admin_client_instance = None
    yield
    sapphire_module._client_instance = None
    sapphire_module._pool_admin_client_instance = None


class TestSapphireHttpProvider:
    def test_no_headers_keeps_web3_defaults(self):
        provider = sapphire_module.sapphire_http_provider("http://localhost:1", {})
        headers = provider.get_request_kwargs()["headers"]
        assert headers["Content-Type"] == "application/json"

    def test_extra_headers_are_merged_over_defaults(self):
        provider = sapphire_module.sapphire_http_provider(
            "http://localhost:1", {"Authorization": "Bearer secret-token"}
        )
        headers = provider.get_request_kwargs()["headers"]
        assert headers["Authorization"] == "Bearer secret-token"
        # Custom request_kwargs replace web3's defaults, so the helper must
        # merge Content-Type back in or JSON-RPC endpoints reject the request.
        assert headers["Content-Type"] == "application/json"

    def test_returns_a_plain_http_provider(self):
        provider = sapphire_module.sapphire_http_provider("http://localhost:1", {})
        assert isinstance(provider, Web3.HTTPProvider)


class TestGetPoolAdminSapphireClient:
    def test_falls_back_to_lp_client_when_admin_key_unset(self):
        lp_client = MagicMock()
        with patch.object(
            sapphire_module, "load_settings", return_value=_settings("")
        ), patch.object(
            sapphire_module, "get_sapphire_client", return_value=lp_client
        ):
            assert sapphire_module.get_pool_admin_sapphire_client() is lp_client

    def test_shares_lp_client_when_keys_match(self):
        lp_client = MagicMock()
        with patch.object(
            sapphire_module, "load_settings", return_value=_settings(LP_KEY)
        ), patch.object(
            sapphire_module, "get_sapphire_client", return_value=lp_client
        ):
            assert sapphire_module.get_pool_admin_sapphire_client() is lp_client

    def test_builds_separate_client_when_admin_key_differs(self):
        with patch.object(
            sapphire_module, "load_settings", return_value=_settings(ADMIN_KEY)
        ), patch.object(sapphire_module, "SapphireClient") as client_cls:
            client = sapphire_module.get_pool_admin_sapphire_client()
            client_cls.assert_called_once_with(secret_key=ADMIN_KEY)
            assert client is client_cls.return_value

    def test_reuses_the_admin_client_singleton(self):
        with patch.object(
            sapphire_module, "load_settings", return_value=_settings(ADMIN_KEY)
        ), patch.object(sapphire_module, "SapphireClient") as client_cls:
            first = sapphire_module.get_pool_admin_sapphire_client()
            second = sapphire_module.get_pool_admin_sapphire_client()
            assert first is second
            client_cls.assert_called_once()
