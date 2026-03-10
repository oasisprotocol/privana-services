import logging
from typing import Optional

import httpx

from src.config import load_settings
from src.models.accounting import (
    AccountingBalance,
    AccountingLockedFundsResponse,
    AccountingSubmissionResponse,
    AccountingTokenInfo,
)

logger = logging.getLogger(__name__)


class AccountingClient:
    def __init__(self) -> None:
        settings = load_settings()
        self.base_url = settings.accounting_api_base_url
        self.client = httpx.AsyncClient(timeout=30.0)

    async def get_balance(self, user_address: str, token_id: str) -> AccountingBalance:
        response = await self.client.get(
            f"{self.base_url}/v1/accounting/balances/{user_address}/{token_id}"
        )
        response.raise_for_status()
        return AccountingBalance(**response.json())

    async def get_token_info(self, token_id: str) -> AccountingTokenInfo:
        response = await self.client.get(
            f"{self.base_url}/v1/accounting/tokens/{token_id}"
        )
        response.raise_for_status()
        return AccountingTokenInfo(**response.json())

    async def lock_funds(
        self,
        user_address: str,
        service_address: str,
        token_id: str,
        amount: int,
        expiry: int,
        signature: str,
    ) -> AccountingSubmissionResponse:
        payload = {
            "user_address": user_address,
            "service_address": service_address,
            "token_id": token_id,
            "amount": amount,
            "expiry": expiry,
            "signature": signature,
        }
        response = await self.client.post(
            f"{self.base_url}/v1/accounting/funds/lock",
            json=payload,
        )
        if response.status_code != 200:
            logger.error(f"Lock funds failed: {response.status_code} - {response.text}")
        response.raise_for_status()
        return AccountingSubmissionResponse(**response.json())

    async def unlock_funds(self, user_address: str, lock_id: int) -> AccountingSubmissionResponse:
        payload = {"user_address": user_address, "lock_id": lock_id}
        response = await self.client.post(
            f"{self.base_url}/v1/accounting/funds/unlock",
            json=payload,
        )
        response.raise_for_status()
        return AccountingSubmissionResponse(**response.json())

    async def get_locked_funds(
        self, user_address: str, service_address: Optional[str] = None
    ) -> AccountingLockedFundsResponse:
        params = {}
        if service_address:
            params["service_address"] = service_address
        response = await self.client.get(
            f"{self.base_url}/v1/accounting/funds/locked/{user_address}",
            params=params,
        )
        response.raise_for_status()
        return AccountingLockedFundsResponse(**response.json())

    async def relay_execute(
        self,
        chain_id: int,
        to: str,
        data: str,
        value: int = 0,
        gas_limit: int = 200_000,
    ) -> AccountingSubmissionResponse:
        payload = {
            "chain_id": chain_id,
            "to": to,
            "data": data,
            "value": value,
            "gas_limit": gas_limit,
        }
        response = await self.client.post(
            f"{self.base_url}/v1/accounting/relay/execute",
            json=payload,
        )
        if response.status_code != 200:
            logger.error(f"Relay execute failed: {response.status_code} - {response.text}")
        response.raise_for_status()
        return AccountingSubmissionResponse(**response.json())

    async def relay_settle_swap(
        self,
        user_address: str,
        lock_id: int,
        output_token_id: str,
        output_amount: int,
        swap_tx_hash: Optional[str] = None,
    ) -> AccountingSubmissionResponse:
        payload = {
            "user_address": user_address,
            "lock_id": lock_id,
            "output_token_id": output_token_id,
            "output_amount": output_amount,
        }
        if swap_tx_hash:
            payload["swap_tx_hash"] = swap_tx_hash
        response = await self.client.post(
            f"{self.base_url}/v1/accounting/relay/settle-swap",
            json=payload,
        )
        if response.status_code != 200:
            logger.error(f"Relay settle failed: {response.status_code} - {response.text}")
        response.raise_for_status()
        return AccountingSubmissionResponse(**response.json())

    async def relay_status(self, tx_hash: str) -> AccountingSubmissionResponse:
        response = await self.client.get(
            f"{self.base_url}/v1/accounting/relay/status/{tx_hash}"
        )
        response.raise_for_status()
        return AccountingSubmissionResponse(**response.json())

    async def close(self) -> None:
        await self.client.aclose()


_client_instance: Optional[AccountingClient] = None


def get_accounting_client() -> AccountingClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = AccountingClient()
    return _client_instance
