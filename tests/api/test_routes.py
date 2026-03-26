from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.api import QuoteResponse
from src.models.swap import SwapRecord, SwapStatus


USDC_TOKEN_ID = "0x330ba47d00c7ce3018deee017b319fd7cc6473a2ddc9e6eba6ebb4207be15279"
WETH_TOKEN_ID = "0x335b5cccd1e63b2fe79863a0db73fce430e4e66902e2b78424f8662621e29fb7"
USER_ADDRESS = "0x" + "a" * 40

MOCK_QUOTE = QuoteResponse(
    quote_id="test-quote-id",
    from_token_id=USDC_TOKEN_ID,
    to_token_id=WETH_TOKEN_ID,
    from_chain_id=84532,
    to_chain_id=84532,
    from_amount="1000000",
    to_amount_gross="500000000000000",
    to_amount_estimate="495000000000000",
    to_amount_min="490000000000000",
    fee_bps=10,
    fee_amount="5000000000000",
    tool_used="uniswap",
    liquidity_provider="0xlp",
    transfer_nonce=5,
    expires_at=9999999999,
)


class TestQuoteRoute:
    async def test_returns_200_with_valid_params(self, api_client):
        with patch("src.api.routes.get_quote_service") as mock_svc:
            svc = MagicMock()
            svc.get_quote = AsyncMock(return_value=MOCK_QUOTE)
            mock_svc.return_value = svc

            r = await api_client.get("/v1/quote", params={
                "from_token_id": USDC_TOKEN_ID,
                "to_token_id": WETH_TOKEN_ID,
                "from_amount": "1000000",
                "user_address": USER_ADDRESS,
            })

            assert r.status_code == 200
            data = r.json()
            assert data["quote_id"] == "test-quote-id"
            assert data["from_token_id"] == USDC_TOKEN_ID
            assert data["fee_bps"] == 10

    async def test_returns_400_on_value_error(self, api_client):
        with patch("src.api.routes.get_quote_service") as mock_svc:
            svc = MagicMock()
            svc.get_quote = AsyncMock(side_effect=ValueError("Invalid token"))
            mock_svc.return_value = svc

            r = await api_client.get("/v1/quote", params={
                "from_token_id": USDC_TOKEN_ID,
                "to_token_id": WETH_TOKEN_ID,
                "from_amount": "1000000",
                "user_address": USER_ADDRESS,
            })

            assert r.status_code == 400
            assert "Invalid token" in r.json()["detail"]

    async def test_returns_500_on_unexpected_error(self, api_client):
        with patch("src.api.routes.get_quote_service") as mock_svc:
            svc = MagicMock()
            svc.get_quote = AsyncMock(side_effect=RuntimeError("LiFi down"))
            mock_svc.return_value = svc

            r = await api_client.get("/v1/quote", params={
                "from_token_id": USDC_TOKEN_ID,
                "to_token_id": WETH_TOKEN_ID,
                "from_amount": "1000000",
                "user_address": USER_ADDRESS,
            })

            assert r.status_code == 500

    async def test_missing_required_param_returns_422(self, api_client):
        r = await api_client.get("/v1/quote", params={
            "from_token_id": USDC_TOKEN_ID,
        })
        assert r.status_code == 422

    async def test_default_slippage(self, api_client):
        with patch("src.api.routes.get_quote_service") as mock_svc:
            svc = MagicMock()
            svc.get_quote = AsyncMock(return_value=MOCK_QUOTE)
            mock_svc.return_value = svc

            await api_client.get("/v1/quote", params={
                "from_token_id": USDC_TOKEN_ID,
                "to_token_id": WETH_TOKEN_ID,
                "from_amount": "1000000",
                "user_address": USER_ADDRESS,
            })

            call_kwargs = svc.get_quote.call_args.kwargs
            assert call_kwargs["slippage"] == 0.03


