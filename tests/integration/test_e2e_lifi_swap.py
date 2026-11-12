from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.eip712 import sign_transfer
from src.models.common import Balance
from tests.clients.fake_chain import SAPPHIRE_CHAIN_ID
from tests.services.swap.world import AMOUNT, TOKENS, USDC, USERS, WETH, World

USER = USERS[0]
LOW_BALANCE = Balance(user_address="0xlp", token_id=WETH, balance="1")
PRICING_ROUTES = {
    "routes": [{"toAmount": "58000", "toAmountMin": "56000", "steps": [{"tool": "fly"}]}]
}


@pytest.fixture(autouse=True)
def _an_rpc_for_every_chain(monkeypatch):
    """Quoting checks for an RPC on each chain. These tests reach none."""
    import src.services.swap.quote_service as qs_mod

    monkeypatch.setattr(qs_mod, "get_evm_client", MagicMock())


@pytest.fixture
def world(settings, test_db, monkeypatch):
    return World(settings, test_db, monkeypatch)


def _stub_quote_service(config, lifi_enabled=True):
    import src.services.swap.quote_service as qs_mod
    from src.services.swap.quote_service import QuoteService

    service = QuoteService.__new__(QuoteService)
    service.settings = replace(config, quote_ttl=300, lifi_execution_enabled=lifi_enabled, lifi_max_swap_amount_usd=0)
    service._last_cleanup = 0
    service._token_map = {}
    service.accounting = MagicMock()
    service.accounting.get_transfer_nonce = AsyncMock(return_value=0)
    service.accounting.get_lp_balance = AsyncMock(return_value=LOW_BALANCE)
    service.accounting.get_token_info = AsyncMock(side_effect=[TOKENS[USDC], TOKENS[WETH]])
    service.lifi = MagicMock()
    service.lifi.get_routes = AsyncMock(return_value=PRICING_ROUTES)
    qs_mod._service_instance = service


def _stub_executor(config):
    import src.services.swap.executor as se_mod
    from src.services.swap.executor import SwapExecutor

    executor = SwapExecutor()
    executor.settings = config
    se_mod._executor_instance = executor


async def _swap(api_client, world):
    """Quote and swap through the API, as the app does."""
    _stub_quote_service(world.config)
    _stub_executor(world.config)
    quote = await api_client.get("/v1/quote", params={
        "from_token_id": USDC, "to_token_id": WETH, "from_amount": str(AMOUNT), "user_address": USER.address,
    })
    assert quote.json()["venue"] == "lifi"
    signature = sign_transfer(USER.key, SAPPHIRE_CHAIN_ID, world.config.accounting_contract_address,
                              world.lp_address, USDC, AMOUNT, 0)
    swap = await api_client.post("/v1/swap", json={
        "quote_id": quote.json()["quote_id"], "input_nonce": 0, "input_signature": signature,
    })
    assert swap.status_code == 200
    assert swap.json()["status"] == "scheduled"
    return swap.json()["swap_id"]


class TestLifiSwapEndToEnd:
    async def test_quote_returns_lifi_venue_when_lp_dry(self, api_client, world):
        _stub_quote_service(world.config, lifi_enabled=True)
        resp = await api_client.get("/v1/quote", params={
            "from_token_id": USDC, "to_token_id": WETH, "from_amount": str(AMOUNT), "user_address": USER.address,
        })
        assert resp.status_code == 200
        assert resp.json()["venue"] == "lifi"

    async def test_flag_off_lp_dry_returns_400(self, api_client, world):
        _stub_quote_service(world.config, lifi_enabled=False)
        resp = await api_client.get("/v1/quote", params={
            "from_token_id": USDC, "to_token_id": WETH, "from_amount": str(AMOUNT), "user_address": USER.address,
        })
        assert resp.status_code == 400
        assert "Insufficient liquidity" in resp.json()["detail"]

    async def test_a_swap_runs_every_stage_and_credits_the_user(self, api_client, world):
        swap_id = await _swap(api_client, world)

        await world.until(swap_id, "completed")

        status = (await api_client.get(f"/v1/swap/{swap_id}/status")).json()
        assert (status["status"], status["to_amount_actual"], status["reason"]) == ("completed", "59940", None)
        assert world.evm_owners(swap_id) == ["input", "approve", "lifi", "deposit"]
        assert world.paid(USER) == [(WETH, 59940)]

    async def test_a_reverted_lifi_tx_ends_refunded(self, api_client, world):
        world.lifi.reverts = True
        swap_id = await _swap(api_client, world)

        await world.until(swap_id, "refunded")

        status = (await api_client.get(f"/v1/swap/{swap_id}/status")).json()
        assert (status["status"], status["reason"]) == ("refunded", "refunded")
        assert world.evm_owners(swap_id) == ["input", "approve", "lifi", "refund"]
        assert world.paid(USER) == [(USDC, AMOUNT)]
