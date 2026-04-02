import os

import httpx
import pytest
from dotenv import load_dotenv

load_dotenv()

from src.core.eip712 import sign_transfer

LP_ADDRESS = os.getenv("LIQUIDITY_PROVIDER_ADDRESS")
LP_PK = os.getenv("LIQUIDITY_PROVIDER_PRIVATE_KEY")
ACCOUNTING_CONTRACT = os.getenv("ACCOUNTING_CONTRACT_ADDRESS")
CHAIN_ID = int(os.getenv("ACCOUNTING_CHAIN_ID", "23295"))

TEST_USER_ADDRESS = "0xd8991364507FAfC256EafF950d28618735753476"
TEST_USER_PK = "0x7b07a59f24f1900ec4e6ac3e521c1acd2cca3518f717abda1dc8bbcbbc344c4e"

USDC_TOKEN_ID = "0x330ba47d00c7ce3018deee017b319fd7cc6473a2ddc9e6eba6ebb4207be15279"
WETH_TOKEN_ID = "0x335b5cccd1e63b2fe79863a0db73fce430e4e66902e2b78424f8662621e29fb7"

pytestmark = [
    pytest.mark.skipif(
        not LP_PK or not LP_ADDRESS,
        reason="Integration tests require .env with LP credentials",
    ),
    pytest.mark.integration,
]


@pytest.fixture
async def api_client():
    import src.clients.accounting as acct_mod
    import src.clients.lifi as lifi_mod
    import src.clients.sapphire as saph_mod
    import src.services.quote_service as qs_mod
    import src.services.swap_executor as se_mod
    acct_mod._client_instance = None
    lifi_mod._client_instance = None
    saph_mod._client_instance = None
    qs_mod._service_instance = None
    se_mod._executor_instance = None

    from src.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, timeout=120, base_url="http://test") as c:
        yield c

    acct_mod._client_instance = None
    lifi_mod._client_instance = None
    saph_mod._client_instance = None
    qs_mod._service_instance = None
    se_mod._executor_instance = None


class TestHealthCheck:
    async def test_health_returns_ok(self, api_client):
        r = await api_client.get("/health")
        data = r.json()
        assert data["status"] in ("ok", "degraded")
        assert "checks" in data


