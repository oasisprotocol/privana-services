from __future__ import annotations

from typing import Optional

from flexvaults import FlexvaultsClient

from src.core.config import load_settings

_client: Optional[FlexvaultsClient] = None


def get_flexvaults_client() -> FlexvaultsClient:
    global _client
    if _client is None:
        settings = load_settings()
        _client = FlexvaultsClient(base_url=settings.accounting_api_base_url)
    return _client


def reset_flexvaults_client() -> None:
    global _client
    _client = None


__all__ = ["get_flexvaults_client", "reset_flexvaults_client"]
