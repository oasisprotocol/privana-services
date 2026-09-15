import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_account import Account

from src.core.db import _run_migrations
from src.core.eip712 import recover_transfer_signer, sign_transfer
from src.services.swap.executor import SwapExecutor
from src.services.swap.worker import SwapWorker


@pytest.fixture
def worker(settings, monkeypatch):
    import src.services.swap.internal_pipeline as module

    worker = SwapWorker()
    worker.settings = replace(settings, lifi_execution_enabled=True)
    worker._internal_pipeline.settings = worker.settings
    worker.sapphire = MagicMock()
    worker.sapphire.w3.eth.get_transaction_count.return_value = 10
    worker.sapphire.submit_contract_call.side_effect = ["0x" + f"{i:064x}" for i in range(1, 20)]
    worker.sapphire.wait_for_receipt.return_value = {"status": 1, "blockNumber": 100}
    worker.accounting = MagicMock()
    worker.accounting.get_transfer_nonce = AsyncMock(return_value=7)
    worker.accounting.get_lp_balance = AsyncMock(return_value=MagicMock(balance=str(10**25)))
    monkeypatch.setattr(module, "get_sapphire_client", lambda: worker.sapphire)
    monkeypatch.setattr(module, "get_accounting_client", lambda: worker.accounting)
    return worker


@pytest.fixture
def enqueue(settings, insert_quote):
    async def make(i=1, venue="internal", nonce=0, key=None, **overrides):
        key = key or "0x" + f"{i:064x}"
        user = Account.from_key(key).address.lower()
        insert_quote(f"q{i}", user_address=user, venue=venue, **overrides)
        sig = sign_transfer(
            private_key=key, chain_id=settings.accounting_chain_id,
            verifying_contract=settings.accounting_contract_address,
            to_address=settings.liquidity_provider_address,
            token_id="0x" + "aa" * 32, amount=1000000, nonce=nonce,
        )
        executor = SwapExecutor()
        executor.settings = replace(settings, lifi_execution_enabled=True)
        return await executor.schedule_swap(f"q{i}", nonce, sig)
    return make


def rows(db):
    return [dict(r) for r in db.execute("SELECT * FROM swaps ORDER BY rowid")]


async def test_five_submissions_precede_receipts_and_sixth_waits(worker, enqueue, test_db, settings):
    for i in range(1, 7):
        await enqueue(i)
    events = []
    original_send = worker.sapphire.submit_contract_call.side_effect

    def send(**kwargs):
        events.append("send")
        assert len([r for r in rows(test_db) if r["status"] == "executing"]) == 5
        return next(original_send)

    def receipt(tx_hash):
        events.append("receipt")
        assert events[:5] == ["send"] * 5
        assert sum(r["swap_tx_hash"] is not None for r in rows(test_db)) == 5
        return {"status": 1, "blockNumber": 100}

    worker.sapphire.submit_contract_call.side_effect = send
    worker.sapphire.wait_for_receipt.side_effect = receipt
    await worker.run_internal_once()
    assert [r["status"] for r in rows(test_db)] == ["completed"] * 5 + ["scheduled"]
    assert [r["output_nonce"] for r in rows(test_db)[:5]] == list(range(7, 12))
    assert [c.kwargs["args"][7] for c in worker.sapphire.simulate_contract_call.call_args_list] == [7] * 5
    for call in worker.sapphire.submit_contract_call.call_args_list:
        args = call.kwargs["args"]
        signer = recover_transfer_signer(
            chain_id=settings.accounting_chain_id,
            verifying_contract=settings.accounting_contract_address,
            to_address=args[0], token_id="0x" + args[5].hex(), amount=args[6],
            nonce=args[7], signature="0x" + args[8].hex(),
        )
        assert signer.lower() == settings.liquidity_provider_address.lower()
    assert all(r["to_amount_actual"] == r["to_amount_estimate"] for r in rows(test_db)[:5])
    worker.sapphire.submit_contract_call.side_effect = original_send
    worker.sapphire.wait_for_receipt.side_effect = None
    await worker.run_internal_once()
    assert rows(test_db)[-1]["status"] == "completed"


async def test_failed_preflight_does_not_leave_nonce_gap(worker, enqueue, test_db):
    await enqueue(1)
    await enqueue(2)
    worker.sapphire.simulate_contract_call.side_effect = [ValueError("Insufficient balance"), None]
    await worker.run_internal_once()
    assert [r["status"] for r in rows(test_db)] == ["failed", "completed"]
    assert worker.sapphire.submit_contract_call.call_args.kwargs["args"][7] == 7


async def test_liquidity_reserved_across_batch(worker, enqueue, test_db):
    await enqueue(1)
    await enqueue(2)
    worker.accounting.get_lp_balance.return_value.balance = "1000000"
    await worker.run_internal_once()
    assert [r["status"] for r in rows(test_db)] == ["completed", "failed"]
    assert "Insufficient liquidity" in rows(test_db)[1]["error"]


async def test_zero_fee_payout_is_not_discounted(worker, enqueue, test_db):
    await enqueue(to_amount_estimate="1000000")
    worker.accounting.get_lp_balance.return_value.balance = "999999"
    await worker.run_internal_once()
    assert rows(test_db)[0]["status"] == "failed"
    worker.sapphire.submit_contract_call.assert_not_called()


