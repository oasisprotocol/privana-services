import asyncio
import logging
from typing import Optional

import httpx

from src.config import load_settings
from src.models.accounting import (
    Balance,
    TokenInfo,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 1.0


async def _request_with_retry(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    for attempt in range(MAX_RETRIES):
        try:
            response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            logger.warning(f"Accounting API request failed (attempt {attempt + 1}), retrying: {exc}")
            await asyncio.sleep(RETRY_DELAY * (attempt + 1))


class AccountingClient:
    def __init__(self) -> None:
        settings = load_settings()
        self.base_url = settings.accounting_api_base_url
        self.client = httpx.AsyncClient(timeout=30.0)

    async def get_balance(self, user_address: str, token_id: str) -> Balance:
        response = await _request_with_retry(
            self.client, "GET",
            f"{self.base_url}/v1/accounting/balances/{user_address}/{token_id}",
        )
        return Balance(**response.json())

    async def get_token_info(self, token_id: str) -> TokenInfo:
        response = await _request_with_retry(
            self.client, "GET",
            f"{self.base_url}/v1/accounting/tokens/{token_id}",
        )
        return TokenInfo(**response.json())

    async def get_transfer_nonce(self, user_address: str) -> int:
        response = await _request_with_retry(
            self.client, "GET",
            f"{self.base_url}/v1/accounting/funds/transfer/nonce/{user_address}",
        )
        return response.json()["nonce"]

    async def close(self) -> None:
        await self.client.aclose()


_client_instance: Optional[AccountingClient] = None


def get_accounting_client() -> AccountingClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = AccountingClient()
    return _client_instance
