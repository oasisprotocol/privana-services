import logging
from typing import Any, Optional

import httpx

from src.config import load_settings

logger = logging.getLogger(__name__)


class LiFiClient:
    def __init__(self) -> None:
        settings = load_settings()
        self.api_url = settings.lifi_api_url.rstrip("/")
        self.integrator = settings.lifi_integrator
        headers = {"accept": "application/json"}
        if settings.lifi_api_key:
            headers["x-lifi-api-key"] = settings.lifi_api_key
        self.client = httpx.AsyncClient(timeout=30.0, headers=headers)

    async def get_quote(
        self,
        from_chain: int,
        to_chain: int,
        from_token: str,
        to_token: str,
        from_amount: str,
        from_address: str,
        slippage: float = 0.03,
    ) -> dict[str, Any]:
        params = {
            "fromChain": str(from_chain),
            "toChain": str(to_chain),
            "fromToken": from_token,
            "toToken": to_token,
            "fromAmount": from_amount,
            "fromAddress": from_address,
            "slippage": str(slippage),
            "integrator": self.integrator,
        }
        logger.info(f"Li.Fi quote request: {params}")
        response = await self.client.get(f"{self.api_url}/quote", params=params)
        if response.status_code != 200:
            logger.error(f"Li.Fi quote failed: {response.status_code} - {response.text}")
        response.raise_for_status()
        return response.json()

    async def get_status(
        self,
        tx_hash: str,
        from_chain: Optional[int] = None,
        to_chain: Optional[int] = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {"txHash": tx_hash}
        if from_chain is not None:
            params["fromChain"] = str(from_chain)
        if to_chain is not None:
            params["toChain"] = str(to_chain)
        response = await self.client.get(f"{self.api_url}/status", params=params)
        response.raise_for_status()
        return response.json()

    async def get_tokens(self) -> dict[str, Any]:
        response = await self.client.get(f"{self.api_url}/tokens")
        response.raise_for_status()
        return response.json()

    async def get_chains(self) -> dict[str, Any]:
        response = await self.client.get(f"{self.api_url}/chains")
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        await self.client.aclose()


_client_instance: Optional[LiFiClient] = None


def get_lifi_client() -> LiFiClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = LiFiClient()
    return _client_instance
