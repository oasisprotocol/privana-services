import json
import threading
import time

import pytest
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound, Web3RPCError

import src.clients.evm_batcher as module
from src.clients.base_evm import EvmClient
from src.clients.evm_batcher import EvmBatcher, TxIntent, find_evm_tx, submit
from src.core.config import load_settings
from src.core.db import get_db
from tests.clients.fake_chain import (
    CHAIN_ID,
    SAPPHIRE_CHAIN_ID,
    FakeChain,
    Unreachable,
    decode,
    hash_of,
    nonce_of,
    sapphire_sender,
)

TARGET = "0x" + "dd" * 20
LP = load_settings().liquidity_provider_address


def result(tx_id: int) -> dict:
    return dict(get_db().execute("SELECT * FROM evm_transactions WHERE id = ?", (tx_id,)).fetchone())


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(module, "RECEIPT_POLL_SEC", 0)
    monkeypatch.setattr(module, "RECEIPT_TIMEOUT_SEC", 0.05)


@pytest.fixture
def chain():
    return FakeChain()


@pytest.fixture
def batcher(chain, test_db, settings):
    # The LP account, which these tests queue for.
    evm = EvmClient("http://localhost:1", settings.liquidity_provider_secret_key)
    evm.w3.eth = chain
    return EvmBatcher(evm, test_db, threading.Event())


def intent(owner="swap:a:lifi", **fields):
    return TxIntent(owner=owner, to=TARGET, gas=21_000, **fields)


def queue(batcher, count, **fields):
    return [submit(CHAIN_ID, LP, intent(owner=f"swap:{i}:lifi", **fields))[0] for i in range(count)]


def test_submitting_an_owner_again_keeps_its_one_tx(batcher, test_db):
    (first,) = submit(CHAIN_ID, LP, intent(owner="swap:a:lifi"))
    (again,) = submit(CHAIN_ID, LP, intent(owner="swap:a:lifi", data="0xother"))

    assert again == first
    assert result(first)["data"] == "0x"
    assert test_db.execute("SELECT COUNT(*) FROM evm_transactions").fetchone()[0] == 1


def test_txs_submitted_together_get_consecutive_ids(batcher):
    approve, swap = submit(CHAIN_ID, LP, intent(owner="swap:a:approve"), intent(owner="swap:a:lifi"))

    assert swap == approve + 1


def test_a_tx_is_found_by_its_owner(batcher):
    (tx_id,) = submit(CHAIN_ID, LP, intent(owner="swap:a:deposit"))

    assert find_evm_tx("swap:a:deposit")["id"] == tx_id
    assert find_evm_tx("swap:b:deposit") is None


def test_submit_only_queues_the_tx(batcher, chain):
    (tx_id,) = submit(CHAIN_ID, LP, TxIntent(owner="swap:a:deposit", to=TARGET, gas=65_000, data="0xabcd", value=5))

    row = result(tx_id)
    assert (row["status"], row["owner"], row["data"], row["value"], row["gas"]) == (
        "queued", "swap:a:deposit", "0xabcd", "5", 65_000,
    )
    assert chain.sent == []


def test_a_round_sends_the_batch_back_to_back_and_waits_only_for_the_last(batcher, chain):
    ids = queue(batcher, 5)
    asked = []
    real = chain.get_transaction_receipt

    def receipt(tx_hash):
        asked.append(tx_hash)
        return real(tx_hash)

    chain.get_transaction_receipt = receipt
    chain.auto_mine = True

    batcher.run_round()

    # All five went out before any receipt was asked for, and the first asked is the last's.
    assert [nonce_of(raw) for raw in chain.sent] == [0, 1, 2, 3, 4]
    assert asked[0] == hash_of(chain.sent[-1])
    assert [result(i)["status"] for i in ids] == ["mined"] * 5


