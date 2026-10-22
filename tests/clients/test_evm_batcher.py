import json
import threading

import pytest
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import ContractLogicError, TransactionNotFound, Web3RPCError

import src.clients.evm_batcher as module
from src.clients.base_evm import EvmClient
from src.clients.evm_batcher import EvmBatcher, TxIntent, result, submit

CHAIN_ID = 8453
TARGET = "0x" + "dd" * 20


class Unreachable(ConnectionError):
    """The RPC did not answer."""


class FakeChain:
    """A node that takes txs and mines them only when told to, or on its own
    once `auto_mine` is set."""

    def __init__(self) -> None:
        self.mined_count = 0
        self.pool: dict[int, str] = {}
        self.sent: list[str] = []
        self.receipts: dict[str, dict] = {}
        self.chain_id = CHAIN_ID
        self.max_priority_fee = 100
        self.base_fee = 1_000
        self.auto_mine = False
        self.reverting_calls: set[str] = set()
        self.refuse: list[Exception] = []
        self.on_send = None

    def get_transaction_count(self, address, block="latest"):
        if block == "pending":
            return max([self.mined_count, *[n + 1 for n in self.pool]])
        return self.mined_count

    def get_block(self, _):
        return {"baseFeePerGas": self.base_fee}

    def call(self, tx, *_):
        if tx["data"] == "0xdown":
            raise Unreachable("rpc timed out")
        if tx["data"] in self.reverting_calls:
            raise ContractLogicError("execution reverted")
        return b""

    def send_raw_transaction(self, raw):
        raw = Web3.to_hex(HexBytes(raw))
        if self.on_send is not None:
            self.on_send(raw)
        if self.refuse:
            raise self.refuse.pop(0)
        self.sent.append(raw)
        self.pool[nonce_of(raw)] = raw
        return Web3.keccak(hexstr=raw)

    def mine(self, count: int = 1, status: int = 1) -> None:
        for _ in range(count):
            raw = self.pool.pop(self.mined_count)
            self.receipts[Web3.to_hex(Web3.keccak(hexstr=raw))] = {"status": status, "logs": []}
            self.mined_count += 1

    def get_transaction(self, tx_hash):
        for raw in self.pool.values():
            if hash_of(raw) == tx_hash:
                return TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
        raise TransactionNotFound(tx_hash)

    def get_transaction_receipt(self, tx_hash):
        if self.auto_mine:
            while self.mined_count in self.pool:
                self.mine()
        if tx_hash not in self.receipts:
            raise TransactionNotFound(tx_hash)
        return self.receipts[tx_hash]


def nonce_of(raw: str) -> int:
    return TypedTransaction.from_bytes(HexBytes(raw)).as_dict()["nonce"]


def hash_of(raw: str) -> str:
    return Web3.to_hex(Web3.keccak(hexstr=raw))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(module, "RECEIPT_POLL_SEC", 0)
    monkeypatch.setattr(module, "RECEIPT_TIMEOUT_SEC", 0.05)


@pytest.fixture
def chain():
    return FakeChain()


@pytest.fixture
def batcher(chain, test_db, settings):
    # The LP account, which `submit` queues for.
    evm = EvmClient("http://localhost:1", settings.liquidity_provider_secret_key)
    evm.w3.eth = chain
    return EvmBatcher(evm, CHAIN_ID, test_db, threading.Event())


def intent(owner="swap:a:lifi", **fields):
    return TxIntent(owner=owner, to=TARGET, gas=21_000, **fields)


def queue(batcher, count, **fields):
    return [submit(CHAIN_ID, intent(owner=f"swap:{i}:lifi", **fields)) for i in range(count)]


def test_submit_only_queues_the_tx(batcher, chain):
    tx_id = submit(CHAIN_ID, TxIntent(owner="swap:a:deposit", to=TARGET, gas=65_000, data="0xabcd", value=5))

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
        seen.append(f"{nonce_of(raw)}:{hash_of(raw)}" in result(tx_id)["attempts"])

    chain.on_send = saved_first

    batcher.run_round()

    assert seen == [True]


