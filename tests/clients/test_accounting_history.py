import json
from unittest.mock import AsyncMock

import httpx
import pytest

from src.clients.accounting import HISTORY_PAGE_LIMIT, AccountingClient
from src.models.common import HistoryEntry

USDC_TOKEN_ID = "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514"
COUNTERPARTY = "0x705b2433b76c383C20AE0d60803334f0AD13b6e8"


def _entry(kind, timestamp, amount="1000000", **overrides):
    entry = {
        "kind": kind,
        "timestamp": timestamp,
        "token_id": USDC_TOKEN_ID,
        "amount": amount,
        "counterparty": COUNTERPARTY,
        "deposit_id": None,
        "chain_id": 84532,
    }
    entry.update(overrides)
    return entry


def _response(entries, total, status_code=200):
    return httpx.Response(
        status_code=status_code,
        content=json.dumps({"history": entries, "total": total}),
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "https://accounting.test/v1/accounting/history"),
    )


def _client_with_pages(pages):
    client = AccountingClient.__new__(AccountingClient)
    client.base_url = "https://accounting.test"
    client.client = AsyncMock(spec=httpx.AsyncClient)
    client.client.request = AsyncMock(side_effect=pages)
    return client


class TestGetUserHistory:
    async def test_single_page_returns_all_entries(self):
        client = _client_with_pages([
            _response([_entry("deposit", 100), _entry("withdraw", 200)], total=2),
        ])

        history = await client.get_user_history("siwe-token")

        assert [e.kind for e in history] == ["deposit", "withdraw"]
        assert all(isinstance(e, HistoryEntry) for e in history)
        assert client.client.request.await_count == 1

    async def test_sends_siwe_token_header_and_pages_from_oldest(self):
        client = _client_with_pages([_response([_entry("deposit", 100)], total=1)])

        await client.get_user_history("siwe-token")

        call = client.client.request.await_args
        assert call.kwargs["headers"] == {"X-SIWE-Token": "siwe-token"}
        assert f"offset=0&limit={HISTORY_PAGE_LIMIT}" in call.args[1]

    async def test_walks_every_page_until_total(self):
        first = [_entry("deposit", ts) for ts in range(HISTORY_PAGE_LIMIT)]
        second = [_entry("withdraw", HISTORY_PAGE_LIMIT + 1)]
        client = _client_with_pages([
            _response(first, total=HISTORY_PAGE_LIMIT + 1),
            _response(second, total=HISTORY_PAGE_LIMIT + 1),
        ])

        history = await client.get_user_history("siwe-token")

        assert len(history) == HISTORY_PAGE_LIMIT + 1
        assert client.client.request.await_count == 2
        second_url = client.client.request.await_args_list[1].args[1]
        assert "offset=1" in second_url

    async def test_short_page_terminates_even_if_total_overstates(self):
        client = _client_with_pages([
            _response([_entry("deposit", 100)], total=50),
        ])

        history = await client.get_user_history("siwe-token")

        assert len(history) == 1
        assert client.client.request.await_count == 1

    async def test_result_is_sorted_by_timestamp(self):
        client = _client_with_pages([
            _response(
                [_entry("withdraw", 300), _entry("deposit", 100), _entry("deposit", 200)],
                total=3,
            ),
        ])

        history = await client.get_user_history("siwe-token")

        assert [e.timestamp for e in history] == [100, 200, 300]

    async def test_empty_history_returns_empty_list(self):
        client = _client_with_pages([_response([], total=0)])

        assert await client.get_user_history("siwe-token") == []

    async def test_auth_failure_propagates(self):
        client = _client_with_pages([_response([], total=0, status_code=401)])

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.get_user_history("bad-token")

        assert exc_info.value.response.status_code == 401

    async def test_optional_fields_survive_null(self):
        client = _client_with_pages([
            _response(
                [_entry("unknown", 100, amount=None, token_id=None, counterparty=None)],
                total=1,
            ),
        ])

        history = await client.get_user_history("siwe-token")

        assert history[0].kind == "unknown"
        assert history[0].amount is None
        assert history[0].token_id is None
