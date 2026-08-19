from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
from fastapi import HTTPException, Request

from src.clients.accounting import JwtIdentity, get_accounting_client

logger = logging.getLogger(__name__)

T = TypeVar("T")

_ACCOUNTING_NETWORK_ERRORS = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.TimeoutException,
)


def auth_error(detail: str = "Bearer token required") -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def bearer_token(authorization: str) -> str:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise auth_error("Invalid bearer token")
    return token.strip()


async def resolve_via_accounting(
    call: Callable[[], Awaitable[T]],
    *,
    failure_detail: str,
    log_label: str,
) -> T:
    """Run an accounting auth lookup and map its failures to HTTP responses.

    401 and 403 pass through; any other failure (bad status, network error,
    malformed body) becomes 502 so one place owns the mapping for every
    JWT-bearing endpoint.
    """
    try:
        return await call()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise auth_error("Invalid bearer token") from exc
        if exc.response.status_code == 403:
            raise HTTPException(
                status_code=403,
                detail="Bearer token is not allowed",
            ) from exc
        logger.warning("%s failed with status %d", log_label, exc.response.status_code)
        raise HTTPException(status_code=502, detail=failure_detail) from exc
    except _ACCOUNTING_NETWORK_ERRORS as exc:
        logger.warning("%s request failed: %s", log_label, exc)
        raise HTTPException(status_code=502, detail=failure_detail) from exc
    except (KeyError, ValueError, RuntimeError) as exc:
        logger.warning("%s returned an invalid response", log_label)
        raise HTTPException(status_code=502, detail=failure_detail) from exc


async def jwt_identity(request: Request) -> JwtIdentity:
    """Resolve the caller of a private read that needs to know who they are.

    A bare X-SIWE-Token authorises a read but does not tell us whose it is,
    and no accounting endpoint resolves one to an address — so endpoints that
    key data by address take the JWT and reject the SIWE token outright
    rather than trusting an address supplied by the client.
    """
    if request.headers.get("x-siwe-token"):
        raise HTTPException(
            status_code=400,
            detail="Use Authorization bearer token; X-SIWE-Token is not accepted",
        )

    authorization = request.headers.get("authorization")
    if not authorization:
        raise auth_error()

    token = bearer_token(authorization)
    return await resolve_via_accounting(
        lambda: get_accounting_client().get_jwt_identity(token),
        failure_detail="Accounting token validation failed",
        log_label="Accounting JWT identity lookup",
    )
