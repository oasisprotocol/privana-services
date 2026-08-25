from src.core.validation import MAX_REVERT_REASON_LENGTH, describe_error, sanitize_error


class _ApiError(Exception):
    """Shape of the accounting SDK's AccountingApiError: the useful text is on
    .detail, and Exception only ever sees the terse message."""

    def __init__(self, message: str, status_code: int, detail: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class TestDescribeError:
    def test_appends_the_detail_the_sdk_hides(self):
        exc = _ApiError(
            "API request failed: 400 Bad Request",
            400,
            "Insufficient native balance on Base Sepolia. EVM address 0xE5A9 has 0 wei.",
        )

        described = describe_error(exc)

        assert "400 Bad Request" in described
        assert "Insufficient native balance on Base Sepolia" in described

    def test_plain_exception_is_unchanged(self):
        assert describe_error(RuntimeError("boom")) == "boom"

    def test_detail_is_not_repeated_when_already_in_the_message(self):
        exc = _ApiError("failed: already said it", 400, "already said it")

        assert describe_error(exc) == "failed: already said it"

    def test_empty_detail_is_ignored(self):
        assert describe_error(_ApiError("bare", 500, None)) == "bare"

    def test_non_string_detail_is_ignored(self):
        """Unrelated libraries hang objects off .detail; only text is safe to
        fold into a message a caller may read."""
        exc = _ApiError("bare", 500, {"internal": "structure", "trace": [1, 2, 3]})

        assert describe_error(exc) == "bare"


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


class TestIncidentEndToEnd:
    def test_bridge_gas_failure_now_names_the_cause(self):
        """The deposit path did sanitize_error(str(exc)) and recorded only
        'API request failed: 400 Bad Request'. It should name the real cause."""
        exc = _ApiError(
            "API request failed: 400 Bad Request",
            400,
            "Insufficient native balance on Base Sepolia. EVM address "
            "0xE5A94d196DE8EeC7ABEc59aca32C322F3Dccc74A has 0 wei, "
            "needs at least 10000000000000 wei.",
        )

        recorded = sanitize_error(describe_error(exc))

        assert "Insufficient native balance on Base Sepolia" in recorded
        assert "0xE5A94d196DE8EeC7ABEc59aca32C322F3Dccc74A" in recorded
