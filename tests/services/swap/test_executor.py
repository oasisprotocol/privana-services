import time
from dataclasses import replace
from unittest.mock import patch

import pytest
from eth_account import Account

from src.core.eip712 import sign_transfer
from src.services.swap.executor import SwapExecutor

KEY = "0x" + "22" * 32
USER = Account.from_key(KEY).address.lower()
TOKEN = "0x" + "aa" * 32


@pytest.fixture
def request_data(settings, insert_quote):
    def make(quote_id="q1", nonce=0, **overrides):
        insert_quote(quote_id, user_address=USER, **overrides)
        signature = sign_transfer(
            private_key=KEY, chain_id=settings.accounting_chain_id,
            verifying_contract=settings.accounting_contract_address,
            to_address=settings.liquidity_provider_address,
            token_id=TOKEN, amount=1000000, nonce=nonce,
        )
        return quote_id, nonce, signature
    return make


@pytest.fixture
def executor(settings):
    service = SwapExecutor()
    service.settings = replace(settings, lifi_execution_enabled=True)
    return service


@pytest.mark.parametrize("venue", ["internal", "lifi"])
async def test_schedule_persists_authorization_without_chain_calls(
    test_db, executor, request_data, venue
):
    args = request_data(venue=venue)
    with patch("src.clients.sapphire.get_sapphire_client", side_effect=AssertionError("RPC")), \
         patch("src.clients.accounting.get_accounting_client", side_effect=AssertionError("RPC")), \
         patch("src.services.swap.lifi_pipeline.get_lifi_pipeline", side_effect=AssertionError("pipeline")):
        result = await executor.schedule_swap(*args)
    row = dict(test_db.execute("SELECT * FROM swaps").fetchone())
    assert result.status == "scheduled"
    assert result.venue == venue
    assert result.swap_tx_hash is None
    assert row["input_nonce"] == "0"
    assert row["input_signature"] == args[2].lower()
    assert row["quote_id"] == args[0]


async def test_retry_is_idempotent_even_after_quote_deleted(test_db, executor, request_data):
    args = request_data()
    first = await executor.schedule_swap(*args)
    test_db.execute("DELETE FROM quotes")
    second = await executor.schedule_swap(*args)
    assert second.id == first.id
    assert test_db.execute("SELECT count(*) FROM swaps").fetchone()[0] == 1


async def test_same_nonce_can_be_recorded_for_separate_attempts(executor, request_data):
    first = await executor.schedule_swap(*request_data("q1"))
    second = await executor.schedule_swap(*request_data("q2"))
    assert first.id != second.id


async def test_expired_quote_is_rejected(executor, request_data):
    with pytest.raises(ValueError, match="expired"):
        await executor.schedule_swap(*request_data(expires_at=int(time.time()) - 1))


async def test_missing_quote_is_rejected(executor, request_data):
    _, nonce, signature = request_data()
    with pytest.raises(ValueError, match="not found"):
        await executor.schedule_swap("missing", nonce, signature)


async def test_wrong_signer_is_rejected(test_db, executor, request_data):
    args = request_data()
    test_db.execute("UPDATE quotes SET user_address = ?", ("0x" + "33" * 20,))
    with pytest.raises(ValueError, match="not created for this user"):
        await executor.schedule_swap(*args)
    assert test_db.execute("SELECT count(*) FROM swaps").fetchone()[0] == 0


async def test_changed_transfer_is_rejected(test_db, executor, request_data):
    args = request_data()
    test_db.execute("UPDATE quotes SET from_amount = '2'")
    with pytest.raises(ValueError):
        await executor.schedule_swap(*args)


@pytest.mark.parametrize("nonce", [-1, 2**256])
async def test_invalid_nonce_is_rejected(executor, request_data, nonce):
    quote, _, sig = request_data()
    with pytest.raises(ValueError, match="nonce"):
        await executor.schedule_swap(quote, nonce, sig)


async def test_uint256_nonce_is_not_truncated(test_db, executor, request_data):
    nonce = 2**255
    await executor.schedule_swap(*request_data(nonce=nonce))
    assert test_db.execute("SELECT input_nonce FROM swaps").fetchone()[0] == str(nonce)


async def test_disabled_lifi_is_rejected(executor, request_data, settings):
    executor.settings = replace(settings, lifi_execution_enabled=False)
    with pytest.raises(ValueError, match="disabled"):
        await executor.schedule_swap(*request_data(venue="lifi"))


async def test_invalid_signature_is_rejected(executor, request_data):
    quote, nonce, _ = request_data()
    with pytest.raises(ValueError):
        await executor.schedule_swap(quote, nonce, "not a signature")