def test_each_tx_gets_its_receipt_status_and_logs(batcher, chain):
    ok, bad = queue(batcher, 2)
    batcher.run_round()
    chain.mine()
    chain.mine(status=0)

    batcher.run_round()

    assert result(ok)["status"] == "mined"
    assert result(bad)["status"] == "reverted"
    assert json.loads(result(ok)["logs"]) == []
    assert result(ok)["tx_hash"] == hash_of(chain.sent[0])


def test_every_attempt_is_saved_before_it_reaches_the_node(batcher, chain):
    (tx_id,) = queue(batcher, 1)
    seen = []

    def saved_first(raw):
        seen.append(f"{nonce_of(raw)}:{hash_of(raw)}" in result(tx_id)["attempts"] and not batcher.db.in_transaction)

    chain.on_send = saved_first

    batcher.run_round()

    assert seen == [True]


def test_a_tx_mined_before_a_restart_is_never_sent_again(batcher, chain):
    (tx_id,) = queue(batcher, 1)
    batcher.run_round()
    # Mined after the round gave up waiting, and the service restarted.
    chain.mine()
    sent = list(chain.sent)

    EvmBatcher(batcher.evm, batcher.db, threading.Event()).run_round()

    assert chain.sent == sent
    assert result(tx_id)["status"] == "mined"


def test_a_partly_mined_batch_resends_only_the_stuck_tx_at_its_nonce(batcher, chain, test_db):
    first, stuck = queue(batcher, 2)
    batcher.run_round()
    chain.mine()
    chain.pool.clear()
    age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)

    batcher.run_round()

    assert result(first)["status"] == "mined"
    assert [nonce_of(raw) for raw in chain.sent] == [0, 1, 1]
    assert len(result(stuck)["attempts"].split()) == 2


def test_a_used_nonce_without_a_receipt_yet_holds_the_round(batcher, chain):
    (tx_id,) = queue(batcher, 1)
    batcher.run_round()
    # The node counts the nonce as used before it serves the receipt.
    chain.pool.clear()
    chain.mined_count = 1
    sent = list(chain.sent)

    batcher.run_round()

    assert chain.sent == sent
    assert result(tx_id)["status"] == "queued"


def test_a_tx_whose_simulation_reverts_fails_without_using_a_nonce(batcher, chain):
    (doomed,) = submit(CHAIN_ID, LP, intent(owner="swap:a:lifi", data="0xdead"))
    (fine,) = submit(CHAIN_ID, LP, intent(owner="swap:b:lifi"))
    chain.reverting_calls.add("0xdead")

    batcher.run_round()

    assert result(doomed)["status"] == "failed"
    assert "simulation failed" in result(doomed)["error"] and "execution reverted" in result(doomed)["error"]
    assert result(fine)["attempts"].startswith("0:")


def test_a_dependent_tx_skips_simulation(batcher, chain):
    (tx_id,) = submit(CHAIN_ID, LP, intent(data="0xdead", simulate=False))
    chain.reverting_calls.add("0xdead")

    batcher.run_round()

    assert result(tx_id)["attempts"]


def test_a_nonce_error_stops_the_batch_for_the_next_round(batcher, chain):
    first, second = queue(batcher, 2)
    chain.refuse = [Web3RPCError("nonce too low")]

    batcher.run_round()

    assert chain.sent == []
    assert result(first)["status"] == "queued"
    assert result(second)["attempts"] is None


def test_a_tx_the_node_refuses_fails_and_frees_its_nonce(batcher, chain):
    refused, fine = queue(batcher, 2)
    chain.refuse = [Web3RPCError("insufficient funds for gas * price + value")]

    batcher.run_round()

    assert result(refused)["status"] == "failed"
    assert "insufficient funds" in result(refused)["error"]
    assert [nonce_of(raw) for raw in chain.sent] == [0]
    assert result(fine)["attempts"].startswith("0:")