class TestSwapRoute:
    def _mock_swap(self, status="completed", tx_hash="0x" + "ff" * 32, error=None):
        return SwapRecord(
            id="swap-123",
            quote_id="quote-123",
            user_address=USER_ADDRESS,
            from_token_id=USDC_TOKEN_ID,
            to_token_id=WETH_TOKEN_ID,
            from_amount="1000000",
            to_amount_estimate="495000000000000",
            status=status,
            swap_tx_hash=tx_hash,
            error=error,
            created_at=1000,
            updated_at=1000,
        )

    async def test_returns_200_on_success(self, api_client):
        with patch("src.api.routes.get_swap_executor") as mock_exec:
            executor = MagicMock()
            executor.execute_swap = AsyncMock(return_value=self._mock_swap())
            mock_exec.return_value = executor

            r = await api_client.post("/v1/swap", json={
                "quote_id": "quote-123",
                "user_address": USER_ADDRESS,
                "input_nonce": 0,
                "input_signature": "0x" + "aa" * 65,
            })

            assert r.status_code == 200
            data = r.json()
            assert data["swap_id"] == "swap-123"
            assert data["status"] == "completed"
            assert data["tx_hash"].startswith("0x")

    async def test_returns_400_on_expired_quote(self, api_client):
        with patch("src.api.routes.get_swap_executor") as mock_exec:
            executor = MagicMock()
            executor.execute_swap = AsyncMock(side_effect=ValueError("Quote has expired"))
            mock_exec.return_value = executor

            r = await api_client.post("/v1/swap", json={
                "quote_id": "expired-quote",
                "user_address": USER_ADDRESS,
                "input_nonce": 0,
                "input_signature": "0x" + "aa" * 65,
            })

            assert r.status_code == 400

    async def test_returns_500_on_unexpected_error(self, api_client):
        with patch("src.api.routes.get_swap_executor") as mock_exec:
            executor = MagicMock()
            executor.execute_swap = AsyncMock(side_effect=RuntimeError("Sapphire unreachable"))
            mock_exec.return_value = executor

            r = await api_client.post("/v1/swap", json={
                "quote_id": "quote-123",
                "user_address": USER_ADDRESS,
                "input_nonce": 0,
                "input_signature": "0x" + "aa" * 65,
            })

            assert r.status_code == 500

    async def test_message_reflects_status(self, api_client):
        with patch("src.api.routes.get_swap_executor") as mock_exec:
            executor = MagicMock()
            executor.execute_swap = AsyncMock(
                return_value=self._mock_swap(status="failed", tx_hash=None, error="reverted")
            )
            mock_exec.return_value = executor

            r = await api_client.post("/v1/swap", json={
                "quote_id": "quote-123",
                "user_address": USER_ADDRESS,
                "input_nonce": 0,
                "input_signature": "0x" + "aa" * 65,
            })

            assert r.status_code == 200
            assert r.json()["message"] == "Swap failed"


class TestSwapStatusRoute:
    async def test_returns_200_for_existing_swap(self, api_client):
        swap = SwapRecord(
            id="swap-456",
            quote_id="quote-456",
            user_address=USER_ADDRESS,
            from_token_id=USDC_TOKEN_ID,
            to_token_id=WETH_TOKEN_ID,
            from_amount="1000000",
            to_amount_estimate="495000000000000",
            status="completed",
            swap_tx_hash="0x" + "ff" * 32,
            created_at=1000,
            updated_at=1000,
        )
        with patch("src.api.routes.get_swap_executor") as mock_exec:
            executor = MagicMock()
            executor._get_swap.return_value = swap
            mock_exec.return_value = executor

            r = await api_client.get("/v1/swap/swap-456/status")

            assert r.status_code == 200
            data = r.json()
            assert data["swap_id"] == "swap-456"
            assert data["status"] == "completed"

    async def test_returns_404_for_missing_swap(self, api_client):
        with patch("src.api.routes.get_swap_executor") as mock_exec:
            executor = MagicMock()
            executor._get_swap.side_effect = ValueError("Swap not found")
            mock_exec.return_value = executor

            r = await api_client.get("/v1/swap/nonexistent/status")

            assert r.status_code == 404


class TestHealthEndpoint:
    async def test_returns_ok_when_sapphire_connected(self, api_client):
        with patch("src.clients.sapphire.get_sapphire_client") as mock_saph:
            saph = MagicMock()
            saph.is_connected.return_value = True
            mock_saph.return_value = saph

            r = await api_client.get("/health")

            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "ok"
            assert data["checks"]["sapphire"] == "ok"

    async def test_returns_degraded_when_sapphire_down(self, api_client):
        with patch("src.clients.sapphire.get_sapphire_client") as mock_saph:
            saph = MagicMock()
            saph.is_connected.return_value = False
            mock_saph.return_value = saph

            r = await api_client.get("/health")

            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "degraded"
            assert data["checks"]["sapphire"] == "unavailable"

    async def test_returns_degraded_when_sapphire_raises(self, api_client):
        with patch("src.clients.sapphire.get_sapphire_client") as mock_saph:
            mock_saph.side_effect = RuntimeError("connection failed")

            r = await api_client.get("/health")

            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "degraded"


class TestTokensAndChainsRoutes:
    async def test_list_chains_returns_200(self, api_client):
        r = await api_client.get("/v1/chains")
        assert r.status_code == 200
        data = r.json()
        assert "chains" in data
        assert isinstance(data["chains"], list)
