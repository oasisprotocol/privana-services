import logging
from typing import Optional

import httpx

from src.config import load_settings
from src.models.accounting import (
    Balance,
    TokenInfo,
)

logger = logging.getLogger(__name__)


class AccountingClient:
    def __init__(self) -> None:
        settings = load_settings()
        self.base_url = settings.accounting_api_base_url
        self.client = httpx.AsyncClient(timeout=30.0)

    async def get_balance(self, user_address: str, token_id: str) -> Balance:
        response = await self.client.get(
            f"{self.base_url}/v1/accounting/balances/{user_address}/{token_id}"
        )
        response.raise_for_status()
        return Balance(**response.json())

    async def get_token_info(self, token_id: str) -> TokenInfo:
        response = await self.client.get(
            f"{self.base_url}/v1/accounting/tokens/{token_id}"
        )
        response.raise_for_status()
        return TokenInfo(**response.json())

    async def get_transfer_nonce(self, user_address: str) -> int:
        response = await self.client.get(
            f"{self.base_url}/v1/accounting/funds/transfer/nonce/{user_address}"
        )
        response.raise_for_status()
        return response.json()["nonce"]

    async def close(self) -> None:
        await self.client.aclose()


_client_instance: Optional[AccountingClient] = None


def get_accounting_client() -> AccountingClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = AccountingClient()
    return _client_instance
