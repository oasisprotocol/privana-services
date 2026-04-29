import json
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


def register_aave_strategies_from_config(registry: StrategyRegistry, raw_config: str) -> int:
    """Parse `AAVE_POOL_ASSETS` JSON and register an AaveStrategy per pool.

    Canonical config format:
        `{"<pool_id_hex>": {"token_id": "<bytes32>", "asset_address": "<addr>"}, ...}`.

    Legacy flat form `{"<pool_id_hex>": "<asset_address>"}` is detected and
    skipped with a clear error: token_id is required because the strategy
    needs to bridge accounting funds across chains via the SDK. Operators
    must migrate to the nested form.

    Empty or whitespace-only input is treated as "no Aave pools configured"
    and short-circuits.

    Returns the number of pools registered. Failures (bad JSON, missing
    fields, legacy entries) are logged but do not crash the app: the
    registry just falls back to ManualStrategy for the affected pools.
    """
    if not raw_config.strip():
        logger.info("AAVE_POOL_ASSETS not configured; skipping Aave strategy registration")
        return 0

    try:
        pool_assets = json.loads(raw_config)
    except json.JSONDecodeError:
        logger.exception("AAVE_POOL_ASSETS contains invalid JSON; skipping registration")
        return 0

    if not isinstance(pool_assets, dict):
        logger.error("AAVE_POOL_ASSETS must be a JSON object; got %s", type(pool_assets).__name__)
        return 0

    from src.clients.aave import get_aave_client
    from src.services.earn.strategies.aave import AaveStrategy

    client = get_aave_client()
    count = 0
    for pool_id, entry in pool_assets.items():
        if isinstance(entry, str):
            logger.error(
                "AAVE_POOL_ASSETS pool=%s uses legacy flat shape; expected "
                "{'token_id': ..., 'asset_address': ...}; skipping",
                pool_id,
            )
            continue
        if not isinstance(entry, dict):
            logger.error(
                "AAVE_POOL_ASSETS pool=%s must be an object with token_id+asset_address; got %s",
                pool_id, type(entry).__name__,
            )
            continue

        token_id = entry.get("token_id")
        asset_address = entry.get("asset_address")
        if not isinstance(token_id, str) or not token_id:
            logger.error("AAVE_POOL_ASSETS pool=%s has invalid token_id; skipping", pool_id)
            continue
        if not isinstance(asset_address, str) or not asset_address:
            logger.error("AAVE_POOL_ASSETS pool=%s has invalid asset_address; skipping", pool_id)
            continue

        registry.register(
            pool_id,
            AaveStrategy(client=client, asset_address=asset_address, token_id=token_id),
        )
        logger.info(
            "Registered AaveStrategy pool=%s asset=%s token=%s",
            pool_id, asset_address, token_id,
        )
        count += 1
    return count
