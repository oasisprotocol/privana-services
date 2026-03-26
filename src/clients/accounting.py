import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

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
        self._lp_address = settings.liquidity_provider_address
        self._lp_private_key = settings.liquidity_provider_private_key
        self._chain_id = settings.accounting_chain_id
        self._siwe_token: Optional[str] = None
        self._jwt_token: Optional[str] = None
        self._auth_timestamp: Optional[float] = None

    async def _ensure_authenticated(self) -> None:
        if self._siwe_token and self._jwt_token and self._auth_timestamp:
            elapsed = asyncio.get_event_loop().time() - self._auth_timestamp
            if elapsed < 3600:
                return

        account = Account.from_key(self._lp_private_key)
        r = await self.client.get(
            f"{self.base_url}/v1/accounting/auth/nonce?address={self._lp_address}"
        )
        r.raise_for_status()
        nonce = r.json()["nonce"]

        now = datetime.now(timezone.utc)
        domain = self.base_url.replace("https://", "").replace("http://", "").rstrip("/")
        siwe = (
            f"{domain} wants you to sign in with your Ethereum account:\n"
            f"{self._lp_address}\n\nSign in to FlexVaults\n\n"
            f"URI: {self.base_url}\n"
            f"Version: 1\nChain ID: {self._chain_id}\nNonce: {nonce}\n"
            f"Issued At: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            f"Expiration Time: {(now + timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )
        signed = account.sign_message(encode_defunct(text=siwe))
        r = await self.client.post(
            f"{self.base_url}/v1/accounting/auth/login",
            json={"siwe_message": siwe, "signature": f"0x{signed.signature.hex()}"},
        )
        r.raise_for_status()
        data = r.json()
        self._siwe_token = data["siwe_token"]
        self._jwt_token = data["jwt_access_token"]
        self._auth_timestamp = asyncio.get_event_loop().time()

    def _auth_headers(self) -> dict:
        return {
            "X-SIWE-Token": self._siwe_token,
            "Authorization": f"Bearer {self._jwt_token}",
        }

    async def _authenticated_request(self, method: str, url: str) -> httpx.Response:
        await self._ensure_authenticated()
        response = await self.client.request(method, url, headers=self._auth_headers())
        is_auth_error = response.status_code == 401 or (
            response.status_code == 400 and "SIWE" in response.text
        )
        if is_auth_error:
            self._siwe_token = None
            self._jwt_token = None
            self._auth_timestamp = None
            await self._ensure_authenticated()
            response = await self.client.request(method, url, headers=self._auth_headers())
        response.raise_for_status()
        return response

    async def get_lp_balance(self, token_id: str) -> Balance:
        response = await self._authenticated_request(
            "GET",
            f"{self.base_url}/v1/accounting/balances/{self._lp_address}/{token_id}",
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
