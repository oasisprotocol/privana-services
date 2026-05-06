import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.models.common import Balance, TokenInfo


SAMPLE_TOKEN_NATIVE = {
    "token_id": "0x0000000000000000000000000000000000000000000000000000000000014a34",
    "token_type": 0,
    "token_type_name": "NativeEVM",
    "data": "0x0000000000000000000000000000000000000000000000000000000000014a34",
    "chain_id": 84532,
    "chain_name": "Base Sepolia",
}

SAMPLE_TOKEN_ERC20 = {
    "token_id": "0xabc123def456abc123def456abc123def456abc123def456abc123def456abc1",
    "token_type": 1,
    "token_type_name": "ERC20",
    "data": "0x0000000000000000000000000000000000000000000000000000000000014a34abcdef1234567890abcdef1234567890abcdef12",
    "chain_id": 84532,
    "chain_name": "Base Sepolia",
    "token_address": "0xAbCdEf1234567890AbCdEf1234567890AbCdEf12",
}

SAMPLE_BALANCE = {
    "user_address": "0x1234567890abcdef1234567890abcdef12345678",
    "token_id": "0x0000000000000000000000000000000000000000000000000000000000014a34",
    "balance": "1000000000000000000",
    "token_symbol": "ETH",
    "chain_id": "84532",
}


class TestTokenInfoModel:
    def test_parse_native_token(self):
        info = TokenInfo(**SAMPLE_TOKEN_NATIVE)
        assert info.token_type == 0
        assert info.token_type_name == "NativeEVM"
        assert info.chain_id == 84532
        assert info.token_address is None

    def test_parse_erc20_token(self):
        info = TokenInfo(**SAMPLE_TOKEN_ERC20)
        assert info.token_type == 1
        assert info.token_type_name == "ERC20"
        assert info.chain_id == 84532
        assert info.token_address == "0xAbCdEf1234567890AbCdEf1234567890AbCdEf12"

    def test_missing_optional_fields(self):
        minimal = {
            "token_id": "0x01",
            "token_type": 0,
            "token_type_name": "NativeEVM",
            "data": "0x00",
        }
        info = TokenInfo(**minimal)
        assert info.chain_id is None
        assert info.chain_name is None
        assert info.token_address is None


class TestBalanceModel:
    def test_parse_balance(self):
        bal = Balance(**SAMPLE_BALANCE)
        assert bal.balance == "1000000000000000000"
        assert bal.token_symbol == "ETH"
        assert bal.chain_id == "84532"

    def test_missing_optional_fields(self):
        minimal = {
            "user_address": "0x1234",
            "token_id": "0x01",
            "balance": "0",
        }
        bal = Balance(**minimal)
        assert bal.token_symbol is None
        assert bal.chain_id is None


class TestAccountingClient:
    @pytest.fixture
    def mock_http_client(self):
        return AsyncMock(spec=httpx.AsyncClient)

    @pytest.fixture
    def client(self, mock_http_client):
        with patch("src.clients.accounting.load_settings") as mock_settings:
            from src.models.settings import Settings
            mock_settings.return_value = Settings(
                accounting_api_base_url="http://test:8000",
                liquidity_provider_secret_key="0x4c0883a69102937d6231471b5dbb6204fe512961708279f69e0f0fcbf24b5830",
                liquidity_provider_address="0x2c7536E3605D9C16a7a3D7b1898e529396a65c23",
            )
            from src.clients.accounting import AccountingClient
            acct = AccountingClient()
            acct.client = mock_http_client
            return acct

    def _mock_response(self, data):
        resp = MagicMock()
        resp.json.return_value = data
        resp.raise_for_status.return_value = None
        return resp

    async def test_get_token_info_returns_token_info(self, client, mock_http_client):
        mock_http_client.request.return_value = self._mock_response(SAMPLE_TOKEN_ERC20)

        result = await client.get_token_info("0xabc123")
        assert isinstance(result, TokenInfo)
        assert result.token_type == 1
        assert result.token_type_name == "ERC20"
        mock_http_client.request.assert_called_once_with(
            "GET", "http://test:8000/v1/accounting/tokens/0xabc123"
        )

    async def test_get_transfer_nonce_returns_int(self, client, mock_http_client):
        mock_http_client.request.return_value = self._mock_response({"nonce": 42})

        result = await client.get_transfer_nonce("0xuser")
        assert result == 42
        assert isinstance(result, int)
        mock_http_client.request.assert_called_once_with(
            "GET", "http://test:8000/v1/accounting/funds/transfer/nonce/0xuser"
        )

    async def test_get_lp_balance_returns_balance(self, client, mock_http_client):
        import asyncio
        client._siwe_token = "test-siwe"
        client._jwt_token = "test-jwt"
        client._auth_timestamp = asyncio.get_event_loop().time()
        mock_http_client.request.return_value = self._mock_response(SAMPLE_BALANCE)

        result = await client.get_lp_balance("0xtoken")
        assert isinstance(result, Balance)
        assert result.balance == "1000000000000000000"
        mock_http_client.request.assert_called_once_with(
            "GET", "http://test:8000/v1/accounting/balances/0x2c7536E3605D9C16a7a3D7b1898e529396a65c23/0xtoken",
            headers={"X-SIWE-Token": "test-siwe", "Authorization": "Bearer test-jwt"},
        )
