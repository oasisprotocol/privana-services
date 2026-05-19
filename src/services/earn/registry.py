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

    Missing pool_id falls back to ManualStrategy by design. This is a
    soft-fail safety net: a pool can exist on-chain before its off-chain
    adapter is registered (e.g. during a deploy/config window) and reads
    against it still succeed against the on-chain `totalAssets` snapshot.
    The trade-off is that a forgotten config silently routes through the
    no-op fallback rather than erroring loudly; startup logs make
    registered pool_ids visible so operators can verify coverage.
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


def _extract_token_id(entry: object, pool_id: str) -> Optional[str]:
    """Pull a token_id out of an AAVE_POOL_ASSETS entry, accepting both the
    new canonical form (a bare token_id string) and the legacy nested form
    (``{"token_id": ..., "asset_address": ...}``).

    The legacy ``asset_address`` field is now ignored: the registry asks
    accounting for the canonical address keyed by token_id, so the env value
    is at best redundant and at worst drifts silently. We log a deprecation
    warning whenever we see it.
    """
    if isinstance(entry, str):
        return entry or None
    if isinstance(entry, dict):
        token_id = entry.get("token_id")
        if "asset_address" in entry:
            logger.warning(
                "AAVE_POOL_ASSETS pool=%s: 'asset_address' is deprecated and "
                "ignored; the address is resolved via accounting.get_token_info now. "
                "Drop it from your env or replace the entry with just the token_id.",
                pool_id,
            )
        if isinstance(token_id, str) and token_id:
            return token_id
        return None
    logger.error(
        "AAVE_POOL_ASSETS pool=%s must be a token_id string or {'token_id': ...}; got %s",
        pool_id, type(entry).__name__,
    )
    return None


async def register_aave_strategies_from_config(registry: StrategyRegistry, raw_config: str) -> int:
    """Parse `AAVE_POOL_ASSETS` JSON and register an AaveStrategy per pool.

    Canonical config format (preferred):
        ``{"<pool_id_hex>": "<token_id>", ...}``.

    Legacy nested form ``{"<pool_id_hex>": {"token_id": "...", "asset_address": "..."}}``
    is still accepted; ``asset_address`` is ignored with a deprecation
    warning. Asset addresses are now resolved per pool via
    ``accounting.get_token_info(token_id)``, removing the drift risk between
    env config and the canonical accounting record.

    Empty or whitespace-only input is treated as "no Aave pools configured"
    and short-circuits.

    Returns the number of pools registered. Per-pool failures (bad shape,
    missing token_id, accounting lookup failure, missing token_address) are
    logged but do not crash the app: the registry just falls back to
    ManualStrategy for the affected pools.
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
    from src.clients.accounting import get_accounting_client
    from src.services.earn.strategies.aave import AaveStrategy

    client = get_aave_client()
    accounting = get_accounting_client()
    count = 0
    for pool_id, entry in pool_assets.items():
        token_id = _extract_token_id(entry, pool_id)
        if not token_id:
            logger.error("AAVE_POOL_ASSETS pool=%s has invalid token_id; skipping", pool_id)
            continue

        try:
            token_info = await accounting.get_token_info(token_id)
        except Exception:
            logger.exception(
                "AAVE_POOL_ASSETS pool=%s: failed to resolve token_id=%s via accounting; skipping",
                pool_id, token_id,
            )
            continue

        asset_address = token_info.token_address
        if not asset_address:
            logger.error(
                "AAVE_POOL_ASSETS pool=%s: accounting has no token_address for token_id=%s; skipping",
                pool_id, token_id,
            )
            continue

        registry.register(
            pool_id,
            AaveStrategy(client=client, asset_address=asset_address, token_id=token_id),
        )
        logger.info(
            "Registered AaveStrategy pool=%s asset=%s token=%s chain=%s",
            pool_id, asset_address, token_id, token_info.chain_id,
        )
        count += 1
    return count


async def register_midas_strategies_from_config(registry: StrategyRegistry, raw_config: str) -> int:
    """Parse ``MIDAS_POOL_ASSETS`` JSON and register a MidasStrategy per pool.

    Config format:
        ``{"<pool_id_hex>": "<token_id>", ...}``

    Unlike ``register_aave_strategies_from_config``, no legacy nested form is
    accepted: Midas is a new integration with no prior config shape to
    migrate from. The payment-token address (typically USDC on Base) is
    resolved per pool via ``accounting.get_token_info(token_id)`` and used as
    the strategy's ``asset_address``. The Midas-specific vault/oracle
    addresses come from global settings, not the per-pool config.

    Empty or whitespace-only input short-circuits with a debug log.

    Returns the number of pools registered. Per-pool failures (bad shape,
    accounting lookup failure, missing token_address) are logged but do not
    crash the app; affected pools fall back to ManualStrategy via the
    registry's default.
    """
    if not raw_config.strip():
        logger.info("MIDAS_POOL_ASSETS not configured; skipping Midas strategy registration")
        return 0

    try:
        pool_assets = json.loads(raw_config)
    except json.JSONDecodeError:
        logger.exception("MIDAS_POOL_ASSETS contains invalid JSON; skipping registration")
        return 0

    if not isinstance(pool_assets, dict):
        logger.error(
            "MIDAS_POOL_ASSETS must be a JSON object; got %s",
            type(pool_assets).__name__,
        )
        return 0

    from src.clients.accounting import get_accounting_client
    from src.clients.midas import get_midas_client
    from src.services.earn.strategies.midas import MidasStrategy

    client = get_midas_client()
    accounting = get_accounting_client()
    count = 0
    for pool_id, entry in pool_assets.items():
        if not isinstance(entry, str) or not entry:
            logger.error(
                "MIDAS_POOL_ASSETS pool=%s must be a token_id string; got %s",
                pool_id, type(entry).__name__,
            )
            continue

        token_id = entry

        try:
            token_info = await accounting.get_token_info(token_id)
        except Exception:
            logger.exception(
                "MIDAS_POOL_ASSETS pool=%s: failed to resolve token_id=%s via accounting; skipping",
                pool_id, token_id,
            )
            continue

        asset_address = token_info.token_address
        if not asset_address:
            logger.error(
                "MIDAS_POOL_ASSETS pool=%s: accounting has no token_address for token_id=%s; skipping",
                pool_id, token_id,
            )
            continue

        registry.register(
            pool_id,
            MidasStrategy(client=client, asset_address=asset_address, token_id=token_id),
        )
        logger.info(
            "Registered MidasStrategy pool=%s asset=%s token=%s chain=%s",
            pool_id, asset_address, token_id, token_info.chain_id,
        )
        count += 1
    return count