def test_a_refused_retry_stays_queued_as_its_first_attempt_may_still_land(batcher, chain, test_db):
    (tx_id,) = queue(batcher, 1)
    batcher.run_round()
    chain.pool.clear()
    age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)
    chain.refuse = [Web3RPCError("insufficient funds for gas * price + value")]

    batcher.run_round()

    assert chain.refuse == []
    assert result(tx_id)["status"] == "queued"


def test_a_send_that_may_have_reached_the_node_stays_queued(batcher, chain):
    (tx_id,) = queue(batcher, 1)
    chain.refuse = [ConnectionError("response lost")]

    batcher.run_round()

    assert result(tx_id)["status"] == "queued"
    assert result(tx_id)["attempts"].startswith("0:")


def test_a_round_takes_at_most_the_batch_size(batcher, chain):
    batcher.batch_size = 3
    queue(batcher, 5)

    batcher.run_round()

    assert len(chain.sent) == 3


def test_a_sapphire_sender_sends_one_tx_per_round(test_db, settings):
    chain = FakeChain(SAPPHIRE_CHAIN_ID)
    chain.auto_mine = True
    sender = sapphire_sender(chain, test_db, settings.liquidity_provider_secret_key)
    submit(SAPPHIRE_CHAIN_ID, sender.evm.address, intent(owner="swap:a:lifi"), intent(owner="swap:b:lifi"))

    sender.run_round()

    assert len(chain.sent) == 1


def test_an_empty_queue_asks_the_node_nothing(batcher, chain, monkeypatch):
    monkeypatch.setattr(chain, "get_transaction_count", lambda *a: (_ for _ in ()).throw(AssertionError))

    assert batcher.run_round() is False


def test_an_rpc_failure_mid_batch_still_settles_what_was_sent(batcher, chain):
    (sent_first,) = submit(CHAIN_ID, LP, intent(owner="swap:a:lifi"))
    (unreachable,) = submit(CHAIN_ID, LP, intent(owner="swap:b:lifi", data="0xdown"))
    chain.auto_mine = True

    batcher.run_round()

    assert result(sent_first)["status"] == "mined"
    assert result(unreachable)["status"] == "queued"
    assert result(unreachable)["attempts"] is None


def test_an_rpc_failure_before_sending_leaves_the_queue_for_the_next_round(batcher, chain, monkeypatch):
    (tx_id,) = queue(batcher, 1)
    monkeypatch.setattr(chain, "get_transaction_count", lambda *a: (_ for _ in ()).throw(Unreachable("rpc down")))

    with pytest.raises(Unreachable):
        batcher.run_round()

    assert result(tx_id)["status"] == "queued"
    assert chain.sent == []


def test_a_lost_receipt_lookup_resends_nothing_once_the_rpc_is_back(batcher, chain, monkeypatch):
    (tx_id,) = queue(batcher, 1)
    real = chain.get_transaction_receipt
    monkeypatch.setattr(chain, "get_transaction_receipt", lambda h: (_ for _ in ()).throw(Unreachable("rpc down")))
    with pytest.raises(Unreachable):
        batcher.run_round()
    chain.mine()
    monkeypatch.setattr(chain, "get_transaction_receipt", real)
    sent = list(chain.sent)

    batcher.run_round()

    assert chain.sent == sent
    assert result(tx_id)["status"] == "mined"


def test_the_loop_drains_the_queue_until_stopped(batcher, chain):
    stop = batcher.stop
    chain.auto_mine = True
    (tx_id,) = queue(batcher, 1)
    real = chain.get_transaction_receipt

    def stop_once_mined(tx_hash):
        receipt = real(tx_hash)
        stop.set()
        return receipt

    chain.get_transaction_receipt = stop_once_mined

    batcher.run()

    assert result(tx_id)["status"] == "mined"


