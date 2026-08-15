import json
from pathlib import Path

from src.models.common import HistoryEntry
from src.services.portfolio.reconstruction import BucketPoint, replay_history

FIXTURES = Path(__file__).parent / "fixtures"

USDC = "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514"
WETH = "0x335b5cccd1e63b2fe79863a0db73fce430e4e66902e2b78424f8662621e29fb7"
USER = "0xd8991364507FAfC256EafF950d28618735753476"
SERVICE = "0x9147000000000000000000000000000000000001"


def _load(name):
    payload = json.loads((FIXTURES / name).read_text())
    return [HistoryEntry(**entry) for entry in payload["history"]]


class TestReplayStagingFixture:
    def test_incoming_transfers_accumulate_available(self):
        series = replay_history(_load("staging_transfer_history.json"), USER)

        assert list(series) == [USDC]
        assert series[USDC] == [
            BucketPoint(timestamp=1786364867, available=5000000, locked=0),
            BucketPoint(timestamp=1786429729, available=7000000, locked=0),
        ]


class TestReplayLifecycleFixture:
    def test_every_kind_routes_to_the_right_bucket(self):
        series = replay_history(_load("lifecycle_history.json"), USER)

        assert series[USDC] == [
            BucketPoint(timestamp=1786000000, available=10000000, locked=0),
            BucketPoint(timestamp=1786010000, available=6000000, locked=4000000),
            BucketPoint(timestamp=1786020000, available=5000000, locked=5000000),
            BucketPoint(timestamp=1786030000, available=5000000, locked=3000000),
            BucketPoint(timestamp=1786040000, available=8000000, locked=0),
            BucketPoint(timestamp=1786050000, available=7000000, locked=0),
            BucketPoint(timestamp=1786060000, available=5000000, locked=0),
        ]

    def test_tokens_are_reconstructed_independently(self):
        series = replay_history(_load("lifecycle_history.json"), USER)

        assert series[WETH] == [
            BucketPoint(timestamp=1786015000, available=700000000000000000, locked=0),
        ]

    def test_unknown_kind_is_skipped(self):
        series = replay_history(_load("lifecycle_history.json"), USER)

        total_points = sum(len(points) for points in series.values())
        assert total_points == 8


class TestCounterpartyDependentKinds:
    def test_withdraw_to_self_debits_available(self):
        entries = [
            HistoryEntry(kind="deposit", timestamp=100, token_id=USDC, amount="10"),
            HistoryEntry(
                kind="withdraw", timestamp=200, token_id=USDC, amount="4", counterparty=USER
            ),
        ]

        series = replay_history(entries, USER)

        assert series[USDC][-1] == BucketPoint(timestamp=200, available=6, locked=0)

    def test_withdraw_to_someone_else_is_a_lock_payout(self):
        entries = [
            HistoryEntry(kind="deposit", timestamp=100, token_id=USDC, amount="10"),
            HistoryEntry(kind="createLock", timestamp=150, token_id=USDC, amount="5"),
            HistoryEntry(
                kind="withdraw", timestamp=200, token_id=USDC, amount="4", counterparty=SERVICE
            ),
        ]

        series = replay_history(entries, USER)

        assert series[USDC][-1] == BucketPoint(timestamp=200, available=5, locked=1)

    def test_counterparty_match_is_case_insensitive(self):
        entries = [
            HistoryEntry(
                kind="withdraw",
                timestamp=100,
                token_id=USDC,
                amount="4",
                counterparty=USER.lower(),
            ),
        ]

        series = replay_history(entries, USER)

        assert series[USDC] == [BucketPoint(timestamp=100, available=-4, locked=0)]

    def test_transfer_from_lock_to_self_returns_funds_to_available(self):
        entries = [
            HistoryEntry(kind="deposit", timestamp=100, token_id=USDC, amount="10"),
            HistoryEntry(kind="createLock", timestamp=150, token_id=USDC, amount="5"),
            HistoryEntry(
                kind="transferFromLockOut",
                timestamp=200,
                token_id=USDC,
                amount="5",
                counterparty=USER,
            ),
        ]

        series = replay_history(entries, USER)

        assert series[USDC][-1] == BucketPoint(timestamp=200, available=10, locked=0)

    def test_transfer_from_lock_to_someone_else_consumes_the_lock(self):
        entries = [
            HistoryEntry(kind="deposit", timestamp=100, token_id=USDC, amount="10"),
            HistoryEntry(kind="createLock", timestamp=150, token_id=USDC, amount="5"),
            HistoryEntry(
                kind="transferFromLockOut",
                timestamp=200,
                token_id=USDC,
                amount="2",
                counterparty=SERVICE,
            ),
        ]

        series = replay_history(entries, USER)

        assert series[USDC][-1] == BucketPoint(timestamp=200, available=5, locked=3)


class TestReplayEdgeCases:
    def test_empty_history_yields_no_series(self):
        assert replay_history([], USER) == {}

    def test_out_of_order_entries_are_sorted_before_replay(self):
        entries = [
            HistoryEntry(
                kind="withdraw", timestamp=200, token_id=USDC, amount="3", counterparty=USER
            ),
            HistoryEntry(kind="deposit", timestamp=100, token_id=USDC, amount="5"),
        ]

        series = replay_history(entries, USER)

        assert series[USDC] == [
            BucketPoint(timestamp=100, available=5, locked=0),
            BucketPoint(timestamp=200, available=2, locked=0),
        ]

    def test_truncated_history_preserves_negative_balances(self):
        entries = [
            HistoryEntry(
                kind="withdraw", timestamp=100, token_id=USDC, amount="1000", counterparty=USER
            ),
        ]

        series = replay_history(entries, USER)

        assert series[USDC] == [BucketPoint(timestamp=100, available=-1000, locked=0)]

    def test_same_timestamp_events_collapse_to_final_state(self):
        entries = [
            HistoryEntry(kind="deposit", timestamp=100, token_id=USDC, amount="10"),
            HistoryEntry(kind="createLock", timestamp=100, token_id=USDC, amount="4"),
        ]

        series = replay_history(entries, USER)

        assert series[USDC] == [BucketPoint(timestamp=100, available=6, locked=4)]

    def test_entry_without_amount_is_skipped(self):
        entries = [
            HistoryEntry(kind="deposit", timestamp=100, token_id=USDC, amount=None),
            HistoryEntry(kind="deposit", timestamp=200, token_id=USDC, amount="7"),
        ]

        series = replay_history(entries, USER)

        assert series[USDC] == [BucketPoint(timestamp=200, available=7, locked=0)]
