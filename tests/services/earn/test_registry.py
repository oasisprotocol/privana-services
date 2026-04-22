from unittest.mock import MagicMock

import pytest

from src.services.earn.registry import (
    StrategyRegistry,
    get_strategy_registry,
    reset_strategy_registry,
)
from src.services.earn.strategies.manual import ManualStrategy


POOL_ID = "abc123"
POOL_ID_PREFIXED = "0xABC123"


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
