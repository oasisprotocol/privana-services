import time
from unittest.mock import patch

import pytest
from eth_account import Account

from src.core.db import db_write
from src.core.eip712 import sign_transfer
from src.models.swap import SwapStatus

USER_SK = "0x" + "11" * 32
USER_ADDRESS = Account.from_key(USER_SK).address


def _make_executor(settings):
    with patch("src.services.swap.scheduler.load_settings", return_value=settings):
        from src.services.swap.scheduler import SwapScheduler
        return SwapScheduler()


def _quote_fields(**overrides):
    fields = dict(
        user_address=USER_ADDRESS.lower(),
        from_token_id="0xaaaa",
        to_token_id="0xbbbb",
        from_amount="1000000",
        to_amount_gross="45000000000000000",
        to_amount_estimate="44000000000000000",
        to_amount_min="43000000000000000",
        route_tool="okx",
    )
    fields.update(overrides)
    return fields


def _sign_input(settings):
    return sign_transfer(
        private_key=USER_SK,
        chain_id=settings.accounting_chain_id,
        verifying_contract=settings.accounting_contract_address,
        to_address=settings.liquidity_provider_address,
        token_id="0xaaaa",
        amount=1000000,
        nonce=0,
    )


class TestValidateQuote:
    def test_expired_quote_raises(self, test_db, settings, insert_quote):
        insert_quote("expired_q", expires_at=int(time.time()) - 10, user_address="0xuser")
        executor = _make_executor(settings)
        with pytest.raises(ValueError, match="Quote has expired"):
            executor._validate_quote("expired_q")

    def test_quote_at_exact_expiry_raises(self, test_db, settings, insert_quote):
        insert_quote("q_boundary", expires_at=int(time.time()))
        executor = _make_executor(settings)
        with pytest.raises(ValueError, match="Quote has expired"):
            executor._validate_quote("q_boundary")

    def test_missing_quote_raises(self, test_db, settings):
        executor = _make_executor(settings)
        with pytest.raises(ValueError, match="Quote not found"):
            executor._validate_quote("nonexistent")

    def test_valid_quote_returns_dict(self, test_db, settings, insert_quote):
        insert_quote("q_valid", **_quote_fields())
        executor = _make_executor(settings)
        result = executor._validate_quote("q_valid")
        assert isinstance(result, dict)
        assert result["id"] == "q_valid"


class TestScheduleSwap:
    @pytest.fixture
    def input_signature(self, settings):
        return _sign_input(settings)

    async def test_signer_mismatch_rejected(self, test_db, settings, insert_quote, input_signature):
        insert_quote("q_stranger", **_quote_fields(user_address="0x" + "9" * 40))
        executor = _make_executor(settings)
        with pytest.raises(ValueError, match="Quote was not created for this user"):
            await executor.schedule_swap("q_stranger", 0, input_signature)

    async def test_malformed_signature_rejected(self, test_db, settings, insert_quote):
        insert_quote("q_badsig", **_quote_fields())
        executor = _make_executor(settings)
        with pytest.raises(ValueError):
            await executor.schedule_swap("q_badsig", 0, "0xdeadbeef")

    async def test_queues_the_swap_as_a_scheduled_row(
        self, test_db, settings, insert_quote, input_signature
    ):
        insert_quote("q_sched", **_quote_fields())
        executor = _make_executor(settings)

        result = await executor.schedule_swap("q_sched", 0, input_signature)

        assert result.status == SwapStatus.SCHEDULED.value
        assert result.venue == "internal"
        assert result.swap_tx_hash is None
        assert result.input_nonce == 0
        assert result.input_signature == input_signature
        row = test_db.execute(
            "SELECT status, venue FROM swaps WHERE id = ?", (result.id,)
        ).fetchone()
        assert row["status"] == SwapStatus.SCHEDULED.value
        assert row["venue"] == "internal"

    async def test_rescheduling_the_same_quote_is_rejected(
        self, test_db, settings, insert_quote, input_signature
    ):
        # One quote is consumed by exactly one swap attempt, ever — a second
        # POST for the same quote (a client retry, say) must not create a
        # second row that races the first for the same input. Signed against
        # a different (inactive) nonce so this isolates the quote_id check
        # from the separate active-nonce check exercised above.
        insert_quote("q_once", **_quote_fields())
        executor = _make_executor(settings)
        other_signature = sign_transfer(
            private_key=USER_SK,
            chain_id=settings.accounting_chain_id,
            verifying_contract=settings.accounting_contract_address,
            to_address=settings.liquidity_provider_address,
            token_id="0xaaaa",
            amount=1000000,
            nonce=1,
        )

        await executor.schedule_swap("q_once", 0, input_signature)
        with pytest.raises(ValueError, match="already been scheduled"):
            await executor.schedule_swap("q_once", 1, other_signature)

        rows = test_db.execute(
            "SELECT COUNT(*) AS cnt FROM swaps WHERE quote_id = 'q_once'"
        ).fetchone()
        assert rows["cnt"] == 1

    async def test_second_quote_with_the_same_active_nonce_is_rejected(
        self, test_db, settings, insert_quote, input_signature
    ):
        # Two different quotes signed with the same not-yet-consumed nonce -
        # e.g. a client that fetched the nonce once and reused it - must not
        # both be schedulable, or LifiSwap._submit_input's "a 409/422 means
        # this swap is resuming itself" assumption would no longer hold.
        insert_quote("q_first", **_quote_fields())
        insert_quote("q_second", **_quote_fields())
        executor = _make_executor(settings)

        await executor.schedule_swap("q_first", 0, input_signature)
        with pytest.raises(ValueError, match="already in use by another active swap"):
            await executor.schedule_swap("q_second", 0, input_signature)

    async def test_nonce_can_be_reused_once_the_first_swap_has_settled(
        self, test_db, settings, insert_quote, input_signature
    ):
        # A failed (or completed/refunded) swap frees its nonce: the user
        # must be able to retry against a fresh quote with the same
        # already-signed nonce.
        insert_quote("q_first", **_quote_fields())
        insert_quote("q_second", **_quote_fields())
        executor = _make_executor(settings)

        first = await executor.schedule_swap("q_first", 0, input_signature)
        db_write(
            test_db, "UPDATE swaps SET status = ? WHERE id = ?",
            (SwapStatus.FAILED.value, first.id),
        )

        result = await executor.schedule_swap("q_second", 0, input_signature)

        assert result.status == SwapStatus.SCHEDULED.value

    async def test_lifi_quote_is_queued_on_the_lifi_venue(
        self, test_db, settings, insert_quote, input_signature
    ):
        insert_quote("q_lifi", **_quote_fields(venue="lifi"))
        executor = _make_executor(settings)

        result = await executor.schedule_swap("q_lifi", 0, input_signature)

        assert result.status == SwapStatus.SCHEDULED.value
        assert result.venue == "lifi"
