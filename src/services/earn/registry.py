import logging
from typing import Optional

from src.services.earn.strategies.base import BaseStrategy
from src.services.earn.strategies.manual import ManualStrategy

logger = logging.getLogger(__name__)


class StrategyRegistry:
    """Maps a pool_id (hex string, no 0x prefix) to the BaseStrategy instance
    that handles its external yield flow.

    Why a registry: the vault service must stay protocol-agnostic. Adding a
    new protocol later (Compound, Morpho, Pendle, ...) should be a new file
    under strategies/ plus a single registration call, not an edit to
    vault_service.py.

    Missing pool_id falls back to ManualStrategy so pools without a
    configured adapter continue to work as they did before Sprint 4.
    TODO: decide whether the fallback should be an error instead once
    all pools are expected to have strategies declared.
    """

    def __init__(self) -> None:
        self._strategies: dict[str, BaseStrategy] = {}
        self._default: BaseStrategy = ManualStrategy()

    def register(self, pool_id: str, strategy: BaseStrategy) -> None:
        key = self._normalize(pool_id)
        if key in self._strategies:
            logger.warning(
                "StrategyRegistry: overwriting strategy for pool=%s old=%s new=%s",
                key, self._strategies[key].name, strategy.name,
            )
        self._strategies[key] = strategy

    def get(self, pool_id: str) -> BaseStrategy:
        key = self._normalize(pool_id)
        strategy = self._strategies.get(key)
        if strategy is None:
            logger.debug(
                "StrategyRegistry: no strategy for pool=%s, using default=%s",
                key, self._default.name,
            )
            return self._default
        return strategy

    def has(self, pool_id: str) -> bool:
        return self._normalize(pool_id) in self._strategies

    def pool_ids(self) -> list[str]:
        return list(self._strategies.keys())

    @staticmethod
    def _normalize(pool_id: str) -> str:
        return pool_id.removeprefix("0x").lower()


_registry_instance: Optional[StrategyRegistry] = None


def get_strategy_registry() -> StrategyRegistry:
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = StrategyRegistry()
    return _registry_instance


def reset_strategy_registry() -> None:
    """Test hook. Clears the module-level singleton so each test gets a
    fresh registry.
    """
    global _registry_instance
    _registry_instance = None