def test_one_batcher_thread_starts_per_chain_and_account(monkeypatch, settings):
    from dataclasses import replace

    served = []

    class Chain:
        def __init__(self, chain_id):
            self.chain_id = chain_id

    class Batcher:
        def __init__(self, evm, db, stop, before_round=None):
            self.evm = evm
            self.chain_id = evm.chain_id
            self.stop = stop

        def run(self):
            served.append((self.chain_id, self.evm.address, self.evm._headers))
            self.stop.wait()

    chains = {"http://base": 8453, "http://eth": 1, "http://base-again": 8453, "http://sapphire": 23295}

    def client(rpc_url, key, headers):
        evm = EvmClient("http://localhost:1", key, headers)
        evm.w3.eth = Chain(chains[rpc_url])
        return evm

    auth = {"Authorization": "Bearer t"}
    monkeypatch.setattr(module, "load_settings", lambda: replace(
        settings, base_rpc_url="http://base", ethereum_rpc_url="http://eth", hyperevm_rpc_url="http://base-again",
        sapphire_rpc_url="http://sapphire", sapphire_rpc_headers=auth))
    monkeypatch.setattr(module, "EvmClient", client)
    monkeypatch.setattr(module, "EvmBatcher", Batcher)
    monkeypatch.setattr(module, "connect", lambda: __import__("sqlite3").connect(":memory:"))

    module.start_evm_batchers(lambda db, evm: False)
    try:
        # The LP's thread for the second RPC of chain 8453 ends once it finds it served.
        deadline = time.monotonic() + 5
        while (len(served) < 3 or sum(t.is_alive() for t in module._threads) > 3) and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        module.stop_evm_batchers()

    # A second RPC for chain 8453 would be a second LP sender for its nonces.
    assert sorted(served) == sorted([(8453, LP, {}), (1, LP, {}), (23295, LP, auth)])


def refuse_used_nonces(chain):
    real = chain.send_raw_transaction

    def send(raw):
        if nonce_of(raw) < chain.mined_count:
            raise Web3RPCError("nonce too low")
        return real(raw)

    chain.send_raw_transaction = send


def mine_during_receipt_lookup(chain):
    real = chain.get_transaction_receipt

    def receipt(tx_hash):
        if chain.mined_count in chain.pool:
            chain.mine()
        return real(tx_hash)

    chain.get_transaction_receipt = receipt
    return real


def age(test_db, seconds):
    test_db.execute("UPDATE evm_transactions SET updated_at = updated_at - ?", (seconds,))


def executed(chain, tx_id):
    """The row's distinct attempts the chain mined."""
    return [h for h in {a.split(":")[1] for a in (result(tx_id)["attempts"] or "").split()} if h in chain.receipts]


def test_a_tx_mined_while_the_round_reconciles_does_not_stall_the_queue(batcher, chain):
    (x,) = queue(batcher, 1)
    batcher.run_round()
    (y,) = submit(CHAIN_ID, LP, intent(owner="swap:y:lifi", data="0xbeef"))
    real = mine_during_receipt_lookup(chain)
    refuse_used_nonces(chain)

    batcher.run_round()
    chain.get_transaction_receipt = real
    chain.auto_mine = True
    batcher.run_round()

    assert (result(x)["status"], result(y)["status"]) == ("mined", "mined")


def test_a_lagging_nonce_count_does_not_reuse_a_mined_nonce(batcher, chain):
    (x,) = queue(batcher, 1)
    chain.auto_mine = True
    batcher.run_round()
    (y,) = submit(CHAIN_ID, LP, intent(owner="swap:y:lifi", data="0xbeef"))
    real = chain.get_transaction_count
    stale = [0]
    chain.get_transaction_count = lambda address, block="latest": stale.pop() if stale else real(address, block)
    refuse_used_nonces(chain)

    batcher.run_round()

    assert result(x)["nonce"] == 0
    assert (result(y)["status"], result(y)["nonce"]) == ("mined", 1)


