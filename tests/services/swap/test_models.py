from src.core.db import _run_migrations, db_write
from src.models.swap import SwapStatus, SwapVenue
from src.services.swap.failure import swap_failure


class TestSwapVenue:
    def test_values(self):
        assert SwapVenue.INTERNAL.value == "internal"
        assert SwapVenue.LIFI.value == "lifi"


class TestSwapStatus:
    def test_six_states(self):
        assert {s.value for s in SwapStatus} == {
            "scheduled", "executing", "completed", "failed", "refunding", "refunded",
        }


class TestStageUpgrade:
    ROWS = {
        # id: (venue, status, step, output_signature, error)
        "scheduled": ("lifi", "scheduled", None, None, None),
        "executing_lifi": ("lifi", "executing", "lifi_execute", None, None),
        "failed_holding": ("lifi", "failed", "deposit", None, None),
        "failed_lifi_unsent": ("lifi", "failed", None, None, "input transfer rejected: status=rejected detail=x"),
        "failed_internal": ("internal", "failed", None, None, "pricing unavailable"),
        "failed_reverted": ("internal", "failed", None, "0xsigned", "Transaction reverted: 0xabc"),
        "failed_parked": ("lifi", "failed", "parked", None, None),
        "completed": ("lifi", "completed", "credit", None, None),
    }

    def _insert(self, test_db, swap_id, venue, status, step, output_signature, error=None):
        db_write(test_db, """INSERT INTO swaps (id, quote_id, user_address, from_token_id, to_token_id,
            from_amount, to_amount_estimate, status, venue, step, output_signature, error, created_at, updated_at)
            VALUES (?, 'q', '0xuser', '0xaa', '0xbb', '1', '1', ?, ?, ?, ?, ?, 1, 1)""",
                 (swap_id, status, venue, step, output_signature, error))

    def _row(self, test_db, swap_id):
        return dict(test_db.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone())

    def test_failed_swaps_keep_what_they_told_the_user(self, test_db):
        for swap_id, row in self.ROWS.items():
            self._insert(test_db, swap_id, *row)
        db_write(test_db, "DELETE FROM data_migrations WHERE name = 'swap_stages_v1'")

        _run_migrations(test_db)

        rows = {swap_id: self._row(test_db, swap_id) for swap_id in self.ROWS}
        assert {swap_id: (row["status"], row["step"]) for swap_id, row in rows.items()} == {
            "scheduled": ("scheduled", None),
            "executing_lifi": ("executing", "lifi_execute"),
            "failed_holding": ("failed", "parked"),
            "failed_lifi_unsent": ("failed", "no_funds_moved"),
            "failed_internal": ("failed", "no_funds_moved"),
            "failed_reverted": ("failed", "no_funds_moved"),
            "failed_parked": ("failed", "parked"),
            "completed": ("completed", "credit"),
        }
        assert swap_failure("failed_holding", "failed", rows["failed_holding"]["step"])[0] == "needs_support"
        assert swap_failure("failed_internal", "failed", rows["failed_internal"]["step"])[0] == "no_funds_moved"
