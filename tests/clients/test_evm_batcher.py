import asyncio
import json

import pytest
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import ContractLogicError, TransactionNotFound, Web3RPCError

import src.clients.evm_batcher as module
from src.clients.base_evm import EvmClient
from src.clients.evm_batcher import EvmBatcher, TxIntent

CHAIN_ID = 8453
KEY = "0x" + "11" * 32
TARGET = "0x" + "dd" * 20


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
def batcher(chain):
    evm = EvmClient("http://localhost:1", KEY)
    evm.w3.eth = chain
    batcher = EvmBatcher(evm, CHAIN_ID)
    # Tests drive rounds themselves; the loop submit starts would run them too.
    batcher.start = lambda: None
    return batcher


def intent(owner="swap:a:lifi", **fields):
    return TxIntent(owner=owner, to=TARGET, gas=21_000, **fields)


def queue(batcher, count, **fields):
    return [batcher.submit(intent(owner=f"swap:{i}:lifi", **fields)) for i in range(count)]


def test_submit_only_queues_the_tx(batcher, chain):
    tx_id = batcher.submit(TxIntent(owner="swap:a:deposit", to=TARGET, gas=65_000, data="0xabcd", value=5))

    row = batcher.result(tx_id)
    assert (row["status"], row["owner"], row["data"], row["value"], row["gas"]) == (
        "queued", "swap:a:deposit", "0xabcd", "5", 65_000,
    )
    assert chain.sent == []


async def test_a_round_sends_the_batch_back_to_back_and_waits_only_for_the_last(batcher, chain):
    ids = queue(batcher, 5)
    asked = []
    real = chain.get_transaction_receipt

    def receipt(tx_hash):
        asked.append(tx_hash)
        return real(tx_hash)

    chain.get_transaction_receipt = receipt
    chain.auto_mine = True

    await batcher.run_round()

    # All five went out before any receipt was asked for, and the first asked is the last's.
    assert [nonce_of(raw) for raw in chain.sent] == [0, 1, 2, 3, 4]
    assert asked[0] == hash_of(chain.sent[-1])
    assert [batcher.result(i)["status"] for i in ids] == ["mined"] * 5


async def test_each_tx_gets_its_receipt_status_and_logs(batcher, chain):
    ok, bad = queue(batcher, 2)
    await batcher.run_round()
    chain.mine()
    chain.mine(status=0)

    await batcher.run_round()

    assert batcher.result(ok)["status"] == "mined"
    assert batcher.result(bad)["status"] == "reverted"
    assert json.loads(batcher.result(ok)["logs"]) == []
    assert batcher.result(ok)["tx_hash"] == hash_of(chain.sent[0])


async def test_every_attempt_is_saved_before_it_reaches_the_node(batcher, chain):
    (tx_id,) = queue(batcher, 1)
    seen = []

    def saved_first(raw):
        seen.append(f"{nonce_of(raw)}:{hash_of(raw)}" in batcher.result(tx_id)["attempts"])

    chain.on_send = saved_first

    await batcher.run_round()

    assert seen == [True]


async def test_a_tx_mined_before_a_restart_is_never_sent_again(batcher, chain):
    (tx_id,) = queue(batcher, 1)
    await batcher.run_round()
    # Mined after the round gave up waiting, and the service restarted.
    chain.mine()
    sent = list(chain.sent)

    await EvmBatcher(batcher.evm, CHAIN_ID).run_round()

    assert chain.sent == sent
    assert batcher.result(tx_id)["status"] == "mined"


async def test_a_partly_mined_batch_resends_only_the_stuck_tx_at_its_nonce(batcher, chain):
    first, stuck = queue(batcher, 2)
    await batcher.run_round()
    chain.mine()
    chain.pool.clear()

    await batcher.run_round()

    assert batcher.result(first)["status"] == "mined"
    assert [nonce_of(raw) for raw in chain.sent] == [0, 1, 1]
    assert len(batcher.result(stuck)["attempts"].split()) == 2


async def test_a_used_nonce_without_a_receipt_yet_holds_the_round(batcher, chain):
    queue(batcher, 1)
    await batcher.run_round()
    # The node counts the nonce as used before it serves the receipt.
    chain.pool.clear()
    chain.mined_count = 1
    sent = list(chain.sent)

    await batcher.run_round()

    assert chain.sent == sent


async def test_a_tx_whose_simulation_reverts_fails_without_using_a_nonce(batcher, chain):
    doomed = batcher.submit(intent(owner="swap:a:lifi", data="0xdead"))
    fine = batcher.submit(intent(owner="swap:b:lifi"))
    chain.reverting_calls.add("0xdead")

    await batcher.run_round()

    assert batcher.result(doomed)["status"] == "failed"
    assert "simulation reverted" in batcher.result(doomed)["error"]
    assert batcher.result(fine)["attempts"].startswith("0:")


