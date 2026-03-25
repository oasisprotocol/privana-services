import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_defunct

load_dotenv()

from src.main import app
from src.services.eip712 import sign_transfer

ACCOUNTING_API = os.getenv("ACCOUNTING_API_BASE_URL", "https://flexvaults-staging.rofl.build")
LP_ADDRESS = os.getenv("LIQUIDITY_PROVIDER_ADDRESS")
LP_PK = os.getenv("LIQUIDITY_PROVIDER_PRIVATE_KEY")
ACCOUNTING_CONTRACT = os.getenv("ACCOUNTING_CONTRACT_ADDRESS")
CHAIN_ID = int(os.getenv("ACCOUNTING_CHAIN_ID", "23295"))

TEST_USER_ADDRESS = "0xd8991364507FAfC256EafF950d28618735753476"
TEST_USER_PK = "0x7b07a59f24f1900ec4e6ac3e521c1acd2cca3518f717abda1dc8bbcbbc344c4e"

USDC_TOKEN_ID = "0x330ba47d00c7ce3018deee017b319fd7cc6473a2ddc9e6eba6ebb4207be15279"
WETH_TOKEN_ID = "0x335b5cccd1e63b2fe79863a0db73fce430e4e66902e2b78424f8662621e29fb7"

pytestmark = pytest.mark.skipif(
    not LP_PK or not LP_ADDRESS,
    reason="Integration tests require .env with LP credentials",
)


async def _siwe_login(c: httpx.AsyncClient, address: str, pk: str) -> tuple[str, str]:
    account = Account.from_key(pk)
    r = await c.get(f"{ACCOUNTING_API}/v1/accounting/auth/nonce?address={address}")
    nonce = r.json()["nonce"]
    now = datetime.now(timezone.utc)
    siwe = (
        f"flexvaults-staging.rofl.build wants you to sign in with your Ethereum account:\n"
        f"{address}\n\nSign in to FlexVaults\n\n"
        f"URI: https://flexvaults-staging.rofl.build\n"
        f"Version: 1\nChain ID: {CHAIN_ID}\nNonce: {nonce}\n"
        f"Issued At: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"Expiration Time: {(now + timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    signed = account.sign_message(encode_defunct(text=siwe))
    r = await c.post(f"{ACCOUNTING_API}/v1/accounting/auth/login", json={
        "siwe_message": siwe, "signature": f"0x{signed.signature.hex()}"
    })
    d = r.json()
    return d["siwe_token"], d["jwt_access_token"]


async def _get_balance(c: httpx.AsyncClient, siwe: str, jwt: str, address: str, token_id: str) -> int:
    r = await c.get(
        f"{ACCOUNTING_API}/v1/accounting/balances/{address}/{token_id}",
        headers={"X-SIWE-Token": siwe, "Authorization": f"Bearer {jwt}"},
    )
    return int(r.json().get("balance", "0"))


@pytest.fixture(autouse=True)
def _reset_singletons():
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
    yield
    acct_mod._client_instance = None
    lifi_mod._client_instance = None
    saph_mod._client_instance = None
    qs_mod._service_instance = None
    se_mod._executor_instance = None


@pytest.fixture
async def api_client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, timeout=120, base_url="http://test") as c:
        yield c


@pytest.fixture
async def acct_client():
    async with httpx.AsyncClient(timeout=30) as c:
        yield c


@pytest.fixture
async def accounting_auth(acct_client):
    try:
        user_siwe, user_jwt = await _siwe_login(acct_client, TEST_USER_ADDRESS, TEST_USER_PK)
        lp_siwe, lp_jwt = await _siwe_login(acct_client, LP_ADDRESS, LP_PK)
    except Exception:
        pytest.skip("Accounting API unavailable")
    return {
        "client": acct_client,
        "user_siwe": user_siwe, "user_jwt": user_jwt,
        "lp_siwe": lp_siwe, "lp_jwt": lp_jwt,
    }


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
            "from_amount": "1000000",
            "user_address": TEST_USER_ADDRESS,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["from_token_id"] == USDC_TOKEN_ID.lower()
        assert data["to_token_id"] == WETH_TOKEN_ID.lower()
        assert data["from_amount"] == "1000000"
        assert int(data["to_amount_estimate"]) > 0
        assert int(data["fee_amount"]) > 0
        assert data["liquidity_provider"] == LP_ADDRESS
        assert isinstance(data["transfer_nonce"], int)
        assert data["quote_id"] is not None

    async def test_get_weth_to_usdc_quote(self, api_client):
        r = await api_client.get("/v1/quote", params={
            "from_token_id": WETH_TOKEN_ID,
            "to_token_id": USDC_TOKEN_ID,
            "from_amount": str(10**16),
            "user_address": TEST_USER_ADDRESS,
        })
        assert r.status_code == 200
        data = r.json()
        assert int(data["to_amount_estimate"]) > 0

    async def test_invalid_token_id_returns_400(self, api_client):
        r = await api_client.get("/v1/quote", params={
            "from_token_id": "not-hex",
            "to_token_id": WETH_TOKEN_ID,
            "from_amount": "1000000",
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
    async def test_swap_usdc_to_weth(self, api_client, accounting_auth):
        auth = accounting_auth
        c = auth["client"]

        user_usdc_before = await _get_balance(
            c, auth["user_siwe"], auth["user_jwt"], TEST_USER_ADDRESS, USDC_TOKEN_ID
        )
        user_weth_before = await _get_balance(
            c, auth["user_siwe"], auth["user_jwt"], TEST_USER_ADDRESS, WETH_TOKEN_ID
        )
        lp_usdc_before = await _get_balance(
            c, auth["lp_siwe"], auth["lp_jwt"], LP_ADDRESS, USDC_TOKEN_ID
        )
        lp_weth_before = await _get_balance(
            c, auth["lp_siwe"], auth["lp_jwt"], LP_ADDRESS, WETH_TOKEN_ID
        )

        swap_amount = "1000000"

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

        auth2 = {}
        auth2["user_siwe"], auth2["user_jwt"] = await _siwe_login(c, TEST_USER_ADDRESS, TEST_USER_PK)
        auth2["lp_siwe"], auth2["lp_jwt"] = await _siwe_login(c, LP_ADDRESS, LP_PK)

        user_usdc_after = await _get_balance(
            c, auth2["user_siwe"], auth2["user_jwt"], TEST_USER_ADDRESS, USDC_TOKEN_ID
        )
        user_weth_after = await _get_balance(
            c, auth2["user_siwe"], auth2["user_jwt"], TEST_USER_ADDRESS, WETH_TOKEN_ID
        )
        lp_usdc_after = await _get_balance(
            c, auth2["lp_siwe"], auth2["lp_jwt"], LP_ADDRESS, USDC_TOKEN_ID
        )
        lp_weth_after = await _get_balance(
            c, auth2["lp_siwe"], auth2["lp_jwt"], LP_ADDRESS, WETH_TOKEN_ID
        )

        assert user_usdc_after < user_usdc_before
        assert user_weth_after > user_weth_before
        assert lp_usdc_after > lp_usdc_before
        assert lp_weth_after < lp_weth_before

    async def test_swap_weth_to_usdc(self, api_client, accounting_auth):
        auth = accounting_auth
        c = auth["client"]

        swap_amount = str(10**16)

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
            "from_amount": "1000000",
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
            "from_amount": "1000000",
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
            amount=int("1000000"),
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