class TestQuoteEndpoint:
    async def test_get_usdc_to_weth_quote(self, api_client):
        r = await api_client.get("/v1/quote", params={
            "from_token_id": USDC_TOKEN_ID,
            "to_token_id": WETH_TOKEN_ID,
            "from_amount": "500000",
            "user_address": TEST_USER_ADDRESS,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["from_token_id"] == USDC_TOKEN_ID.lower()
        assert data["to_token_id"] == WETH_TOKEN_ID.lower()
        assert data["from_amount"] == "500000"
        assert int(data["to_amount_estimate"]) > 0
        assert int(data["fee_amount"]) > 0
        assert data["liquidity_provider"] == LP_ADDRESS
        assert isinstance(data["transfer_nonce"], int)
        assert data["quote_id"] is not None

    async def test_get_weth_to_usdc_quote(self, api_client):
        r = await api_client.get("/v1/quote", params={
            "from_token_id": WETH_TOKEN_ID,
            "to_token_id": USDC_TOKEN_ID,
            "from_amount": str(5 * 10**15),
            "user_address": TEST_USER_ADDRESS,
        })
        assert r.status_code == 200
        data = r.json()
        assert int(data["to_amount_estimate"]) > 0

    async def test_invalid_token_id_returns_400(self, api_client):
        r = await api_client.get("/v1/quote", params={
            "from_token_id": "not-hex",
            "to_token_id": WETH_TOKEN_ID,
            "from_amount": "500000",
            "user_address": TEST_USER_ADDRESS,
        })
        assert r.status_code == 400

    async def test_zero_amount_returns_400(self, api_client):
        r = await api_client.get("/v1/quote", params={
            "from_token_id": USDC_TOKEN_ID,
            "to_token_id": WETH_TOKEN_ID,
            "from_amount": "0",
            "user_address": TEST_USER_ADDRESS,
        })
        assert r.status_code == 400


class TestSwapEndpoint:
    async def test_swap_usdc_to_weth(self, api_client):
        swap_amount = "500000"

        r = await api_client.get("/v1/quote", params={
            "from_token_id": USDC_TOKEN_ID,
            "to_token_id": WETH_TOKEN_ID,
            "from_amount": swap_amount,
            "user_address": TEST_USER_ADDRESS,
        })
        assert r.status_code == 200
        quote = r.json()

        sig = sign_transfer(
            private_key=TEST_USER_PK,
            chain_id=CHAIN_ID,
            verifying_contract=ACCOUNTING_CONTRACT,
            user_address=TEST_USER_ADDRESS,
            to_address=quote["liquidity_provider"],
            token_id=USDC_TOKEN_ID,
            amount=int(swap_amount),
            nonce=quote["transfer_nonce"],
        )

        r = await api_client.post("/v1/swap", json={
            "quote_id": quote["quote_id"],
            "user_address": TEST_USER_ADDRESS,
            "input_nonce": quote["transfer_nonce"],
            "input_signature": sig,
        })
        assert r.status_code == 200
        result = r.json()
        assert result["status"] == "completed"
        assert result["tx_hash"] is not None
        assert result["tx_hash"].startswith("0x")
        print(f"\n  USDC→WETH swap tx: {result['tx_hash']}")

    async def test_swap_weth_to_usdc(self, api_client):
        swap_amount = str(5 * 10**15)

        r = await api_client.get("/v1/quote", params={
            "from_token_id": WETH_TOKEN_ID,
            "to_token_id": USDC_TOKEN_ID,
            "from_amount": swap_amount,
            "user_address": TEST_USER_ADDRESS,
        })
        assert r.status_code == 200
        quote = r.json()

        sig = sign_transfer(
            private_key=TEST_USER_PK,
            chain_id=CHAIN_ID,
            verifying_contract=ACCOUNTING_CONTRACT,
            user_address=TEST_USER_ADDRESS,
            to_address=quote["liquidity_provider"],
            token_id=WETH_TOKEN_ID,
            amount=int(swap_amount),
            nonce=quote["transfer_nonce"],
        )

        r = await api_client.post("/v1/swap", json={
            "quote_id": quote["quote_id"],
            "user_address": TEST_USER_ADDRESS,
            "input_nonce": quote["transfer_nonce"],
            "input_signature": sig,
        })
        assert r.status_code == 200
        result = r.json()
        assert result["status"] == "completed"
        assert result["tx_hash"] is not None
        print(f"\n  WETH→USDC swap tx: {result['tx_hash']}")

    async def test_expired_quote_returns_400(self, api_client):
        r = await api_client.post("/v1/swap", json={
            "quote_id": "nonexistent-quote-id",
            "user_address": TEST_USER_ADDRESS,
            "input_nonce": 0,
            "input_signature": "0x" + "aa" * 65,
        })
        assert r.status_code == 400

    async def test_invalid_signature_format_returns_400(self, api_client):
        r = await api_client.get("/v1/quote", params={
            "from_token_id": USDC_TOKEN_ID,
            "to_token_id": WETH_TOKEN_ID,
            "from_amount": "500000",
            "user_address": TEST_USER_ADDRESS,
        })
        if r.status_code != 200:
            pytest.skip("Quote failed")
        quote = r.json()

        r = await api_client.post("/v1/swap", json={
            "quote_id": quote["quote_id"],
            "user_address": TEST_USER_ADDRESS,
            "input_nonce": 0,
            "input_signature": "0xbad",
        })
        assert r.status_code == 400


class TestSwapStatus:
    async def test_get_status_after_swap(self, api_client):
        r = await api_client.get("/v1/quote", params={
            "from_token_id": USDC_TOKEN_ID,
            "to_token_id": WETH_TOKEN_ID,
            "from_amount": "500000",
            "user_address": TEST_USER_ADDRESS,
        })
        if r.status_code != 200:
            pytest.skip("Quote failed")
        quote = r.json()

        sig = sign_transfer(
            private_key=TEST_USER_PK,
            chain_id=CHAIN_ID,
            verifying_contract=ACCOUNTING_CONTRACT,
            user_address=TEST_USER_ADDRESS,
            to_address=quote["liquidity_provider"],
            token_id=USDC_TOKEN_ID,
            amount=int("500000"),
            nonce=quote["transfer_nonce"],
        )

        r = await api_client.post("/v1/swap", json={
            "quote_id": quote["quote_id"],
            "user_address": TEST_USER_ADDRESS,
            "input_nonce": quote["transfer_nonce"],
            "input_signature": sig,
        })
        swap = r.json()

        r = await api_client.get(f"/v1/swap/{swap['swap_id']}/status")
        assert r.status_code == 200
        status = r.json()
        assert status["swap_id"] == swap["swap_id"]
        assert status["status"] == "completed"
        assert status["swap_tx_hash"] is not None
        assert status["from_token_id"] == USDC_TOKEN_ID.lower()
        assert status["to_token_id"] == WETH_TOKEN_ID.lower()
        print(f"\n  Status check swap tx: {status['swap_tx_hash']}")

    async def test_nonexistent_swap_returns_404(self, api_client):
        r = await api_client.get("/v1/swap/nonexistent-id/status")
        assert r.status_code == 404