async def test_same_user_next_nonce_waits_for_next_batch(worker, enqueue, test_db):
    key = "0x" + "22" * 32
    await enqueue(1, key=key, nonce=0)
    await enqueue(2, key=key, nonce=1)
    await worker.run_internal_once()
    assert [r["status"] for r in rows(test_db)] == ["completed", "scheduled"]
    await worker.run_internal_once()
    assert rows(test_db)[1]["status"] == "completed"


async def test_quote_cleanup_does_not_destroy_scheduled_swap(worker, enqueue, test_db):
    await enqueue()
    test_db.execute("DELETE FROM quotes")
    await worker.run_internal_once()
    assert rows(test_db)[0]["status"] == "completed"


async def test_receipt_timeout_keeps_executing_and_prevents_next_batch(worker, enqueue, test_db):
    await enqueue()
    worker.sapphire.wait_for_receipt.side_effect = TimeoutError("RPC timeout")
    await worker.run_internal_once()
    await enqueue(2)
    await worker.run_internal_once()
    assert [r["status"] for r in rows(test_db)] == ["executing", "scheduled"]
    assert rows(test_db)[0]["swap_tx_hash"]
    assert worker.sapphire.submit_contract_call.call_count == 1
    worker.sapphire.wait_for_receipt.side_effect = None
    await worker.run_internal_once()
    assert [r["status"] for r in rows(test_db)] == ["completed", "scheduled"]
    await worker.run_internal_once()
    assert rows(test_db)[1]["status"] == "completed"


async def test_reverted_receipt_is_failed(worker, enqueue, test_db):
    await enqueue()
    worker.sapphire.wait_for_receipt.return_value = {"status": 0}
    await worker.run_internal_once()
    assert rows(test_db)[0]["status"] == "failed"
    assert rows(test_db)[0]["to_amount_actual"] is None


async def test_submission_error_stops_nonce_sequence_and_requeues_unsent(worker, enqueue, test_db):
    for i in range(1, 4):
        await enqueue(i)
    worker.sapphire.submit_contract_call.side_effect = ["0x123", TimeoutError("send timeout")]
    await worker.run_internal_once()
    assert [r["status"] for r in rows(test_db)] == ["completed", "executing", "scheduled"]
    assert "manual recovery" in rows(test_db)[1]["error"]
    assert worker.sapphire.submit_contract_call.call_count == 2


async def test_restart_recovers_unbroadcast_claim_and_does_not_resend_ambiguous(worker, enqueue, test_db):
    await enqueue(1)
    await enqueue(2)
    test_db.execute("UPDATE swaps SET status = 'executing'")
    test_db.execute("UPDATE swaps SET output_signature = '0xab' WHERE quote_id = 'q2'")
    await worker.run_internal_once()
    assert [r["status"] for r in rows(test_db)] == ["scheduled", "executing"]
    worker.sapphire.submit_contract_call.assert_not_called()


async def test_existing_pending_evm_transaction_delays_batch(worker, enqueue, test_db):
    await enqueue()
    worker.sapphire.w3.eth.get_transaction_count.side_effect = [11, 10]
    await worker.run_internal_once()
    assert rows(test_db)[0]["status"] == "scheduled"
    worker.sapphire.submit_contract_call.assert_not_called()


async def test_lifi_dispatch_uses_same_record_and_quote_id(worker, enqueue, test_db):
    record = await enqueue(venue="lifi")
    worker._pipeline = MagicMock()
    worker._pipeline.launch = AsyncMock()
    await worker.run_lifi_once()
    call = worker._pipeline.launch.call_args
    assert call.kwargs["swap_id"] == record.id
    assert call.args[2] == 0
    assert call.args[3] == rows(test_db)[0]["input_signature"]
    assert call.args[0]["id"] == "q1"
    assert rows(test_db)[0]["status"] == "executing"
    assert len(rows(test_db)) == 1
    await worker.run_lifi_once()
    worker._pipeline.launch.assert_awaited_once()


async def test_internal_worker_leaves_lifi_to_dispatcher(worker, enqueue, test_db):
    await enqueue(venue="lifi")
    await worker.run_internal_once()
    assert rows(test_db)[0]["status"] == "scheduled"
    worker.sapphire.submit_contract_call.assert_not_called()


async def test_lifi_dispatch_failure_is_recorded(worker, enqueue, test_db):
    await enqueue(venue="lifi")
    worker._pipeline = MagicMock()
    worker._pipeline.launch = AsyncMock(side_effect=RuntimeError("unavailable"))
    await worker.run_lifi_once()
    assert rows(test_db)[0]["status"] == "failed"


async def test_migration_renames_pending_but_preserves_queue(worker, enqueue, test_db):
    await enqueue(1)
    await enqueue(2)
    test_db.execute("UPDATE swaps SET status = 'pending' WHERE quote_id = 'q1'")
    _run_migrations(test_db)
    assert [r["status"] for r in rows(test_db)] == ["executing", "scheduled"]


async def test_stop_waits_for_current_iteration(worker):
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def iteration():
        entered.set()
        await finish.wait()

    worker.settings = replace(worker.settings, lifi_execution_enabled=False)
    worker._internal_pipeline.run_internal_once = iteration
    await worker.start()
    await entered.wait()
    stopping = asyncio.create_task(worker.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    finish.set()
    await stopping
    assert worker._tasks == []