async def test_a_dependent_tx_skips_simulation(batcher, chain):
    tx_id = batcher.submit(intent(data="0xdead", simulate=False))
    chain.reverting_calls.add("0xdead")

    await batcher.run_round()

    assert batcher.result(tx_id)["attempts"]


async def test_a_nonce_error_stops_the_batch_for_the_next_round(batcher, chain):
    first, second = queue(batcher, 2)
    chain.refuse = [Web3RPCError("nonce too low")]

    await batcher.run_round()

    assert chain.sent == []
    assert batcher.result(first)["status"] == "queued"
    assert batcher.result(second)["attempts"] is None


async def test_a_tx_the_node_refuses_fails_and_frees_its_nonce(batcher, chain):
    refused, fine = queue(batcher, 2)
    chain.refuse = [Web3RPCError("insufficient funds for gas * price + value")]

    await batcher.run_round()

    assert batcher.result(refused)["status"] == "failed"
    assert "insufficient funds" in batcher.result(refused)["error"]
    assert [nonce_of(raw) for raw in chain.sent] == [0]
    assert batcher.result(fine)["attempts"].startswith("0:")


async def test_a_refused_retry_stays_queued_as_its_first_attempt_may_still_land(batcher, chain):
    (tx_id,) = queue(batcher, 1)
    await batcher.run_round()
    chain.pool.clear()
    chain.refuse = [Web3RPCError("insufficient funds for gas * price + value")]

    await batcher.run_round()

    assert batcher.result(tx_id)["status"] == "queued"


async def test_a_send_that_may_have_reached_the_node_stays_queued(batcher, chain):
    (tx_id,) = queue(batcher, 1)
    chain.refuse = [ConnectionError("response lost")]

    await batcher.run_round()

    assert batcher.result(tx_id)["status"] == "queued"
    assert batcher.result(tx_id)["attempts"].startswith("0:")


async def test_a_round_takes_at_most_the_batch_size(batcher, chain, monkeypatch):
    monkeypatch.setattr(module, "BATCH_SIZE", 3)
    queue(batcher, 5)

    await batcher.run_round()

    assert len(chain.sent) == 3


async def test_an_empty_queue_asks_the_node_nothing(batcher, chain, monkeypatch):
    monkeypatch.setattr(chain, "get_transaction_count", lambda *a: (_ for _ in ()).throw(AssertionError))

    assert await batcher.run_round() is False


async def test_submit_starts_the_loop_that_drains_the_queue(batcher, chain):
    del batcher.start
    chain.auto_mine = True

    tx_id = batcher.submit(intent())
    for _ in range(100):
        if batcher.result(tx_id)["status"] != "queued":
            break
        await asyncio.sleep(0.01)

    assert batcher.result(tx_id)["status"] == "mined"


def test_threads_asking_at_once_share_one_batcher_per_chain(monkeypatch):
    import threading
    import time as clock

    monkeypatch.setattr(module, "_batchers", {})

    def slow_client(chain_id):
        clock.sleep(0.05)
        return EvmClient("http://localhost:1", KEY)

    monkeypatch.setattr(module, "get_evm_client", slow_client)
    got = []
    threads = [threading.Thread(target=lambda: got.append(module.get_evm_batcher(1))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len({id(b) for b in got}) == 1
    assert module.get_evm_batcher(1) is not module.get_evm_batcher(8453)


async def test_startup_starts_only_chains_with_queued_lp_txs(test_db, monkeypatch, settings):
    lp = settings.liquidity_provider_address.lower()
    for chain_id, address, status in [(8453, lp, "queued"), (1, lp, "mined"), (999, "0xother", "queued")]:
        test_db.execute(
            "INSERT INTO evm_transactions (chain_id, address, owner, to_address, data, value, gas, status, "
            "created_at, updated_at) VALUES (?, ?, 'swap:a:lifi', '0xto', '0x', '0', 21000, ?, 0, 0)",
            (chain_id, address, status),
        )
    started = asyncio.Event()
    asked = []

    class Started:
        def start(self):
            started.set()

    def flaky(chain_id):
        asked.append(chain_id)
        if len(asked) == 1:
            raise ConnectionError("rpc down")
        return Started()

    monkeypatch.setattr(module, "get_evm_batcher", flaky)
    monkeypatch.setattr(module, "IDLE_INTERVAL_SEC", 0.01)

    module.start_evm_batcher_recovery()
    await asyncio.wait_for(started.wait(), timeout=5)

    assert asked == [8453, 8453]
