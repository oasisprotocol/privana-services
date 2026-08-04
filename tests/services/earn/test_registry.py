from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import load_settings
from src.models.api import TokenInfo
from src.models.settings import Settings
from src.services.earn.registry import (
    StrategyRegistry,
    get_strategy_registry,
    register_aave_strategies_from_config,
    register_midas_strategies_from_config,
    reset_strategy_registry,
)
from src.services.earn.strategies.manual import ManualStrategy


def _accounting_client(addresses_by_token: dict[str, str], chain_id: int = 84532) -> MagicMock:
    """Stub accounting client that resolves token_id -> token_address."""
    client = MagicMock()

    async def _get_token_info(token_id: str) -> TokenInfo:
        addr = addresses_by_token.get(token_id)
        return TokenInfo(
            token_id=token_id,
            token_type=1,
            token_type_name="ERC20",
            chain_id=chain_id,
            token_address=addr,
        )

    client.get_token_info = AsyncMock(side_effect=_get_token_info)
    return client


POOL_ID = "abc123"
POOL_ID_PREFIXED = "0xABC123"


def _settings() -> Settings:
    return replace(
        load_settings(),
        liquidity_provider_secret_key=(
            "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        ),
        liquidity_provider_address="0xd8991364507FAfC256EafF950d28618735753476",
        accounting_contract_address="0xad3C76e4E621C0cfF7540479Ee9B0A945723A642",
        accounting_chain_id=23295,
    )


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_strategy_registry()
    yield
    reset_strategy_registry()


@pytest.fixture
def registry() -> StrategyRegistry:
    return StrategyRegistry()


def test_register_and_get_returns_registered_strategy(registry: StrategyRegistry) -> None:
    strategy = MagicMock()
    strategy.name = "aave-v3"
    registry.register(POOL_ID, strategy)

    assert registry.get(POOL_ID) is strategy


def test_get_with_unknown_pool_returns_manual_default(registry: StrategyRegistry) -> None:
    fallback = registry.get("0xdeadbeef")

    assert isinstance(fallback, ManualStrategy)


def test_has_reports_registration_state(registry: StrategyRegistry) -> None:
    assert registry.has(POOL_ID) is False

    registry.register(POOL_ID, MagicMock())
    assert registry.has(POOL_ID) is True


def test_pool_id_is_normalized_case_and_prefix(registry: StrategyRegistry) -> None:
    strategy = MagicMock()
    registry.register(POOL_ID_PREFIXED, strategy)

    assert registry.get("abc123") is strategy
    assert registry.get("0xABC123") is strategy
    assert registry.has("0xabc123") is True


def test_register_overwrites_existing(registry: StrategyRegistry, caplog) -> None:
    first = MagicMock()
    first.name = "old"
    second = MagicMock()
    second.name = "new"

    registry.register(POOL_ID, first)
    registry.register(POOL_ID, second)

    assert registry.get(POOL_ID) is second
    assert any("overwriting strategy" in r.message for r in caplog.records)


def test_pool_ids_returns_only_registered(registry: StrategyRegistry) -> None:
    registry.register("aa", MagicMock())
    registry.register("0xBB", MagicMock())

    assert sorted(registry.pool_ids()) == ["aa", "bb"]


def test_singleton_returns_same_instance() -> None:
    first = get_strategy_registry()
    second = get_strategy_registry()

    assert first is second


def test_reset_clears_singleton() -> None:
    first = get_strategy_registry()
    reset_strategy_registry()
    second = get_strategy_registry()

    assert first is not second


class TestRegisterAaveFromConfig:
    async def test_empty_config_is_noop(self, registry: StrategyRegistry) -> None:
        assert await register_aave_strategies_from_config(registry, "") == 0
        assert await register_aave_strategies_from_config(registry, "   ") == 0
        assert registry.pool_ids() == []

    async def test_invalid_json_logs_and_returns_zero(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        result = await register_aave_strategies_from_config(registry, "{not valid")

        assert result == 0
        assert any("invalid JSON" in r.message for r in caplog.records)

    async def test_non_object_json_logs_and_returns_zero(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        result = await register_aave_strategies_from_config(
            registry, '["not", "an", "object"]'
        )

        assert result == 0
        assert any("must be a JSON object" in r.message for r in caplog.records)

    async def test_registers_each_pool_from_token_id_string(
        self, registry: StrategyRegistry
    ) -> None:
        config = '{"0xab12": "0xaaaa", "0xcd34": "0xbbbb"}'
        accounting = _accounting_client({
            "0xaaaa": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "0xbbbb": "0x4200000000000000000000000000000000000006",
        })
        with patch("src.clients.aave.get_aave_client", return_value=MagicMock()), \
             patch("src.clients.accounting.get_accounting_client", return_value=accounting), \
             patch("src.services.earn.strategies.aave.load_settings", return_value=_settings()):
            count = await register_aave_strategies_from_config(registry, config)

        assert count == 2
        assert sorted(registry.pool_ids()) == ["ab12", "cd34"]
        registered = registry.get("0xab12")
        assert registered.name == "aave-v3"
        assert registered.asset_address == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        assert registered.token_id == "0xaaaa"

    async def test_accepts_legacy_nested_form_and_warns_on_asset_address(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        config = (
            '{"0xab12": {"token_id": "0xaaaa", '
            '"asset_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}}'
        )
        accounting = _accounting_client({
            "0xaaaa": "0x4200000000000000000000000000000000000006",
        })
        with patch("src.clients.aave.get_aave_client", return_value=MagicMock()), \
             patch("src.clients.accounting.get_accounting_client", return_value=accounting), \
             patch("src.services.earn.strategies.aave.load_settings", return_value=_settings()):
            count = await register_aave_strategies_from_config(registry, config)

        assert count == 1
        registered = registry.get("0xab12")
        # asset_address comes from accounting, not the env entry.
        assert registered.asset_address == "0x4200000000000000000000000000000000000006"
        assert any("'asset_address' is deprecated" in r.message for r in caplog.records)

    async def test_skips_entries_with_missing_token_id(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        config = '{"0xab12": {}, "0xcd34": "0xbbbb"}'
        accounting = _accounting_client({
            "0xbbbb": "0x4200000000000000000000000000000000000006",
        })
        with patch("src.clients.aave.get_aave_client", return_value=MagicMock()), \
             patch("src.clients.accounting.get_accounting_client", return_value=accounting), \
             patch("src.services.earn.strategies.aave.load_settings", return_value=_settings()):
            count = await register_aave_strategies_from_config(registry, config)

        assert count == 1
        assert registry.pool_ids() == ["cd34"]
        assert any("invalid token_id" in r.message for r in caplog.records)

    async def test_skips_when_accounting_has_no_token_address(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        config = '{"0xab12": "0xaaaa", "0xcd34": "0xbbbb"}'
        accounting = _accounting_client({
            "0xbbbb": "0x4200000000000000000000000000000000000006",
        })
        with patch("src.clients.aave.get_aave_client", return_value=MagicMock()), \
             patch("src.clients.accounting.get_accounting_client", return_value=accounting), \
             patch("src.services.earn.strategies.aave.load_settings", return_value=_settings()):
            count = await register_aave_strategies_from_config(registry, config)

        assert count == 1
        assert registry.pool_ids() == ["cd34"]
        assert any("no token_address" in r.message for r in caplog.records)

    async def test_skips_when_accounting_lookup_raises(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        config = '{"0xab12": "0xaaaa", "0xcd34": "0xbbbb"}'
        accounting = MagicMock()

        async def _get_token_info(token_id: str):
            if token_id == "0xaaaa":
                raise RuntimeError("accounting unavailable")
            return TokenInfo(
                token_id=token_id, token_type=1, token_type_name="ERC20",
                chain_id=84532,
                token_address="0x4200000000000000000000000000000000000006",
            )

        accounting.get_token_info = AsyncMock(side_effect=_get_token_info)
        with patch("src.clients.aave.get_aave_client", return_value=MagicMock()), \
             patch("src.clients.accounting.get_accounting_client", return_value=accounting), \
             patch("src.services.earn.strategies.aave.load_settings", return_value=_settings()):
            count = await register_aave_strategies_from_config(registry, config)

        assert count == 1
        assert registry.pool_ids() == ["cd34"]
        assert any("failed to resolve token_id" in r.message for r in caplog.records)


def _midas_settings() -> Settings:
    return replace(
        load_settings(),
        liquidity_provider_secret_key=(
            "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        ),
        liquidity_provider_address="0xd8991364507FAfC256EafF950d28618735753476",
        accounting_contract_address="0xad3C76e4E621C0cfF7540479Ee9B0A945723A642",
        accounting_chain_id=23295,
        midas_default_slippage_bps=50,
        midas_oracle_heartbeat_sec=86400,
    )


USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
MTBILL_TOKEN = "0xDD629E5241CbC5919847783e6C96B2De4754e438"
AAVE_LLAMA_UUID = "7e0661bf-8cf3-45e6-9424-31916d4c7b84"
MIDAS_LLAMA_UUID = "c4a1f2d0-2b6e-4c9a-8d3f-1a2b3c4d5e6f"


def _llama_client(meta: object) -> MagicMock:
    client = MagicMock()
    if isinstance(meta, Exception):
        client.get_pool_meta = AsyncMock(side_effect=meta)
    else:
        client.get_pool_meta = AsyncMock(return_value=meta)
    return client


async def _register_aave_with_llama(registry, llama, llama_config: str):
    accounting = _accounting_client({"0xaaaa": USDC_BASE})
    with patch("src.clients.aave.get_aave_client", return_value=MagicMock()), \
         patch("src.clients.accounting.get_accounting_client", return_value=accounting), \
         patch("src.services.earn.registry.get_defillama_client", return_value=llama), \
         patch("src.services.earn.strategies.aave.load_settings", return_value=_settings()):
        await register_aave_strategies_from_config(
            registry, '{"0xab12": "0xaaaa"}', llama_config
        )
    return registry.get("0xab12")


async def _register_midas_with_llama(registry, llama, llama_config: str):
    accounting = _accounting_client({"0xaaaa": USDC_BASE})
    with patch("src.clients.midas.get_midas_client", return_value=MagicMock()), \
         patch("src.clients.accounting.get_accounting_client", return_value=accounting), \
         patch("src.services.earn.registry.get_defillama_client", return_value=llama), \
         patch("src.services.earn.strategies.midas.load_settings", return_value=_midas_settings()):
        await register_midas_strategies_from_config(
            registry, '{"0xab12": "0xaaaa"}', llama_config
        )
    return registry.get("0xab12")


class TestAaveDefiLlamaWiring:
    async def test_pool_without_a_uuid_has_no_history(self, registry: StrategyRegistry) -> None:
        strategy = await _register_aave_with_llama(registry, _llama_client(None), "")

        assert strategy._defillama_pool_id is None
        assert await strategy.get_apy_history() == []

    async def test_verified_uuid_is_wired_in(self, registry: StrategyRegistry) -> None:
        llama = _llama_client(
            {"project": "aave-v3", "symbol": "USDC", "chain": "Base",
             "underlyingTokens": [USDC_BASE]}
        )

        strategy = await _register_aave_with_llama(
            registry, llama, '{"0xab12": "%s"}' % AAVE_LLAMA_UUID
        )

        assert strategy._defillama_pool_id == AAVE_LLAMA_UUID

    async def test_uuid_from_another_project_is_rejected(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        # Underlying happens to match, but it's a Compound pool. Serving its curve
        # under an Aave pool would be a confident lie.
        llama = _llama_client(
            {"project": "compound-v3", "symbol": "USDC", "chain": "Base",
             "underlyingTokens": [USDC_BASE]}
        )

        strategy = await _register_aave_with_llama(
            registry, llama, '{"0xab12": "%s"}' % AAVE_LLAMA_UUID
        )

        assert strategy._defillama_pool_id is None
        assert any("refusing to serve" in r.message for r in caplog.records)

    async def test_unknown_uuid_is_rejected(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        strategy = await _register_aave_with_llama(
            registry, _llama_client(None), '{"0xab12": "not-a-real-pool"}'
        )

        assert strategy._defillama_pool_id is None
        assert any("does not know" in r.message for r in caplog.records)

    async def test_unreachable_defillama_keeps_the_configured_uuid(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        # A blip at boot must not disable the chart until someone restarts us.
        llama = _llama_client(RuntimeError("connection refused"))

        strategy = await _register_aave_with_llama(
            registry, llama, '{"0xab12": "%s"}' % AAVE_LLAMA_UUID
        )

        assert strategy._defillama_pool_id == AAVE_LLAMA_UUID
        assert any("could not verify" in r.message for r in caplog.records)

    async def test_testnet_asset_does_not_disable_the_chart(
        self, registry: StrategyRegistry
    ) -> None:
        # Our pool holds Sepolia USDC while DefiLlama only carries the mainnet
        # venue, so the two token addresses legitimately differ. That must not
        # disable the chart — showing the mainnet rate is the whole point.
        llama = _llama_client(
            {"project": "aave-v3", "symbol": "USDC", "chain": "Base",
             "underlyingTokens": [USDC_BASE]}
        )
        accounting = _accounting_client({"0xaaaa": "0x036CbD53842c5426634e7929541eC2318f3dCF7e"})
        with patch("src.clients.aave.get_aave_client", return_value=MagicMock()), \
             patch("src.clients.accounting.get_accounting_client", return_value=accounting), \
             patch("src.services.earn.registry.get_defillama_client", return_value=llama), \
             patch("src.services.earn.strategies.aave.load_settings", return_value=_settings()):
            await register_aave_strategies_from_config(
                registry, '{"0xab12": "0xaaaa"}', '{"0xab12": "%s"}' % AAVE_LLAMA_UUID
            )

        assert registry.get("0xab12")._defillama_pool_id == AAVE_LLAMA_UUID

    async def test_bad_json_disables_history_without_crashing(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        strategy = await _register_aave_with_llama(registry, _llama_client(None), "{oops")

        assert strategy._defillama_pool_id is None
        assert strategy.name == "aave-v3"  # the pool itself still registers


class TestRegisterMidasFromConfig:
    async def test_empty_config_is_noop(self, registry: StrategyRegistry) -> None:
        assert await register_midas_strategies_from_config(registry, "") == 0
        assert await register_midas_strategies_from_config(registry, "   ") == 0
        assert registry.pool_ids() == []

    async def test_invalid_json_logs_and_returns_zero(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        result = await register_midas_strategies_from_config(registry, "{not valid")

        assert result == 0
        assert any("invalid JSON" in r.message for r in caplog.records)

    async def test_non_object_json_logs_and_returns_zero(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        result = await register_midas_strategies_from_config(
            registry, '["not", "an", "object"]'
        )

        assert result == 0
        assert any("must be a JSON object" in r.message for r in caplog.records)

    async def test_registers_each_pool_from_token_id_string(
        self, registry: StrategyRegistry
    ) -> None:
        config = '{"0xab12": "0xaaaa", "0xcd34": "0xbbbb"}'
        accounting = _accounting_client({
            "0xaaaa": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "0xbbbb": "0x4200000000000000000000000000000000000006",
        })
        with patch("src.clients.midas.get_midas_client", return_value=MagicMock()), \
             patch("src.clients.accounting.get_accounting_client", return_value=accounting), \
             patch("src.services.earn.strategies.midas.load_settings", return_value=_midas_settings()):
            count = await register_midas_strategies_from_config(registry, config)

        assert count == 2
        assert sorted(registry.pool_ids()) == ["ab12", "cd34"]
        registered = registry.get("0xab12")
        assert registered.name == "midas-mtbill"
        assert registered.asset_address == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        assert registered.token_id == "0xaaaa"

    async def test_rejects_dict_form_no_legacy_support(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        config = (
            '{"0xab12": {"token_id": "0xaaaa"}, "0xcd34": "0xbbbb"}'
        )
        accounting = _accounting_client({
            "0xbbbb": "0x4200000000000000000000000000000000000006",
        })
        with patch("src.clients.midas.get_midas_client", return_value=MagicMock()), \
             patch("src.clients.accounting.get_accounting_client", return_value=accounting), \
             patch("src.services.earn.strategies.midas.load_settings", return_value=_midas_settings()):
            count = await register_midas_strategies_from_config(registry, config)

        assert count == 1
        assert registry.pool_ids() == ["cd34"]
        assert any("must be a token_id string" in r.message for r in caplog.records)

    async def test_rejects_empty_string_entry(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        config = '{"0xab12": "", "0xcd34": "0xbbbb"}'
        accounting = _accounting_client({
            "0xbbbb": "0x4200000000000000000000000000000000000006",
        })
        with patch("src.clients.midas.get_midas_client", return_value=MagicMock()), \
             patch("src.clients.accounting.get_accounting_client", return_value=accounting), \
             patch("src.services.earn.strategies.midas.load_settings", return_value=_midas_settings()):
            count = await register_midas_strategies_from_config(registry, config)

        assert count == 1
        assert registry.pool_ids() == ["cd34"]
        assert any("must be a token_id string" in r.message for r in caplog.records)

    async def test_skips_when_accounting_has_no_token_address(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        config = '{"0xab12": "0xaaaa", "0xcd34": "0xbbbb"}'
        accounting = _accounting_client({
            "0xbbbb": "0x4200000000000000000000000000000000000006",
        })
        with patch("src.clients.midas.get_midas_client", return_value=MagicMock()), \
             patch("src.clients.accounting.get_accounting_client", return_value=accounting), \
             patch("src.services.earn.strategies.midas.load_settings", return_value=_midas_settings()):
            count = await register_midas_strategies_from_config(registry, config)

        assert count == 1
        assert registry.pool_ids() == ["cd34"]
        assert any("no token_address" in r.message for r in caplog.records)

    async def test_skips_when_accounting_lookup_raises(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        config = '{"0xab12": "0xaaaa", "0xcd34": "0xbbbb"}'
        accounting = MagicMock()

        async def _get_token_info(token_id: str):
            if token_id == "0xaaaa":
                raise RuntimeError("accounting unavailable")
            return TokenInfo(
                token_id=token_id, token_type=1, token_type_name="ERC20",
                chain_id=84532,
                token_address="0x4200000000000000000000000000000000000006",
            )

        accounting.get_token_info = AsyncMock(side_effect=_get_token_info)
        with patch("src.clients.midas.get_midas_client", return_value=MagicMock()), \
             patch("src.clients.accounting.get_accounting_client", return_value=accounting), \
             patch("src.services.earn.strategies.midas.load_settings", return_value=_midas_settings()):
            count = await register_midas_strategies_from_config(registry, config)

        assert count == 1
        assert registry.pool_ids() == ["cd34"]
        assert any("failed to resolve token_id" in r.message for r in caplog.records)


class TestMidasDefiLlamaWiring:
    # DefiLlama keys midas-rwa rows by the underlying (symbol="USDC") and names
    # the product in poolMeta, so poolMeta is the discriminator. mTBILL is one
    # fund with a chain-independent APY, listed on Ethereum/Etherlink but not
    # Base — we custody it on Base, so chain deliberately differs and is not
    # asserted.
    def _midas_row(self, **overrides) -> dict:
        row = {"project": "midas-rwa", "symbol": "USDC", "poolMeta": "mTBILL",
               "chain": "Ethereum", "underlyingTokens": [MTBILL_TOKEN]}
        row.update(overrides)
        return row

    async def test_pool_without_a_uuid_has_no_history(self, registry: StrategyRegistry) -> None:
        strategy = await _register_midas_with_llama(registry, _llama_client(None), "")

        assert strategy._defillama_pool_id is None
        assert await strategy.get_apy_history() == []

    async def test_mtbill_is_wired_in(self, registry: StrategyRegistry) -> None:
        llama = _llama_client(self._midas_row())

        strategy = await _register_midas_with_llama(
            registry, llama, '{"0xab12": "%s"}' % MIDAS_LLAMA_UUID
        )

        assert strategy._defillama_pool_id == MIDAS_LLAMA_UUID

    async def test_ethereum_row_accepted_though_we_hold_on_base(
        self, registry: StrategyRegistry
    ) -> None:
        # The fund's APY is chain-independent; the Ethereum row is the right
        # source for our Base-held mTBILL, so chain must NOT disable it.
        llama = _llama_client(self._midas_row(chain="Ethereum"))

        strategy = await _register_midas_with_llama(
            registry, llama, '{"0xab12": "%s"}' % MIDAS_LLAMA_UUID
        )

        assert strategy._defillama_pool_id == MIDAS_LLAMA_UUID

    async def test_defillama_casing_still_matches(self, registry: StrategyRegistry) -> None:
        # poolMeta casing can vary; the assert is case-insensitive so that alone
        # must not disable the chart.
        llama = _llama_client(self._midas_row(poolMeta="MTBILL"))

        strategy = await _register_midas_with_llama(
            registry, llama, '{"0xab12": "%s"}' % MIDAS_LLAMA_UUID
        )

        assert strategy._defillama_pool_id == MIDAS_LLAMA_UUID

    async def test_wrong_midas_product_is_rejected(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        # Same project + underlying, but mBASIS is a different Midas product with
        # its own APY (5.39% vs mTBILL's 4.37%). Serving its curve under the
        # mTBILL pool would be a confident lie.
        llama = _llama_client(self._midas_row(poolMeta="mBASIS", chain="Base"))

        strategy = await _register_midas_with_llama(
            registry, llama, '{"0xab12": "%s"}' % MIDAS_LLAMA_UUID
        )

        assert strategy._defillama_pool_id is None
        assert any("not product" in r.message for r in caplog.records)

    async def test_wrong_project_is_rejected(
        self, registry: StrategyRegistry, caplog
    ) -> None:
        llama = _llama_client(self._midas_row(project="midas"))

        strategy = await _register_midas_with_llama(
            registry, llama, '{"0xab12": "%s"}' % MIDAS_LLAMA_UUID
        )

        assert strategy._defillama_pool_id is None
        assert any("not project" in r.message for r in caplog.records)
