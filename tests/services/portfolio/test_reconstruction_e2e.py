import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from src.clients.accounting import AccountingClient
from src.services.portfolio.reconstruction import BucketPoint, replay_history

FIXTURES = Path(__file__).parent / "fixtures"

USDC = "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514"


def _client_returning(payload):
    client = AccountingClient.__new__(AccountingClient)
    client.base_url = "https://accounting.test"
    client.client = AsyncMock(spec=httpx.AsyncClient)
    client.client.request = AsyncMock(
        return_value=httpx.Response(
            status_code=200,
            content=json.dumps(payload),
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", "https://accounting.test/v1/accounting/history"),
        )
    )
    return client


class TestHistoryToBucketsSeam:
    async def test_paged_history_replays_into_bucket_series(self):
        payload = json.loads((FIXTURES / "staging_transfer_history.json").read_text())
        client = _client_returning(payload)

        history = await client.get_user_history("siwe-token")
        series = replay_history(history)

        assert series[USDC] == [
            BucketPoint(timestamp=1786364867, available=5000000, locked=0),
            BucketPoint(timestamp=1786429729, available=7000000, locked=0),
        ]

    async def test_lifecycle_fixture_survives_the_full_pipeline(self):
        payload = json.loads((FIXTURES / "lifecycle_history.json").read_text())
        client = _client_returning(payload)

        history = await client.get_user_history("siwe-token")
        series = replay_history(history)

        assert series[USDC][-1] == BucketPoint(
            timestamp=1786060000, available=5000000, locked=0
        )
        assert len(history) == 10
        assert sum(len(points) for points in series.values()) == 8
