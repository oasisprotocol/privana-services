import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.common import router as common_router
from src.api.earn import router as earn_router
from src.api.operations import router as operations_router
from src.api.swap import router as swap_router
from src.core.config import load_settings
from src.core.db import close_db, get_db

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
    if errors and settings.environment.lower() != "development":
        raise RuntimeError(
            "Missing required configuration:\n  - " + "\n  - ".join(errors)
        )
    elif errors:
        for e in errors:
            logger.warning(f"Config warning: {e}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from src.clients.accounting import get_accounting_client
    from src.clients.lifi import get_lifi_client
    from src.services.earn.registry import (
        get_strategy_registry,
        register_aave_strategies_from_config,
        register_midas_strategies_from_config,
    )
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

    if settings.lifi_execution_enabled:
        from src.services.swap.lifi_pipeline import recover_inflight_lifi_swaps

        asyncio.create_task(recover_inflight_lifi_swaps())

    try:
        await get_price_sampler().start()
    except Exception:
        logger.exception("Price sampler failed to start; no price history will be recorded")

    try:
        await get_pool_rate_sampler().start()
    except Exception:
        logger.exception("Pool rate sampler failed to start; no earn rate history will be recorded")

    yield

    try:
        await get_price_sampler().stop()
    except Exception:
        logger.warning("Error stopping price sampler")

    try:
        await get_pool_rate_sampler().stop()
    except Exception:
        logger.warning("Error stopping pool rate sampler")
    try:
        await get_accounting_client().close()
    except Exception:
        logger.warning("Error closing accounting client")
    try:
        await get_lifi_client().close()
    except Exception:
        logger.warning("Error closing Li.Fi client")

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


@app.get("/health")
async def health():
    from src.clients.sapphire import get_sapphire_client

    checks = {"api": "ok"}
    try:
        sapphire = get_sapphire_client()
        checks["sapphire"] = "ok" if sapphire.is_connected() else "unavailable"
    except Exception:
        checks["sapphire"] = "unavailable"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "checks": checks}


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
