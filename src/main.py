import logging
from contextlib import asynccontextmanager

from eth_account import Account
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.common import router as common_router
from src.api.earn import router as earn_router
from src.api.operations import router as operations_router
from src.api.portfolio import router as portfolio_router
from src.api.swap import router as swap_router
from src.core.config import load_settings
from src.core.db import close_db, get_db
from src.core.validation import validate_token_id

logger = logging.getLogger(__name__)

settings = load_settings()

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger().setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _validate_settings() -> None:
    errors = []
    if not settings.liquidity_provider_secret_key:
        errors.append("LIQUIDITY_PROVIDER_SECRET_KEY is not set")
    if not settings.accounting_contract_address or settings.accounting_contract_address == _ZERO_ADDRESS:
        errors.append("ACCOUNTING_CONTRACT_ADDRESS is not set")
    if not settings.swap_manager_contract_address or settings.swap_manager_contract_address == _ZERO_ADDRESS:
        errors.append("SWAP_MANAGER_CONTRACT_ADDRESS is not set")
    if not settings.earn_manager_contract_address or settings.earn_manager_contract_address == _ZERO_ADDRESS:
        errors.append("EARN_MANAGER_CONTRACT_ADDRESS is not set")
    if not settings.privana_api_base_url:
        errors.append("PRIVANA_API_BASE_URL is not set")
    if not settings.base_rpc_url:
        errors.append("BASE_RPC_URL is not set")
    # Pool payouts and moves sign as the earn account: never the LP's float.
    if settings.earn_pool_address.lower() == settings.liquidity_provider_address.lower() != "":
        errors.append("EARN_POOL_SECRET_KEY is the LP's key; earn needs its own account")
    elif not settings.earn_pool_address and (settings.aave_pool_assets or settings.midas_pool_assets):
        errors.append("EARN_POOL_SECRET_KEY is not set but earn pools are configured")
    if not settings.internal_swap_token_ids:
        errors.append("INTERNAL_SWAP_TOKEN_IDS is not set")
    for token_id in sorted(settings.internal_swap_token_ids):
        try:
            validate_token_id(token_id, f"INTERNAL_SWAP_TOKEN_IDS entry {token_id!r}")
        except ValueError as exc:
            errors.append(str(exc))
    try:
        from src.core.fee_policy import get_fee_policies

        get_fee_policies(refresh=True)
    except ValueError as exc:
        errors.append(str(exc))
    if errors and settings.environment.lower() != "development":
        raise RuntimeError(
            "Missing required configuration:\n  - " + "\n  - ".join(errors)
        )
    elif errors:
        for e in errors:
            logger.warning(f"Config warning: {e}")


