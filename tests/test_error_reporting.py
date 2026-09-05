from src.core.validation import MAX_REVERT_REASON_LENGTH, sanitize_error


class TestSanitizeError:
    def test_keeps_the_revert_reason(self):
        raw = "Transaction reverted: InsufficientBalance (module: evm, code: 8)"

        assert sanitize_error(raw) == (
            "Transaction reverted: InsufficientBalance (module: evm, code: 8)"
        )

    def test_keeps_a_contract_custom_error(self):
        assert (
            sanitize_error("execution reverted: InsufficientShares()")
            == "Transaction reverted: InsufficientShares()"
        )

    def test_revert_without_a_reason_stays_generic(self):
        assert sanitize_error("execution reverted") == "Transaction reverted on-chain"

    def test_rpc_url_is_stripped_from_the_reason(self):
        raw = "execution reverted: bad call via https://rpc.example/secret-key"

        sanitized = sanitize_error(raw)

        assert "rpc.example" not in sanitized
        assert "[url]" in sanitized

    def test_long_reason_is_capped(self):
        raw = "execution reverted: " + "x" * 400

        assert len(sanitize_error(raw)) <= len("Transaction reverted: ") + MAX_REVERT_REASON_LENGTH

    def test_insufficient_funds_still_collapses(self):
        assert (
            sanitize_error("sender doesn't have enough insufficient funds for gas")
            == "Insufficient gas funds for transaction"
        )

    def test_nonce_conflict_still_collapses(self):
        assert sanitize_error("nonce too low") == "Transaction nonce conflict"

    def test_unremarkable_error_passes_through(self):
        assert sanitize_error("something went wrong") == "something went wrong"

    def test_reverted_inside_another_word_is_not_a_revert(self):
        assert sanitize_error("unreverted state detected") == "unreverted state detected"

    def test_trailing_trace_payload_is_not_included(self):
        raw = "execution reverted: InsufficientShares()\nTrace: 0xdeadbeef internal=1"

        sanitized = sanitize_error(raw)

        assert sanitized == "Transaction reverted: InsufficientShares()"
        assert "Trace" not in sanitized

    def test_revert_mentioning_a_nonce_keeps_the_reason(self):
        """Both branches could claim this one. The revert reason is the more
        specific answer, so it wins."""
        assert (
            sanitize_error("execution reverted: InvalidNonce()")
            == "Transaction reverted: InvalidNonce()"
        )


class TestComposed:
    def test_api_error_detail_passes_through_sanitize(self):
        assert sanitize_error("API request failed: 400 Bad Request: pool is paused") == (
            "API request failed: 400 Bad Request: pool is paused"
        )
