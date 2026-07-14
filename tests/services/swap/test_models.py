from src.models.swap import LifiSwapStep, SwapStatus, SwapVenue


class TestSwapVenue:
    def test_values(self):
        assert SwapVenue.INTERNAL.value == "internal"
        assert SwapVenue.LIFI.value == "lifi"


class TestSwapStatus:
    def test_six_states(self):
        assert {s.value for s in SwapStatus} == {
            "pending", "executing", "completed", "failed", "refunding", "refunded",
        }


class TestLifiSwapStep:
    def test_step_order(self):
        assert [s.value for s in LifiSwapStep] == [
            "input_transfer", "withdraw", "lifi_execute", "deposit", "credit",
        ]


class TestSchema:
    def test_new_columns_exist(self, test_db):
        quote_cols = {r["name"] for r in test_db.execute("PRAGMA table_info(quotes)")}
        swap_cols = {r["name"] for r in test_db.execute("PRAGMA table_info(swaps)")}
        assert "venue" in quote_cols
        assert {"venue", "step", "withdrawal_index", "lifi_tx_hash", "deposit_tx_hash"} <= swap_cols