def test_an_identical_tx_is_never_settled_by_another_rows_receipt(batcher, chain):
    (x,) = submit(CHAIN_ID, LP, intent(owner="swap:x:deposit"))
    batcher.run_round()
    (y,) = submit(CHAIN_ID, LP, intent(owner="swap:y:deposit"))
    real = mine_during_receipt_lookup(chain)
    refuse_used_nonces(chain)

    batcher.run_round()
    chain.get_transaction_receipt = real
    chain.auto_mine = True
    batcher.run_round()

    assert len(chain.sent) == 2
    assert result(x)["tx_hash"] != result(y)["tx_hash"]


def test_a_nonce_another_sender_took_fails_the_tx_and_frees_the_queue(batcher, chain, test_db):
    (stuck,) = queue(batcher, 1)
    batcher.run_round()
    # Someone else's tx used nonce 0; ours can never be mined.
    chain.pool.clear()
    chain.mined_count = 1
    batcher.run_round()
    assert result(stuck)["status"] == "queued"
    batcher._used_since[stuck] -= module.STUCK_AFTER_SEC + 1
    (behind,) = submit(CHAIN_ID, LP, intent(owner="swap:b:lifi", data="0xbeef"))
    chain.auto_mine = True

    batcher.run_round()

    # Failed with its attempts: never signed again, so never sent twice.
    assert (result(stuck)["status"], len(result(stuck)["attempts"].split()), result(stuck)["nonce"]) == (
        "failed", 1, 0)
    assert (result(behind)["status"], result(behind)["nonce"]) == ("mined", 1)


def test_a_retry_the_node_refuses_waits_and_goes_out_once_taken(batcher, chain, test_db):
    # Out of gas, say: the nonce stays free, so the tx waits for a top-up
    # instead of failing its swap.
    (tx_id,) = queue(batcher, 1)
    batcher.run_round()
    chain.pool.clear()
    age(test_db, module.STUCK_AFTER_SEC + 1)
    chain.refuse = [Web3RPCError("insufficient funds for gas * price + value")]
    batcher.run_round()
    assert result(tx_id)["status"] == "queued"
    age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)
    chain.auto_mine = True

    batcher.run_round()

    assert result(tx_id)["status"] == "mined"


def test_a_tx_waits_out_the_receipt_window_before_it_or_the_txs_behind_go_out(batcher, chain, test_db, monkeypatch):
    (tx_id,) = queue(batcher, 1)
    batcher.run_round()
    monkeypatch.setattr(module, "RECEIPT_TIMEOUT_SEC", 60)
    chain.pool.clear()
    (behind,) = submit(CHAIN_ID, LP, intent(owner="swap:z:lifi", data="0xbeef"))
    for _ in range(3):
        batcher.run_round()
    assert len(result(tx_id)["attempts"].split()) == 1
    assert result(behind)["attempts"] is None
    age(test_db, 61)
    monkeypatch.setattr(module, "RECEIPT_TIMEOUT_SEC", 0.05)

    batcher.run_round()

    assert len(result(tx_id)["attempts"].split()) == 2


def test_a_stuck_tx_whose_nonce_is_free_never_moves_the_one_behind(batcher, chain, test_db):
    stuck, behind = queue(batcher, 2)
    batcher.run_round()
    chain.pool.pop(0)
    age(test_db, module.STUCK_AFTER_SEC + 1)

    batcher.run_round()

    # The first is signed again at its own nonce; the one behind is still held, so it waits.
    assert (result(stuck)["status"], result(behind)["status"]) == ("queued", "queued")
    assert [nonce_of(raw) for raw in chain.sent] == [0, 1, 0]
    chain.auto_mine = True
    batcher.run_round()
    assert len(executed(chain, behind)) == 1 and {n for n, _ in batcher._attempts(result(behind))} == {1}