def _earn_signs_as_lp() -> bool:
    """EarnManager calls sign as the pool admin, or the LP when it has no key
    of its own. The LP's Sapphire nonce belongs to its sender, so earn must
    not write as the LP next to it."""
    key = settings.pool_admin_secret_key or settings.liquidity_provider_secret_key
    return bool(key) and Account.from_key(key).address.lower() == settings.liquidity_provider_address.lower()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from src.clients.accounting import get_accounting_client
    from src.clients.lifi import get_lifi_client
    from src.services.earn.cache import get_pool_list_cache
    from src.services.earn.idle_deployer import get_idle_deployer
    from src.services.earn.registry import (
        get_strategy_registry,
        register_aave_strategies_from_config,
        register_midas_strategies_from_config,
    )
    from src.services.earn.worker import get_earn_worker
    from src.services.pool_rate_history import get_pool_rate_sampler
    from src.services.price_history import get_price_sampler

    logger.info("Privana services starting...")

    _validate_settings()
    get_db()

    try:
        registered = await register_aave_strategies_from_config(
            get_strategy_registry(),
            settings.aave_pool_assets,
            settings.defillama_pool_ids,
        )
        if registered:
            logger.info("Earn strategy registry: %d Aave pool(s) registered", registered)
    except Exception:
        logger.exception("Aave strategy registration failed; affected pools fall back to manual")

    try:
        registered = await register_midas_strategies_from_config(
            get_strategy_registry(),
            settings.midas_pool_assets,
            settings.defillama_pool_ids,
        )
        if registered:
            logger.info("Earn strategy registry: %d Midas pool(s) registered", registered)
    except Exception:
        logger.exception("Midas strategy registration failed; affected pools fall back to manual")

    from src.services.swap.worker import get_swap_worker

    swap_worker = get_swap_worker()
    await swap_worker.start()

    from src.clients.evm_batcher import start_evm_batchers, stop_evm_batchers
    from src.clients.privana_lanes import (
        run_transfer_lanes,
        start_withdraw_lanes,
        stop_withdraw_lanes,
    )

    start_evm_batchers(run_transfer_lanes)
    start_withdraw_lanes()

    try:
        await get_price_sampler().start()
    except Exception:
        logger.exception("Price sampler failed to start; no price history will be recorded")

    try:
        await get_pool_rate_sampler().start()
    except Exception:
        logger.exception("Pool rate sampler failed to start; no earn rate history will be recorded")

    try:
        await get_pool_list_cache().start()
    except Exception:
        logger.exception("Earn pool listing cache failed to start")

    if _earn_signs_as_lp():
        logger.error("Earn worker and idle deployer not started: the pool admin is the LP, whose Sapphire "
                     "nonce its sender owns; queued earn operations will not run")
    else:
        try:
            await get_earn_worker().start()
        except Exception:
            logger.exception("Earn worker failed to start; queued earn operations will not run")

        try:
            await get_idle_deployer().start()
        except Exception:
            logger.exception("Idle deployer failed to start; seeded funds will sit undeployed")

    try:
        yield
    finally:
        await swap_worker.stop()

    try:
        await get_price_sampler().stop()
    except Exception:
        logger.warning("Error stopping price sampler")

    try:
        await get_pool_rate_sampler().stop()
    except Exception:
        logger.warning("Error stopping pool rate sampler")
    try:
        await get_pool_list_cache().stop()
    except Exception:
        logger.warning("Error stopping earn pool listing cache")
    try:
        await get_idle_deployer().stop()
    except Exception:
        logger.warning("Error stopping idle deployer")
    try:
        await get_earn_worker().stop()
    except Exception:
        logger.warning("Error stopping earn worker")
    await stop_withdraw_lanes()
    try:
        await get_accounting_client().close()
    except Exception:
        logger.warning("Error closing accounting client")
    try:
        await get_lifi_client().close()
    except Exception:
        logger.warning("Error closing Li.Fi client")

    stop_evm_batchers()
    close_db()
    logger.info("Privana services shut down")


app = FastAPI(
    title="Privana Services",
    description="Privana Services for DeFi",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(common_router)
app.include_router(swap_router)
app.include_router(earn_router)
app.include_router(operations_router)
app.include_router(portfolio_router)


@app.get("/health")
async def health():
    from src.clients.sapphire import get_sapphire_client

    checks = {"api": "ok"}
    try:
        sapphire = get_sapphire_client()
        checks["sapphire"] = "ok" if sapphire.is_connected() else "unavailable"
    except Exception:
        checks["sapphire"] = "unavailable"

    from src.clients.evm_batcher import STUCK_AFTER_SEC, sender_health
    from src.clients.privana_lanes import lane_health

    senders, lanes = sender_health(), lane_health()
    # A dead sender thread or a write waiting past the stuck window needs a person.
    waits = [*senders["oldest_queued_sec"].values(), *lanes.values()]
    checks["senders"] = "ok" if senders["threads_alive"] == senders["threads"] else "thread stopped"
    checks["queues"] = "ok" if all(w < STUCK_AFTER_SEC for w in waits) else "stalled"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "checks": checks, "senders": senders, "lanes": lanes}


def main():
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.environment.lower() == "development",
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
