from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.api import TokenInfo
from src.models.settings import Settings
from src.services.earn.registry import (
    StrategyRegistry,
    get_strategy_registry,
    register_aave_strategies_from_config,
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
    return Settings(
        liquidity_provider_secret_key=(
            "0x7b07a59f24f1900ec4e6ac3e521c1acd2cca3518f717abda1dc8bbcbbc344c4e"
        ),
        liquidity_provider_address="0xd8991364507FAfC256EafF950d28618735753476",
        accounting_contract_address="0xFfB141bF8269E458b074A274bE6E8F971f08A401",
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
