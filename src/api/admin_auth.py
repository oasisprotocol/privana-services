import hmac
from typing import Optional

from fastapi import Header, HTTPException, status

from src.core.config import load_settings


def require_admin(x_admin_api_key: Optional[str] = Header(None)) -> None:
    """FastAPI dependency — gates admin endpoints behind a static API key.

    The key is read from the ADMIN_API_KEY env var. If unset, all admin
    endpoints are locked (fail-closed). Uses hmac.compare_digest to avoid
    timing side channels on key comparison.
    """
    expected = load_settings().admin_api_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API is not configured",
        )
    if x_admin_api_key is None or not hmac.compare_digest(x_admin_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin API key",
        )
