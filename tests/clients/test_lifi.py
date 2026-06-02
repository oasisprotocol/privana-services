from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.core.config import load_settings


SAMPLE_ROUTES_RESPONSE = {
    "routes": [
        {
            "toAmount": "2000000000000000000",
            "toAmountMin": "1950000000000000000",
            "steps": [{"tool": "uniswap"}],
        }
    ]
}


class TestLiFiClient:
    @pytest.fixture
    def mock_http_client(self):
        return AsyncMock(spec=httpx.AsyncClient)

    @pytest.fixture
    def client(self, mock_http_client):
        with patch("src.clients.lifi.load_settings") as mock_settings:
            mock_settings.return_value = replace(
                load_settings(),
                lifi_api_url="https://li.quest/v1",
                lifi_integrator="test",
                lifi_api_key="test-key",
            )
            from src.clients.lifi import LiFiClient
            lifi = LiFiClient()
            lifi.client = mock_http_client
            return lifi

    def _mock_response(self, data, status_code=200):
        resp = MagicMock()
        resp.json.return_value = data
        resp.status_code = status_code
        resp.text = ""
        resp.raise_for_status.return_value = None
        return resp

    async def test_get_routes_sends_correct_payload(self, client, mock_http_client):
        mock_http_client.post.return_value = self._mock_response(SAMPLE_ROUTES_RESPONSE)

        await client.get_routes(
            from_chain_id=1,
            to_chain_id=1,
            from_token_address="0xfrom",
            to_token_address="0xto",
            from_amount="1000000",
        )

        mock_http_client.post.assert_called_once_with(
            "https://li.quest/v1/advanced/routes",
            json={
                "fromChainId": 1,
                "toChainId": 1,
                "fromTokenAddress": "0xfrom",
                "toTokenAddress": "0xto",
                "fromAmount": "1000000",
            },
        )

    async def test_get_routes_returns_parsed_response(self, client, mock_http_client):
        mock_http_client.post.return_value = self._mock_response(SAMPLE_ROUTES_RESPONSE)

        result = await client.get_routes(
            from_chain_id=1,
            to_chain_id=1,
            from_token_address="0xfrom",
            to_token_address="0xto",
            from_amount="1000000",
        )

        assert result == SAMPLE_ROUTES_RESPONSE
        assert len(result["routes"]) == 1

    async def test_get_routes_raises_on_http_error(self, client, mock_http_client):
        error_resp = self._mock_response({}, status_code=429)
        error_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "rate limited", request=MagicMock(), response=error_resp
        )
        mock_http_client.post.return_value = error_resp

        with pytest.raises(httpx.HTTPStatusError):
            await client.get_routes(
                from_chain_id=1,
                to_chain_id=1,
                from_token_address="0xfrom",
                to_token_address="0xto",
                from_amount="1000000",
            )

    async def test_get_tokens_calls_correct_endpoint(self, client, mock_http_client):
        mock_http_client.get.return_value = self._mock_response({"tokens": {}})

        result = await client.get_tokens()

        mock_http_client.get.assert_called_once_with("https://li.quest/v1/tokens")
        assert result == {"tokens": {}}

    async def test_get_chains_calls_correct_endpoint(self, client, mock_http_client):
        mock_http_client.get.return_value = self._mock_response({"chains": []})

        result = await client.get_chains()

        mock_http_client.get.assert_called_once_with("https://li.quest/v1/chains")
        assert result == {"chains": []}

    def test_sets_api_key_header(self):
        with patch("src.clients.lifi.load_settings") as mock_settings:
            mock_settings.return_value = replace(
                load_settings(),
                lifi_api_url="https://li.quest/v1",
                lifi_api_key="my-secret-key",
            )
            from src.clients.lifi import LiFiClient
            lifi = LiFiClient()
            assert lifi.client.headers.get("x-lifi-api-key") == "my-secret-key"

    def test_strips_trailing_slash_from_url(self):
        with patch("src.clients.lifi.load_settings") as mock_settings:
            mock_settings.return_value = replace(
                load_settings(),
                lifi_api_url="https://li.quest/v1/",
            )
            from src.clients.lifi import LiFiClient
            lifi = LiFiClient()
            assert lifi.api_url == "https://li.quest/v1"
