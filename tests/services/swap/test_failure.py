import time

import pytest

from src.core.db import db_write
from src.services.swap.failure import log_swap_failure, swap_failure


@pytest.mark.parametrize("venue,step", [
    ("lifi", "input_transfer"), ("lifi", "withdraw"), ("lifi", "lifi_execute"),
    ("lifi", "deposit"), ("lifi", "credit"),
])
def test_a_failed_swap_holding_the_users_funds_says_how_to_get_them_back(venue, step):
    reason, message = swap_failure("swap-123", "failed", venue, step)

    assert reason == "needs_support"
    assert "swap ID swap-123" in message


@pytest.mark.parametrize("venue,step", [("internal", None), ("lifi", None)])
def test_a_failed_swap_that_never_took_funds_says_so(venue, step):
    assert swap_failure("swap-123", "failed", venue, step)[0] == "no_funds_moved"


def test_a_refunded_swap_says_the_funds_came_back():
    reason, message = swap_failure("swap-123", "refunded", "lifi", "lifi_execute")

    assert reason == "refunded"
    assert "returned" in message


@pytest.mark.parametrize("status", ["scheduled", "executing", "refunding", "completed"])
def test_a_swap_still_running_or_done_has_nothing_to_explain(status):
    assert swap_failure("swap-123", status, "lifi", "withdraw") == (None, None)


def test_the_log_carries_the_provider_error_and_the_swap_state(test_db, caplog, monkeypatch):
    import src.services.swap.failure as module

    now = int(time.time())
    db_write(test_db, """INSERT INTO swaps (id, quote_id, user_address, from_token_id, to_token_id,
        from_amount, to_amount_estimate, status, venue, step, lifi_tx_hash, created_at, updated_at)
        VALUES ('s1', 'q1', '0xuser', '0xaa', '0xbb', '1000', '990', 'failed', 'lifi',
                'lifi_execute', '0xabc', ?, ?)""", (now, now))
    records = []
    monkeypatch.setattr(module.logger, "error", lambda msg, *args: records.append(msg % args))

    try:
        raise RuntimeError("route reverted at https://eth.example/v2/SECRETKEY?x=1: STF")
    except RuntimeError as exc:
        log_swap_failure("s1", "lifi swap failed", exc)

    line = records[0]
    assert "swap s1 lifi swap failed" in line
    # The provider's own words and the swap's state, for tracing.
    assert "route reverted" in line and "STF" in line
    assert '"step": "lifi_execute"' in line and '"lifi_tx_hash": "0xabc"' in line
    assert "Traceback" in line
    # A key in a provider URL never reaches the logs.
    assert "SECRETKEY" not in line and "https://eth.example/…" in line
