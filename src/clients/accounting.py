import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

from src.core.abi import load_abi
from src.core.config import load_settings
from src.models.common import (
    Balance,
    HistoryEntry,
    TokenInfo,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 1.0
JWT_SIWE_CACHE_SKEW_SECONDS = 30
HISTORY_PAGE_LIMIT = 100


@dataclass(frozen=True)
class _JwtSiweAuth:
    siwe_token: str
    address: str
    expires_at: float


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
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500 or attempt == MAX_RETRIES - 1:
                raise
            logger.warning(
                f"Accounting API returned {exc.response.status_code} "
                f"(attempt {attempt + 1}), retrying"
            )
            await asyncio.sleep(RETRY_DELAY * (attempt + 1))


class AccountingClient:
    def __init__(self) -> None:
        settings = load_settings()
        self.base_url = settings.privana_api_base_url
        self.client = httpx.AsyncClient(timeout=30.0)
        self._lp_address = settings.liquidity_provider_address
        self._lp_secret_key = settings.liquidity_provider_secret_key
        self._chain_id = settings.accounting_chain_id
        self._siwe_token: Optional[str] = None
        self._jwt_token: Optional[str] = None
        self._auth_timestamp: Optional[float] = None
        self._jwt_siwe_cache: dict[str, _JwtSiweAuth] = {}
        self._jwt_siwe_lock = asyncio.Lock()
        self._accounting_contract = None

    async def _ensure_authenticated(self) -> None:
        if self._siwe_token and self._jwt_token and self._auth_timestamp:
            elapsed = asyncio.get_event_loop().time() - self._auth_timestamp
            if elapsed < 3600:
                return

        account = Account.from_key(self._lp_secret_key)
        r = await self.client.get(
            f"{self.base_url}/v1/accounting/auth/nonce?address={self._lp_address}"
        )
        r.raise_for_status()
        nonce = r.json()["nonce"]

        now = datetime.now(timezone.utc)
        domain = self.base_url.replace("https://", "").replace("http://", "").rstrip("/")
        siwe = (
            f"{domain} wants you to sign in with your Ethereum account:\n"
            f"{self._lp_address}\n\nSign in to Privana on chain {self._chain_id}\n\n"
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
        return {"Authorization": f"Bearer {self._jwt_token}"}

    async def _authenticated_request(self, method: str, url: str) -> httpx.Response:
        await self._ensure_authenticated()
        for attempt in range(MAX_RETRIES):
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
            if response.status_code >= 500 and attempt < MAX_RETRIES - 1:
                logger.warning(
                    f"Accounting API returned {response.status_code} "
                    f"(attempt {attempt + 1}), retrying"
                )
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                continue
            response.raise_for_status()
            return response

    async def get_lp_balance(self, token_id: str) -> Balance:
        response = await self._authenticated_request(
            "GET",
            f"{self.base_url}/v1/accounting/balances/{token_id}",
        )
        return Balance(**response.json())

    async def get_token_info(self, token_id: str) -> TokenInfo:
        response = await _request_with_retry(
            self.client, "GET",
            f"{self.base_url}/v1/accounting/tokens/{token_id}",
        )
        return TokenInfo(**response.json())

    async def _exchange_jwt_for_siwe_auth(self, jwt_token: str) -> _JwtSiweAuth:
        jwt_token = jwt_token.strip()
        if not jwt_token:
            raise ValueError("JWT bearer token is required")

        cache_key = hashlib.sha256(jwt_token.encode("utf-8")).hexdigest()
        loop = asyncio.get_event_loop()
        now = loop.time()
        cached = self._jwt_siwe_cache.get(cache_key)
        if cached and cached.expires_at > now:
            return cached

        async with self._jwt_siwe_lock:
            now = loop.time()
            cached = self._jwt_siwe_cache.get(cache_key)
            if cached and cached.expires_at > now:
                return cached

            response = await self.client.post(
                f"{self.base_url}/v1/accounting/auth/jwt/siwe-token",
                headers={"Authorization": f"Bearer {jwt_token}"},
            )
            response.raise_for_status()
            data = response.json()
            siwe_token = data["siwe_token"]
            address = data["address"]
            expires_in = int(data["expires_in"])
            if not siwe_token or expires_in <= 0:
                raise RuntimeError("Accounting JWT exchange returned an invalid token")
            if not Web3.is_address(address):
                raise RuntimeError("Accounting JWT exchange returned an invalid address")

            for key, auth in list(self._jwt_siwe_cache.items()):
                if auth.expires_at <= now:
                    del self._jwt_siwe_cache[key]

            cache_ttl = expires_in - JWT_SIWE_CACHE_SKEW_SECONDS
            auth = _JwtSiweAuth(
                siwe_token=siwe_token,
                address=Web3.to_checksum_address(address),
                expires_at=now + cache_ttl,
            )
            if cache_ttl > 0:
                self._jwt_siwe_cache[cache_key] = auth
            else:
                self._jwt_siwe_cache.pop(cache_key, None)
            return auth

    async def exchange_jwt_for_siwe_token(self, jwt_token: str) -> str:
        return (await self._exchange_jwt_for_siwe_auth(jwt_token)).siwe_token

    async def get_jwt_user_address(self, jwt_token: str) -> str:
        return (await self._exchange_jwt_for_siwe_auth(jwt_token)).address

    async def get_user_history(self, siwe_token: str) -> list[HistoryEntry]:
        """Page through the caller's full activity history, oldest first.

        The endpoint pages by page number, not row offset: page 0 is the
        oldest page, -1 the latest. Reading forward from page 0 keeps the
        result stable while new entries append — they only extend the final
        page. ``total`` is re-read per page so growth during the walk still
        terminates, and a short page ends the loop even if ``total`` lies.
        """
        headers = {"X-SIWE-Token": siwe_token}
        entries: list[HistoryEntry] = []
        page_index = 0
        while True:
            response = await _request_with_retry(
                self.client,
                "GET",
                f"{self.base_url}/v1/accounting/history"
                f"?offset={page_index}&limit={HISTORY_PAGE_LIMIT}",
                headers=headers,
            )
            data = response.json()
            page = [HistoryEntry(**entry) for entry in data["history"]]
            entries.extend(page)
            if len(page) < HISTORY_PAGE_LIMIT or len(entries) >= int(data["total"]):
                break
            page_index += 1
        entries.sort(key=lambda entry: entry.timestamp)
        return entries

    async def get_transfer_nonce(self, user_address: str) -> int:
        """Read ``transferNonces[user]`` directly from the Accounting contract.

        Bypasses the ROFL REST endpoint because the staged service has been
        observed returning 0 even when the on-chain value is non-zero (e.g.
        after swap activity). The chain is the source of truth — signing a
        Transfer with the wrong nonce reverts as ``InvalidNonce``, so we read
        from where the verifier reads.
        """
        if self._accounting_contract is None:
            settings = load_settings()
            w3 = Web3(Web3.HTTPProvider(settings.sapphire_rpc_url))
            self._accounting_contract = w3.eth.contract(
                address=Web3.to_checksum_address(settings.accounting_contract_address),
                abi=load_abi("Accounting"),
            )

        return await asyncio.to_thread(
            self._accounting_contract.functions.transferNonces(
                Web3.to_checksum_address(user_address)
            ).call
        )

    async def close(self) -> None:
        await self.client.aclose()


_client_instance: Optional[AccountingClient] = None


def get_accounting_client() -> AccountingClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = AccountingClient()
    return _client_instance
