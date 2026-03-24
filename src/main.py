import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes import router
from src.config import load_settings
from src.db import close_db, get_db

logger = logging.getLogger(__name__)

settings = load_settings()

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger().setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _validate_settings() -> None:
    errors = []
    if settings.liquidity_provider_address == _ZERO_ADDRESS:
        errors.append("LIQUIDITY_PROVIDER_ADDRESS is not set")
    if not settings.liquidity_provider_private_key:
        errors.append("LIQUIDITY_PROVIDER_PRIVATE_KEY is not set")
    if settings.accounting_contract_address == _ZERO_ADDRESS:
        errors.append("ACCOUNTING_CONTRACT_ADDRESS is not set")
    if settings.liq_manager_contract_address == _ZERO_ADDRESS:
        errors.append("LIQ_MANAGER_CONTRACT_ADDRESS is not set")
    if not settings.accounting_api_base_url:
        errors.append("ACCOUNTING_API_BASE_URL is not set")
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

    logger.info("FlexVaults Swap starting...")

    _validate_settings()
    get_db()

    yield

    try:
        await get_accounting_client().close()
    except Exception:
        logger.warning("Error closing accounting client")
    try:
        await get_lifi_client().close()
    except Exception:
        logger.warning("Error closing Li.Fi client")

    close_db()
    logger.info("FlexVaults Swap shut down")


app = FastAPI(
    title="FlexVaults Swap",
    description="Order routing service for FlexVaults token swaps",
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

app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}


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