def test_a_receipt_a_lagging_node_hides_never_resends_a_mined_tx(batcher, chain, test_db):
    hidden, seen = queue(batcher, 2)
    real = chain.get_transaction_receipt
    lagging = [True]

    def receipt(tx_hash):
        if chain.mined_count in chain.pool:
            chain.mine(len(chain.pool))
        if lagging[0] and tx_hash in result(hidden)["attempts"]:
            raise TransactionNotFound(tx_hash)
        return real(tx_hash)

    chain.get_transaction_receipt = receipt
    batcher.run_round()
    assert (result(hidden)["status"], result(seen)["nonce"]) == ("queued", 1)
    real_count = chain.get_transaction_count
    stale = [0]
    chain.get_transaction_count = lambda address, block="latest": stale.pop() if stale else real_count(address, block)
    age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)

    batcher.run_round()
    lagging[0] = False
    batcher.run_round()

    assert len(chain.sent) == 2
    assert result(hidden)["status"] == "mined"


def test_stopping_cuts_a_receipt_wait_short(batcher, chain, monkeypatch):
    import time as clock

    monkeypatch.setattr(module, "RECEIPT_TIMEOUT_SEC", 30)
    monkeypatch.setattr(module, "RECEIPT_POLL_SEC", 1)
    queue(batcher, 1)
    threading.Timer(0.1, batcher.stop.set).start()

    began = clock.monotonic()
    batcher.run_round()

    assert clock.monotonic() - began < 2

def test_a_retry_outbids_a_held_version_the_base_fee_priced_out(batcher, chain, test_db):
    (tx_id,) = queue(batcher, 1)
    batcher.run_round()
    first = TypedTransaction.from_bytes(HexBytes(chain.sent[0])).as_dict()
    age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)
    chain.base_fee = 50_000

    batcher.run_round()

    retry = TypedTransaction.from_bytes(HexBytes(chain.sent[1])).as_dict()
    assert retry["nonce"] == first["nonce"]
    for fee in ("maxFeePerGas", "maxPriorityFeePerGas"):
        assert retry[fee] >= first[fee] * 1.125


def test_a_held_version_that_can_still_be_mined_is_not_signed_again(batcher, chain, test_db):
    queue(batcher, 1)
    batcher.run_round()
    for _ in range(30):
        age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)
        batcher.run_round()

    assert len(chain.sent) == 1

def test_a_simulation_the_node_rejects_fails_the_tx_unsigned(batcher, chain):
    (doomed,) = submit(CHAIN_ID, LP, intent(owner="swap:a:lifi", data="0xpoor"))
    real = chain.call
    chain.call = lambda tx, *a: (_ for _ in ()).throw(Web3RPCError("insufficient funds for transfer")) \
        if tx["data"] == "0xpoor" else real(tx, *a)

    batcher.run_round()

    assert (result(doomed)["status"], result(doomed)["attempts"]) == ("failed", None)
    assert "insufficient funds for transfer" in result(doomed)["error"]


