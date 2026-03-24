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

    async def get_routes(
        self,
        from_chain_id: int,
        to_chain_id: int,
        from_token_address: str,
        to_token_address: str,
        from_amount: str,
    ) -> dict[str, Any]:
        payload = {
            "fromChainId": from_chain_id,
            "toChainId": to_chain_id,
            "fromTokenAddress": from_token_address,
            "toTokenAddress": to_token_address,
            "fromAmount": from_amount,
        }
        logger.info(f"Li.Fi routes request: {payload}")
        response = await self.client.post(
            f"{self.api_url}/advanced/routes", json=payload
        )
        if response.status_code != 200:
            logger.error(
                f"Li.Fi routes failed: {response.status_code} - {response.text}"
            )
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