def test_a_tx_mined_before_a_restart_is_never_sent_again(batcher, chain):
    (tx_id,) = queue(batcher, 1)
    batcher.run_round()
    # Mined after the round gave up waiting, and the service restarted.
    chain.mine()
    sent = list(chain.sent)

    EvmBatcher(batcher.evm, CHAIN_ID, batcher.db, threading.Event()).run_round()

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
    queue(batcher, 1)
    batcher.run_round()
    # The node counts the nonce as used before it serves the receipt.
    chain.pool.clear()
    chain.mined_count = 1
    sent = list(chain.sent)

    batcher.run_round()

    assert chain.sent == sent


def test_a_tx_whose_simulation_reverts_fails_without_using_a_nonce(batcher, chain):
    doomed = submit(CHAIN_ID, intent(owner="swap:a:lifi", data="0xdead"))
    fine = submit(CHAIN_ID, intent(owner="swap:b:lifi"))
    chain.reverting_calls.add("0xdead")

    batcher.run_round()

    assert result(doomed)["status"] == "failed"
    assert "simulation failed: ContractLogicError" in result(doomed)["error"]
    assert result(fine)["attempts"].startswith("0:")


def test_a_dependent_tx_skips_simulation(batcher, chain):
    tx_id = submit(CHAIN_ID, intent(data="0xdead", simulate=False))
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


def test_a_refused_retry_stays_queued_as_its_first_attempt_may_still_land(batcher, chain):
    (tx_id,) = queue(batcher, 1)
    batcher.run_round()
    chain.pool.clear()
    chain.refuse = [Web3RPCError("insufficient funds for gas * price + value")]

    batcher.run_round()

    assert result(tx_id)["status"] == "queued"


def test_a_send_that_may_have_reached_the_node_stays_queued(batcher, chain):
    (tx_id,) = queue(batcher, 1)
    chain.refuse = [ConnectionError("response lost")]

    batcher.run_round()

    assert result(tx_id)["status"] == "queued"
    assert result(tx_id)["attempts"].startswith("0:")


def test_a_round_takes_at_most_the_batch_size(batcher, chain, monkeypatch):
    monkeypatch.setattr(module, "BATCH_SIZE", 3)
    queue(batcher, 5)

    batcher.run_round()

    assert len(chain.sent) == 3


def test_an_empty_queue_asks_the_node_nothing(batcher, chain, monkeypatch):
    monkeypatch.setattr(chain, "get_transaction_count", lambda *a: (_ for _ in ()).throw(AssertionError))

    assert batcher.run_round() is False


def test_an_rpc_failure_mid_batch_still_settles_what_was_sent(batcher, chain):
    sent_first = submit(CHAIN_ID, intent(owner="swap:a:lifi"))
    unreachable = submit(CHAIN_ID, intent(owner="swap:b:lifi", data="0xdown"))
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


def test_one_batcher_thread_starts_per_configured_chain(monkeypatch, settings):
    from dataclasses import replace

    served = []
    started = threading.Event()

    class Chain:
        def __init__(self, chain_id):
            self.chain_id = chain_id

    class Batcher:
        def __init__(self, evm, chain_id, db, stop):
            self.chain_id = chain_id
            self.stop = stop

        def run(self):
            served.append(self.chain_id)
            if len(served) == 2:
                started.set()
            self.stop.wait()

    def client(rpc_url, key):
        evm = EvmClient("http://localhost:1", key)
        evm.w3.eth = Chain({"http://base": 8453, "http://eth": 1, "http://base-again": 8453}[rpc_url])
        return evm

    monkeypatch.setattr(module, "load_settings", lambda: replace(
        settings, base_rpc_url="http://base", ethereum_rpc_url="http://eth", hyperevm_rpc_url="http://base-again"))
    monkeypatch.setattr(module, "EvmClient", client)
    monkeypatch.setattr(module, "EvmBatcher", Batcher)
    monkeypatch.setattr(module, "connect", lambda: __import__("sqlite3").connect(":memory:"))

    module.start_evm_batchers()
    try:
        assert started.wait(5)
    finally:
        module.stop_evm_batchers()

    # A second RPC for chain 8453 would be a second sender for its nonces.
    assert sorted(served) == [1, 8453]


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
    test_db.execute("UPDATE evm_transactions SET sent_at = sent_at - ?, updated_at = updated_at - ?",
                    (seconds, seconds))


