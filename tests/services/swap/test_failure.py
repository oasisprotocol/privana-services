import pytest

from src.services.swap.failure import swap_failure


@pytest.mark.parametrize("status,error,reason", [
    # The three errors quoted in #183, as users saw them.
    ("refunded", "execution quote below floor: net_min=7690063 floor=7799419", "price_moved"),
    ("failed", "Network request failed: Server disconnected without sending a response.; "
               "refund failed, manual recovery required: Network request failed", "under_review"),
    ("failed", "transaction 0x04244d2a may have been sent, outcome unknown; "
               "manual recovery required", "under_review"),
    ("failed", "Transaction reverted: 0x" + "ab" * 32, "no_funds_moved"),
    ("failed", "input transfer rejected: status=error detail=bad sig", "no_funds_moved"),
    ("failed", "input transfer not confirmed on ledger", "under_review"),
    ("scheduled", "Submission outcome unknown; retrying: Transaction reverted", "delayed"),
    ("executing", "Submission outcome unknown; manual recovery required", "under_review"),
    ("refunded", "lifi route of 0xab ended FAILED", "no_funds_moved"),
    ("refunded", None, "no_funds_moved"),
])
def test_every_failure_gets_a_reason_users_can_read(status, error, reason):
    code, message = swap_failure(status, error)

    assert code == reason
    # Nothing internal reaches the user: no hashes, amounts or provider text.
    assert "0x" not in message and "floor" not in message and "Network" not in message


def test_a_refund_says_the_funds_came_back():
    assert swap_failure("refunded", "execution quote below floor: net_min=1 floor=2")[1] == (
        "The swap did not go through, and your funds were returned."
    )


@pytest.mark.parametrize("status", ["completed", "executing", "scheduled"])
def test_a_swap_without_an_error_has_nothing_to_explain(status):
    assert swap_failure(status, None) == (None, None)
