from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from web3 import Web3

from src.clients.accounting import JwtIdentity
from src.core.db import db_write

USER_ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
USER_CHECKSUM = Web3.to_checksum_address(USER_ADDRESS)
OTHER_USER_ADDRESS = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
TOKEN_A = "0x" + "11" * 32
TOKEN_B = "0x" + "22" * 32
POOL_ID = "0x" + "33" * 32


def _insert_swap(
    db,
    swap_id: str,
    *,
    user_address: str = USER_ADDRESS,
    status: str = "pending",
    created_at: int = 100,
    updated_at: int = 100,
):
    db_write(
        db,
        """INSERT INTO swaps
           (id, quote_id, user_address, from_token_id, to_token_id,
            from_amount, to_amount_estimate, to_amount_actual, status,
            swap_tx_hash, error, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            swap_id,
            f"quote-{swap_id}",
            user_address.lower(),
            TOKEN_A,
            TOKEN_B,
            "1000",
            "990",
            None,
            status,
            None,
            "swap failed" if status == "failed" else None,
            created_at,
            updated_at,
        ),
    )


def _insert_earn(
    db,
    tx_id: str,
    *,
    operation: str = "deposit",
    user_address: str = USER_ADDRESS,
    status: str = "pending",
    created_at: int = 100,
    updated_at: int = 100,
):
    db_write(
        db,
        """INSERT INTO earn_transactions
           (id, operation, pool_id, user_address, token_id, amount,
            signer_address, nonce, signature, status, tx_hash, error,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            tx_id,
            operation,
            POOL_ID,
            user_address.lower(),
            TOKEN_A,
            "500",
            user_address.lower(),
            7,
            "0x" + "aa" * 65,
            status,
            None,
            "earn failed" if status == "failed" else None,
            created_at,
            updated_at,
        ),
    )


def _auth_client(address: str = USER_CHECKSUM) -> MagicMock:
    acct = MagicMock()
    acct.get_jwt_identity = AsyncMock(
        return_value=JwtIdentity(siwe_token="0x" + "ee" * 32, address=address)
    )
    return acct


class TestUnsettledOperationsRoute:
    async def test_returns_user_unsettled_swap_and_earn_operations(self, api_client, test_db):
        _insert_swap(test_db, "swap-pending", status="pending", updated_at=200)
        _insert_swap(test_db, "swap-completed", status="completed", updated_at=500)
        _insert_swap(
            test_db,
            "swap-other-user",
            user_address=OTHER_USER_ADDRESS,
            status="failed",
            updated_at=600,
        )
        _insert_earn(test_db, "earn-failed", operation="withdraw", status="failed", updated_at=400)
        _insert_earn(test_db, "earn-canceled", status="canceled", updated_at=300)
        _insert_earn(test_db, "earn-pending", status="pending", updated_at=100)

        acct = _auth_client()
        with patch("src.api._auth.get_accounting_client", return_value=acct):
            r = await api_client.get(
                "/v1/operations/unsettled",
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.status_code == 200
        acct.get_jwt_identity.assert_awaited_once_with("user-jwt")
        operations = r.json()["operations"]
        assert [op["operation_id"] for op in operations] == [
            "earn-failed",
            "earn-canceled",
            "swap-pending",
            "earn-pending",
        ]

        earn = operations[0]
        assert earn["operation_type"] == "earn_withdraw"
        assert earn["status"] == "failed"
        assert earn["pool_id"] == POOL_ID
        assert earn["token_id"] == TOKEN_A
        assert earn["amount"] == "500"
        assert earn["from_token_id"] is None

        swap = operations[2]
        assert swap["operation_type"] == "swap"
        assert swap["quote_id"] == "quote-swap-pending"
        assert swap["from_token_id"] == TOKEN_A
        assert swap["to_token_id"] == TOKEN_B
        assert swap["from_amount"] == "1000"
        assert swap["to_amount_estimate"] == "990"
        assert swap["pool_id"] is None

    async def test_applies_limit_after_ordering(self, api_client, test_db):
        _insert_swap(test_db, "older", updated_at=100)
        _insert_earn(test_db, "newer", updated_at=200)

        with patch("src.api._auth.get_accounting_client", return_value=_auth_client()):
            r = await api_client.get(
                "/v1/operations/unsettled",
                params={"limit": 1},
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.status_code == 200
        assert [op["operation_id"] for op in r.json()["operations"]] == ["newer"]

    async def test_returns_empty_list_when_user_has_no_unsettled_operations(self, api_client):
        acct = _auth_client()
        with patch("src.api._auth.get_accounting_client", return_value=acct):
            r = await api_client.get(
                "/v1/operations/unsettled",
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.status_code == 200
        assert r.json() == {"operations": []}
        acct.get_jwt_identity.assert_awaited_once_with("user-jwt")

    async def test_rejects_missing_auth(self, api_client):
        r = await api_client.get("/v1/operations/unsettled")

        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"

    async def test_rejects_siwe_auth(self, api_client):
        r = await api_client.get(
            "/v1/operations/unsettled",
            headers={"X-SIWE-Token": "0x" + "ee" * 32},
        )

        assert r.status_code == 400
        assert r.json()["detail"] == "Use Authorization bearer token; X-SIWE-Token is not accepted"

    async def test_maps_invalid_bearer_to_401(self, api_client):
        request = httpx.Request("POST", "http://accounting/v1/accounting/auth/jwt/siwe-token")
        response = httpx.Response(401, request=request)
        acct = MagicMock()
        acct.get_jwt_identity = AsyncMock(
            side_effect=httpx.HTTPStatusError("invalid", request=request, response=response)
        )

        with patch("src.api._auth.get_accounting_client", return_value=acct):
            r = await api_client.get(
                "/v1/operations/unsettled",
                headers={"Authorization": "Bearer bad-jwt"},
            )

        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"

    async def test_maps_accounting_failure_to_502(self, api_client):
        request = httpx.Request("POST", "http://accounting/v1/accounting/auth/jwt/siwe-token")
        response = httpx.Response(500, request=request)
        acct = MagicMock()
        acct.get_jwt_identity = AsyncMock(
            side_effect=httpx.HTTPStatusError("failed", request=request, response=response)
        )

        with patch("src.api._auth.get_accounting_client", return_value=acct):
            r = await api_client.get(
                "/v1/operations/unsettled",
                headers={"Authorization": "Bearer user-jwt"},
            )

        assert r.status_code == 502
        assert r.json()["detail"] == "Accounting token validation failed"