def test_a_tx_mined_while_the_round_reconciles_does_not_stall_the_queue(batcher, chain):
    (x,) = queue(batcher, 1)
    batcher.run_round()
    (y,) = [submit(CHAIN_ID, intent(owner="swap:y:lifi", data="0xbeef"))]
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
    (y,) = [submit(CHAIN_ID, intent(owner="swap:y:lifi", data="0xbeef"))]
    real = chain.get_transaction_count
    stale = [0]
    chain.get_transaction_count = lambda address, block="latest": stale.pop() if stale else real(address, block)
    refuse_used_nonces(chain)

    batcher.run_round()

    assert result(x)["nonce"] == 0
    assert (result(y)["status"], result(y)["nonce"]) == ("mined", 1)


def test_an_identical_tx_is_never_settled_by_another_rows_receipt(batcher, chain):
    (x,) = [submit(CHAIN_ID, intent(owner="swap:x:deposit"))]
    batcher.run_round()
    (y,) = [submit(CHAIN_ID, intent(owner="swap:y:deposit"))]
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
    age(test_db, module.STUCK_AFTER_SEC + 1)
    (behind,) = [submit(CHAIN_ID, intent(owner="swap:b:lifi", data="0xbeef"))]
    chain.auto_mine = True

    batcher.run_round()

    # Failed with its attempts: never signed again, so never sent twice.
    assert (result(stuck)["status"], len(result(stuck)["attempts"].split())) == ("failed", 1)
    assert result(behind)["status"] == "mined"


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


def test_a_tx_waits_out_the_receipt_window_before_it_is_signed_again(batcher, chain, test_db, monkeypatch):
    (tx_id,) = queue(batcher, 1)
    batcher.run_round()
    monkeypatch.setattr(module, "RECEIPT_TIMEOUT_SEC", 60)
    for _ in range(3):
        batcher.run_round()
    assert len(result(tx_id)["attempts"].split()) == 1
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

    # Each is signed again at its own nonce only, never the other's.
    assert (result(stuck)["status"], result(behind)["status"]) == ("queued", "queued")
    assert [nonce_of(raw) for raw in chain.sent] == [0, 1, 0, 1]


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

def test_a_retry_outbids_the_attempt_the_node_still_holds(batcher, chain, test_db):
    (tx_id,) = queue(batcher, 1)
    batcher.run_round()
    first = TypedTransaction.from_bytes(HexBytes(chain.sent[0])).as_dict()
    age(test_db, module.RECEIPT_TIMEOUT_SEC + 1)

    batcher.run_round()

    retry = TypedTransaction.from_bytes(HexBytes(chain.sent[1])).as_dict()
    assert retry["nonce"] == first["nonce"]
    for fee in ("maxFeePerGas", "maxPriorityFeePerGas"):
        assert retry[fee] >= first[fee] * 1.125


def test_a_simulation_the_node_rejects_fails_the_tx_unsigned(batcher, chain):
    (doomed,) = [submit(CHAIN_ID, intent(owner="swap:a:lifi", data="0xpoor"))]
    real = chain.call
    chain.call = lambda tx, *a: (_ for _ in ()).throw(Web3RPCError("insufficient funds for transfer")) \
        if tx["data"] == "0xpoor" else real(tx, *a)

    batcher.run_round()

    assert (result(doomed)["status"], result(doomed)["attempts"]) == ("failed", None)
    assert "insufficient funds for transfer" in result(doomed)["error"]


def test_only_exact_already_known_replies_count_as_sent(batcher, chain):
    ids = queue(batcher, 4)
    chain.refuse = [Web3RPCError(m) for m in ("already known", "known transaction", "AlreadyKnown", "unknown transaction")]

    batcher.run_round()

    assert [result(i)["status"] for i in ids] == ["queued", "queued", "queued", "failed"]


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


def test_a_failed_send_is_logged_without_its_rpc_url(batcher, chain, caplog):
    queue(batcher, 1)
    chain.refuse = [ConnectionError("https://rpc.example/v2/SECRETKEY timed out")]

    with caplog.at_level("WARNING", logger="src.clients.evm_batcher"):
        batcher.run_round()

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "ConnectionError: [url] timed out" in logged
    assert "SECRETKEY" not in logged


def test_an_rpc_key_in_a_request_path_never_reaches_the_log():
    error = ConnectionError("HTTPSConnectionPool(host='base-mainnet.g.alchemy.com', port=443): "
                            "Max retries exceeded with url: /v2/SECRETKEY (Caused by timeout)")

    logged = module._error(error)

    assert "SECRETKEY" not in logged
    assert "with url: [url]" in logged