def rpc_error(message, code=-32000):
    """`message` as web3 raises a node's error reply."""
    return Web3RPCError(str({"code": code, "message": message}),
                        rpc_response={"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}})


@pytest.mark.parametrize("reply", ["already known", "unknown transaction", "context deadline exceeded", "limit exceeded"])
def test_a_reply_that_may_hide_a_taken_tx_is_tried_again_at_its_nonce(batcher, chain, test_db, reply):
    (tx_id,) = queue(batcher, 1)
    chain.refuse = [rpc_error(reply)]
    batcher.run_round()
    assert result(tx_id)["status"] == "queued"
    age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)
    chain.auto_mine = True

    batcher.run_round()

    assert {n for n, _ in batcher._attempts(result(tx_id))} == {0}
    assert result(tx_id)["status"] == "mined"

def test_a_round_without_progress_sleeps_instead_of_spinning(batcher, chain, monkeypatch):
    monkeypatch.setattr(module, "IDLE_INTERVAL_SEC", 0.05)
    queue(batcher, 1)
    batcher.run_round()
    chain.pool.clear()
    chain.mined_count = 1
    reads = []
    real = chain.get_transaction_count
    chain.get_transaction_count = lambda *a: reads.append(1) or real(*a)
    threading.Timer(0.2, batcher.stop.set).start()

    batcher.run()

    assert len(reads) <= 10


def test_a_block_landing_mid_round_never_moves_a_retry_to_a_new_nonce(batcher, chain, test_db):
    first, second = queue(batcher, 2)
    refuse_used_nonces(chain)
    batcher.run_round()
    age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)
    chain.pool.clear()
    chain.sent.clear()
    batcher.run_round()
    real = chain.get_transaction_receipt
    second_hashes = result(second)["attempts"]

    def receipt(tx_hash):
        # The block with both lands after the round looked up the first.
        if tx_hash in second_hashes and chain.mined_count in chain.pool:
            chain.mine(len(chain.pool))
        return real(tx_hash)

    chain.get_transaction_receipt = receipt
    age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)
    batcher.run_round()
    chain.get_transaction_receipt = real
    batcher.run_round()

    assert {n for n, _ in batcher._attempts(result(first))} == {0}
    assert len(executed(chain, first)) == 1
    assert result(first)["status"] == "mined"


def test_a_tx_re_signed_for_a_while_is_not_failed_when_its_receipt_lags(batcher, chain, test_db):
    (tx_id,) = queue(batcher, 1)
    batcher.run_round()
    chain.pool.clear()
    age(test_db, module.STUCK_AFTER_SEC + 1)
    batcher.run_round()
    chain.mine()
    real = chain.get_transaction_receipt
    chain.get_transaction_receipt = lambda tx_hash: (_ for _ in ()).throw(TransactionNotFound(tx_hash))

    batcher.run_round()
    chain.get_transaction_receipt = real
    batcher.run_round()

    assert result(tx_id)["status"] == "mined"


def test_a_tx_mined_long_after_it_was_sent_is_not_failed_when_its_receipt_lags(batcher, chain, test_db):
    # The clock starts when the nonce is first seen used, not when the tx was sent.
    (tx_id,) = queue(batcher, 1)
    batcher.run_round()
    age(test_db, module.STUCK_AFTER_SEC + 1)
    chain.mine()
    real = chain.get_transaction_receipt
    chain.get_transaction_receipt = lambda tx_hash: (_ for _ in ()).throw(TransactionNotFound(tx_hash))

    batcher.run_round()
    chain.get_transaction_receipt = real
    batcher.run_round()

    assert result(tx_id)["status"] == "mined"


@pytest.mark.parametrize("reply", ["out of gas", "invalid opcode: INVALID", "stack underflow (0 <=> 1)"])
def test_a_call_that_fails_as_built_fails_the_tx_unsigned(batcher, chain, reply):
    (doomed,) = submit(CHAIN_ID, LP, intent(owner="swap:a:lifi", data="0xbad0"))
    (fine,) = submit(CHAIN_ID, LP, intent(owner="swap:b:lifi"))
    real = chain.call
    chain.call = lambda tx, *a: (_ for _ in ()).throw(rpc_error(reply)) if tx["data"] == "0xbad0" else real(tx, *a)

    batcher.run_round()

    assert (result(doomed)["status"], result(doomed)["attempts"]) == ("failed", None)
    assert result(fine)["attempts"].startswith("0:")


def test_an_earlier_attempt_mined_after_a_re_sign_settles_the_row_by_its_hash(batcher, chain, test_db):
    (tx_id,) = queue(batcher, 1)
    batcher.run_round()
    age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)
    chain.base_fee = 50_000
    batcher.run_round()
    assert len(chain.sent) == 2
    chain.pool[0] = chain.sent[0]
    chain.mine()

    batcher.run_round()

    assert (result(tx_id)["status"], result(tx_id)["tx_hash"]) == ("mined", hash_of(chain.sent[0]))


