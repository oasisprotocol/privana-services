import os

import httpx
import pytest

from src.services.earn.registry import (
    StrategyRegistry,
    register_aave_strategies_from_config,
)

LP_SK = os.getenv("LIQUIDITY_PROVIDER_SECRET_KEY")
AAVE_POOL_ASSETS = os.getenv("AAVE_POOL_ASSETS", "")
DEFILLAMA_POOL_IDS = os.getenv("DEFILLAMA_POOL_IDS", "")

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

pytestmark = [
    pytest.mark.skipif(
        not LP_SK or not AAVE_POOL_ASSETS.strip(),
        reason="Earn integration tests require LIQUIDITY_PROVIDER_SECRET_KEY and AAVE_POOL_ASSETS",
    ),
    pytest.mark.integration,
]


@pytest.fixture
async def api_client():
    import src.clients.accounting as acct_mod
    import src.clients.lifi as lifi_mod
    import src.clients.sapphire as saph_mod

    acct_mod._client_instance = None
    lifi_mod._client_instance = None
    saph_mod._client_instance = None

    from src.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, timeout=120, base_url="http://test") as c:
        yield c

    acct_mod._client_instance = None
    lifi_mod._client_instance = None
    saph_mod._client_instance = None


@pytest.fixture
async def registered_pools():
    """Register the deployed AAVE_POOL_ASSETS config against live Base Sepolia.

    This is the check that a unit test cannot make: whether the pool address
    and asset in the deployed env actually belong together on-chain.

    Settings are refreshed and the client singletons dropped so the registry
    is built from the environment this run actually loaded, not from whatever
    a previous test left cached. The HTTP clients matter as much as the
    settings: each test runs on its own event loop, and a cached
    httpx.AsyncClient still bound to a closed loop fails the whole setup.
    """
    import src.clients.aave as aave_mod
    import src.clients.accounting as acct_mod
    import src.clients.defillama as llama_mod
    from src.core.config import load_settings

    load_settings(refresh=True)
    aave_mod._client_instance = None
    acct_mod._client_instance = None
    llama_mod._client_instance = None
    registry = StrategyRegistry()
    count = await register_aave_strategies_from_config(
        registry, AAVE_POOL_ASSETS, DEFILLAMA_POOL_IDS
    )
    # Every test below iterates pool_ids(); without this they would pass
    # vacuously whenever registration produced nothing.
    assert count > 0, "AAVE_POOL_ASSETS registered no pools"
    yield registry, count
    aave_mod._client_instance = None
    acct_mod._client_instance = None
    llama_mod._client_instance = None


class TestAaveConfiguration:
    async def test_every_configured_pool_registers(self, registered_pools):
        registry, count = registered_pools
        assert count > 0, (
            "No Aave pool registered from AAVE_POOL_ASSETS. Every pool would "
            "silently fall back to ManualStrategy and earn nothing."
        )
        assert len(registry.pool_ids()) == count

    async def test_configured_asset_is_listed_on_the_configured_pool(self, registered_pools):
        # The regression this file exists for: AAVE_POOL_ADDRESS pointing at a
        # pool that does not list the configured asset. Aave returns a
        # zero-filled reserve rather than reverting, so nothing else notices.
        from src.clients.aave import get_aave_client

        registry, _ = registered_pools
        client = get_aave_client()
        for pool_id in registry.pool_ids():
            strategy = registry.get(pool_id)
            atoken = client.get_aToken_address(strategy.asset_address)
            assert atoken != ZERO_ADDRESS, (
                f"Asset {strategy.asset_address} is not a listed reserve on "
                f"Aave pool {client.pool_address}"
            )

    async def test_every_registered_pool_is_healthy(self, registered_pools):
        registry, _ = registered_pools
        for pool_id in registry.pool_ids():
            strategy = registry.get(pool_id)
            assert await strategy.is_healthy() is True, (
                f"Pool {pool_id} reports unhealthy; deposits would be routed "
                "into a protocol that cannot accept them."
            )

    async def test_supply_apy_is_reported(self, registered_pools):
        registry, _ = registered_pools
        for pool_id in registry.pool_ids():
            apy_bps = await registry.get(pool_id).get_apy_bps()
            assert apy_bps > 0, (
                f"Pool {pool_id} reports {apy_bps} bps. A live Aave reserve pays "
                "something; zero usually means the reserve is not listed."
            )

    async def test_pool_total_assets_is_readable(self, registered_pools):
        registry, _ = registered_pools
        for pool_id in registry.pool_ids():
            assert await registry.get(pool_id).total_assets() >= 0


class TestEarnReadEndpoints:
    async def test_lists_pools(self, api_client):
        r = await api_client.get("/v1/earn/pools")
        assert r.status_code == 200
        pools = r.json()["pools"]
        assert len(pools) > 0
        for pool in pools:
            assert pool["pool_id"].startswith("0x")
            assert pool["token_id"].startswith("0x")
            assert int(pool["total_assets"]) >= 0

    async def test_pool_detail_matches_listing(self, api_client):
        pools = (await api_client.get("/v1/earn/pools")).json()["pools"]
        pool_id = pools[0]["pool_id"]

        r = await api_client.get(f"/v1/earn/pools/{pool_id}")
        assert r.status_code == 200
        detail = r.json()
        assert detail["pool_id"] == pool_id
        assert detail["token_id"] == pools[0]["token_id"]
        assert int(detail["total_shares"]) >= 0

    async def test_deposit_quote_shares_track_the_pool_exchange_rate(self, api_client):
        pools = (await api_client.get("/v1/earn/pools")).json()["pools"]
        pool_id = pools[0]["pool_id"]

        r = await api_client.get("/v1/earn/quote", params={
            "pool_id": pool_id,
            "amount": "1000000",
            "user_address": "0x" + "a" * 40,
        })
        assert r.status_code == 200
        quote = r.json()
        assert int(quote["shares_estimate"]) > 0
        assert quote["pool_id"] == pool_id

    async def test_balance_requires_a_bearer_token(self, api_client):
        r = await api_client.get("/v1/earn/balance", params={"user_address": "0x" + "a" * 40})
        assert r.status_code == 401

    async def test_withdraw_nonce_requires_a_bearer_token(self, api_client):
        r = await api_client.get("/v1/earn/withdraw/nonce")
        assert r.status_code == 401