def test_a_held_version_outbid_by_the_market_tip_is_signed_again(batcher, chain, test_db):
    queue(batcher, 1)
    batcher.run_round()
    age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)
    chain.max_priority_fee = 500

    batcher.run_round()

    assert len(chain.sent) == 2
    assert TypedTransaction.from_bytes(HexBytes(chain.sent[1])).as_dict()["maxPriorityFeePerGas"] >= 500


def test_a_failed_send_logs_what_the_chain_says_about_it(batcher, chain, caplog):
    queue(batcher, 1)
    chain.refuse = [ConnectionError("response lost")]

    with caplog.at_level("WARNING", logger="src.clients.evm_batcher"):
        batcher.run_round()

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "latest_nonce=0 pending_nonce=0 known_to_node=False" in logged


@pytest.fixture
def sapphire(test_db, settings):
    return sapphire_sender(FakeChain(SAPPHIRE_CHAIN_ID), test_db, settings.liquidity_provider_secret_key)


def test_sapphire_calldata_goes_out_encrypted_to_the_current_key_on_every_attempt(sapphire, test_db):
    chain = sapphire.evm.w3.eth
    (tx_id,) = submit(SAPPHIRE_CHAIN_ID, LP, intent(data="0xa9059cbb" + "00" * 64))
    sapphire.run_round()
    chain.pool.clear()
    chain.epoch = 2
    age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)

    sapphire.run_round()

    first, retry = (decode(raw) for raw in chain.sent)
    assert first["nonce"] == retry["nonce"]
    for tx, epoch in ((first, 1), (retry, 2)):
        data = Web3.to_hex(tx["data"])
        assert not data.startswith("0xa9059cbb")
        assert chain.envelope(data)["epoch"] == epoch
        assert chain.plaintext(data) == result(tx_id)["data"]


def test_a_sapphire_retry_outbids_a_held_gas_price_only_once_it_is_priced_out(sapphire, test_db):
    chain = sapphire.evm.w3.eth
    submit(SAPPHIRE_CHAIN_ID, LP, intent())
    sapphire.run_round()
    for _ in range(3):
        age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)
        sapphire.run_round()
    assert len(chain.sent) == 1
    chain.gas_price = 101
    age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)

    sapphire.run_round()

    first, retry = decode(chain.sent[0]), decode(chain.sent[1])
    assert retry["nonce"] == first["nonce"]
    assert retry["gasPrice"] >= first["gasPrice"] * 1.125


def test_a_sapphire_call_is_signed_and_a_failure_short_of_a_revert_is_retried(test_db, settings):
    calls = []

    class SignedCalls:
        def call(self, tx):
            calls.append(tx)
            if len(calls) == 1:
                raise rpc_error("base block not found")
            return b""

    sender = sapphire_sender(FakeChain(SAPPHIRE_CHAIN_ID), test_db, settings.liquidity_provider_secret_key,
                             SignedCalls())
    (tx_id,) = submit(SAPPHIRE_CHAIN_ID, LP, intent(data="0xabcd"))
    sender.run_round()
    assert (result(tx_id)["status"], result(tx_id)["attempts"]) == ("queued", None)

    sender.run_round()

    assert result(tx_id)["attempts"]
    # sapphirepy cannot sign a call with gas or fee fields.
    assert calls[-1] == {"from": sender.evm.address, "to": Web3.to_checksum_address(TARGET), "data": "0xabcd",
                         "value": 0}


def test_a_retry_is_sent_even_when_its_call_now_reverts(batcher, chain, test_db):
    # Its first attempt may have run: the state it reverts against can be its own.
    (tx_id,) = queue(batcher, 1)
    batcher.run_round()
    age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)
    chain.base_fee = 50_000
    chain.reverting_calls.add("0x")

    batcher.run_round()

    assert (result(tx_id)["status"], len(chain.sent)) == ("queued", 2)
