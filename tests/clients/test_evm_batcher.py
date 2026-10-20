import asyncio

import pytest
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound

from src.clients.base_evm import EvmClient
from src.clients.evm_batcher import EvmBatcher, TxIntent

CHAIN_ID = 8453
KEY = "0x" + "11" * 32
TARGET = "0x" + "dd" * 20


class FakeChain:
    """A node that takes txs and mines them only when told to."""

    def __init__(self, mined_count: int = 0) -> None:
        self.mined_count = mined_count
        self.pool: dict[int, str] = {}
        self.sent: list[str] = []
        self.receipts: dict[str, dict] = {}
        self.chain_id = CHAIN_ID
        self.max_priority_fee = 100
        self.base_fee = 1_000
        self.reject_next = 0
        self.on_send = None

    def get_transaction_count(self, address, block="latest"):
        if block == "pending":
            return max([self.mined_count, *[n + 1 for n in self.pool]])
        return self.mined_count

    def get_block(self, _):
        return {"baseFeePerGas": self.base_fee}

    def get_balance(self, _):
        return 10**30

    def send_raw_transaction(self, raw):
        raw = Web3.to_hex(HexBytes(raw))
        if self.on_send is not None:
            self.on_send(raw)
        if self.reject_next:
            self.reject_next -= 1
            raise ConnectionError("node unreachable")
        self.sent.append(raw)
        self.pool[nonce_of(raw)] = raw
        return Web3.keccak(hexstr=raw)

    def mine(self, count: int = 1, status: int = 1) -> None:
        for _ in range(count):
            raw = self.pool.pop(self.mined_count)
            self.receipts[Web3.to_hex(Web3.keccak(hexstr=raw))] = {"status": status}
            self.mined_count += 1

    def get_transaction_receipt(self, tx_hash):
        if tx_hash not in self.receipts:
            raise TransactionNotFound(tx_hash)
        return self.receipts[tx_hash]


def nonce_of(raw: str) -> int:
    return TypedTransaction.from_bytes(HexBytes(raw)).as_dict()["nonce"]


@pytest.fixture
def chain():
    return FakeChain()


@pytest.fixture
def batcher(chain):
    evm = EvmClient("http://localhost:1", KEY)
    evm.w3.eth = chain
    return EvmBatcher(evm, CHAIN_ID)


def intent(owner="swap:a:lifi", **fields):
    return TxIntent(owner=owner, to=TARGET, gas=21_000, **fields)


async def test_swaps_on_one_chain_send_without_waiting_for_each_others_receipts(batcher, chain):
    # Nothing is ever mined: under a lock held through each receipt, the
    # second send would wait forever.
    sent = await asyncio.wait_for(
        asyncio.gather(*(batcher.submit(intent(owner=f"swap:{i}:lifi")) for i in range(5))),
        timeout=5,
    )

    assert sorted(tx.nonce for tx in sent) == [0, 1, 2, 3, 4]
    assert [nonce_of(raw) for raw in chain.sent] == [0, 1, 2, 3, 4]
    assert chain.mined_count == 0


async def test_each_tx_is_saved_before_it_reaches_the_node(batcher, chain, test_db):
    def saved_first(raw):
        row = test_db.execute(
            "SELECT raw FROM evm_transactions WHERE nonce = ?", (nonce_of(raw),)
        ).fetchone()
        assert row is not None and row["raw"] == raw

    chain.on_send = saved_first

    sent = await batcher.submit(intent(owner="swap:a:deposit", value=5))

    row = test_db.execute("SELECT * FROM evm_transactions").fetchone()
    assert (row["chain_id"], row["nonce"], row["owner"], row["status"]) == (CHAIN_ID, 0, "swap:a:deposit", "sent")
    assert row["tx_hash"] == sent.tx_hash
    assert row["address"] == Web3.to_checksum_address(row["address"]).lower()


async def test_a_failed_broadcast_still_holds_its_nonce(batcher, chain, test_db):
    # The node may have taken it before the error, so the nonce is not reused.
    chain.reject_next = 1

    first = await batcher.submit(intent(owner="swap:a:lifi"))
    second = await batcher.submit(intent(owner="swap:b:lifi"))

    assert (first.nonce, second.nonce) == (0, 1)
    assert test_db.execute("SELECT COUNT(*) FROM evm_transactions").fetchone()[0] == 2


async def test_nonces_start_past_both_the_node_and_saved_txs(chain, test_db):
    evm = EvmClient("http://localhost:1", KEY)
    evm.w3.eth = chain
    address = evm.address.lower()
    test_db.execute(
        "INSERT INTO evm_transactions (chain_id, address, nonce, owner, tx_hash, raw, created_at, updated_at) "
        "VALUES (?, ?, 9, 'swap:old:lifi', '0xaa', '0x01', 0, 0)",
        (CHAIN_ID, address),
    )
    chain.mined_count = 4

    assert (await EvmBatcher(evm, CHAIN_ID).submit(intent())).nonce == 10

    chain.mined_count = 20
    assert (await EvmBatcher(evm, CHAIN_ID).submit(intent())).nonce == 20


async def test_a_failure_before_saving_leaves_the_nonce_free(batcher, chain, monkeypatch):
    real_sign = batcher.evm.sign
    calls = []

    def sign_once_broken(tx):
        calls.append(tx["nonce"])
        if len(calls) == 1:
            raise ValueError("bad tx")
        return real_sign(tx)

    monkeypatch.setattr(batcher.evm, "sign", sign_once_broken)

    with pytest.raises(ValueError):
        await batcher.submit(intent(owner="swap:a:lifi"))
    assert (await batcher.submit(intent(owner="swap:b:lifi"))).nonce == 0


async def test_an_unpriceable_batch_fails_without_sending(batcher, chain, monkeypatch):
    monkeypatch.setattr(batcher.evm, "fees", lambda: (_ for _ in ()).throw(ConnectionError("rpc down")))

    with pytest.raises(ConnectionError):
        await batcher.submit(intent())
    assert chain.sent == []


async def test_intents_queued_together_share_one_fee_price(batcher, chain, monkeypatch):
    priced = []
    real_fees = batcher.evm.fees
    monkeypatch.setattr(batcher.evm, "fees", lambda: priced.append(1) or real_fees())

    await asyncio.gather(*(batcher.submit(intent(owner=f"swap:{i}:lifi")) for i in range(4)))

    assert len(priced) < 4


async def test_a_cancelled_callers_intent_is_never_sent(batcher, chain, test_db):
    # It would be a tx nobody tracks, and a caller retrying would pay twice.
    waiting = asyncio.create_task(batcher.submit(intent(owner="swap:a:lifi")))
    await asyncio.sleep(0)
    waiting.cancel()

    sent = await asyncio.wait_for(batcher.submit(intent(owner="swap:b:lifi")), timeout=5)

    assert sent.nonce == 0
    assert [r["owner"] for r in test_db.execute("SELECT owner FROM evm_transactions")] == ["swap:b:lifi"]


async def test_an_intent_the_table_rejects_leaves_its_nonce_to_the_next(batcher, chain):
    with pytest.raises(Exception):
        await batcher.submit(TxIntent(owner=None, to=TARGET, gas=21_000))

    assert (await batcher.submit(intent())).nonce == 0


def test_threads_asking_at_once_share_one_batcher_per_chain(monkeypatch):
    import threading
    import time as clock

    import src.clients.evm_batcher as module

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


def test_each_chain_gets_its_own_batcher(monkeypatch):
    import src.clients.evm_batcher as module

    monkeypatch.setattr(module, "_batchers", {})
    monkeypatch.setattr(module, "get_evm_client", lambda chain_id: EvmClient("http://localhost:1", KEY))

    assert module.get_evm_batcher(1) is module.get_evm_batcher(1)
    assert module.get_evm_batcher(1) is not module.get_evm_batcher(8453)


def row_at(test_db, nonce):
    return test_db.execute("SELECT * FROM evm_transactions WHERE nonce = ?", (nonce,)).fetchone()


async def test_mined_and_reverted_txs_settle_by_their_receipt(batcher, chain, test_db):
    await asyncio.gather(*(batcher.submit(intent(owner=f"swap:{i}:lifi")) for i in range(3)))
    chain.mine()
    chain.mine(status=0)

    await batcher.maintain()

    assert [row_at(test_db, n)["status"] for n in range(3)] == ["mined", "reverted", "sent"]
    assert batcher.saved(1)["status"] == "reverted"


async def test_dropped_txs_go_out_again_lowest_nonce_first(batcher, chain):
    await asyncio.gather(*(batcher.submit(intent(owner=f"swap:{i}:lifi")) for i in range(3)))
    chain.pool.clear()
    chain.sent.clear()

    await batcher.maintain()

    assert [nonce_of(raw) for raw in chain.sent] == [0, 1, 2]


async def test_a_priced_out_tx_is_re_signed_and_the_new_version_settles_it(batcher, chain, test_db):
    first = await batcher.submit(intent())
    chain.base_fee = 50_000

    await batcher.maintain()

    row = row_at(test_db, 0)
    assert row["tx_hash"] != first.tx_hash
    assert row["replaced"] == first.tx_hash
    assert nonce_of(row["raw"]) == 0
    chain.mine()
    await batcher.maintain()
    row = row_at(test_db, 0)
    assert (row["status"], row["replaced"]) == ("mined", first.tx_hash)


async def test_the_original_version_can_still_be_the_one_mined(batcher, chain, test_db):
    first = await batcher.submit(intent())
    chain.base_fee = 50_000
    await batcher.maintain()
    replacement = row_at(test_db, 0)["tx_hash"]
    # A node mined the first version before it saw the replacement.
    chain.mined_count = 1
    chain.receipts[first.tx_hash] = {"status": 1}

    await batcher.maintain()

    row = row_at(test_db, 0)
    assert (row["status"], row["tx_hash"], row["replaced"]) == ("mined", first.tx_hash, replacement)


async def test_a_used_nonce_without_a_receipt_yet_stays_open(batcher, chain, test_db):
    await batcher.submit(intent())
    # The node counts the nonce as used before its receipt is served.
    chain.mined_count = 1

    await batcher.maintain()

    assert row_at(test_db, 0)["status"] == "sent"


async def test_a_round_with_nothing_open_asks_the_node_nothing(batcher, chain, monkeypatch):
    monkeypatch.setattr(chain, "get_transaction_count", lambda *a: (_ for _ in ()).throw(AssertionError))

    await batcher.maintain()


def save_unsettled(test_db, batcher, nonce, status="sent"):
    tx = batcher._tx(intent(owner=f"swap:old{nonce}:lifi"), nonce, batcher.evm.fees())
    tx_hash, raw = batcher.evm.sign(tx)
    test_db.execute(
        "INSERT INTO evm_transactions (chain_id, address, nonce, owner, tx_hash, raw, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'swap:old:lifi', ?, ?, ?, 0, 0)",
        (CHAIN_ID, batcher.address, nonce, tx_hash, raw, status),
    )
    return raw


async def test_after_a_restart_saved_txs_go_out_before_new_ones(batcher, chain, test_db):
    # Signed and saved before the restart; the node has since dropped them.
    old = [save_unsettled(test_db, batcher, n) for n in (0, 1)]

    sent = await batcher.submit(intent(owner="swap:new:lifi"))

    assert sent.nonce == 2
    assert chain.sent == [*old, row_at(test_db, 2)["raw"]]


async def test_a_failed_recovery_round_does_not_block_new_sends(batcher, chain, test_db, monkeypatch):
    save_unsettled(test_db, batcher, 0)
    real_count = chain.get_transaction_count
    calls = []

    def latest_down_once(address, block="latest"):
        if block == "latest" and not calls:
            calls.append(1)
            raise ConnectionError("rpc down")
        return real_count(address, block)

    monkeypatch.setattr(chain, "get_transaction_count", latest_down_once)

    sent = await asyncio.wait_for(batcher.submit(intent()), timeout=5)

    assert sent.nonce == 1


async def test_startup_recovers_only_chains_with_unsettled_lp_txs(batcher, test_db, monkeypatch, settings):
    import src.clients.evm_batcher as module

    lp = settings.liquidity_provider_address.lower()
    rows = [(8453, lp, "sent"), (1, lp, "mined"), (999, "0xother", "sent")]
    for nonce, (chain_id, address, status) in enumerate(rows):
        test_db.execute(
            "INSERT INTO evm_transactions (chain_id, address, nonce, owner, tx_hash, raw, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'swap:a:lifi', '0xaa', '0x01', ?, 0, 0)",
            (chain_id, address, nonce, status),
        )
    asked, started = [], []

    class Started:
        def start(self):
            started.append(1)

    monkeypatch.setattr(module, "get_evm_batcher", lambda chain_id: asked.append(chain_id) or Started())

    await module.recover_evm_batchers()

    assert asked == [8453] and started == [1]


async def test_startup_with_nothing_unsettled_touches_no_rpc(monkeypatch):
    import src.clients.evm_batcher as module

    monkeypatch.setattr(module, "get_evm_batcher", lambda chain_id: (_ for _ in ()).throw(AssertionError))

    await module.recover_evm_batchers()


async def test_rounds_never_overlap(batcher, chain, test_db, monkeypatch):
    # Two rounds at once would each write over the versions the other recorded.
    await batcher.submit(intent())
    real_count = chain.get_transaction_count
    inside, most = [0], [0]

    def slow_count(address, block="latest"):
        inside[0] += 1
        most[0] = max(most[0], inside[0])
        import time as clock
        clock.sleep(0.05)
        inside[0] -= 1
        return real_count(address, block)

    monkeypatch.setattr(chain, "get_transaction_count", slow_count)

    await asyncio.gather(batcher.maintain(), batcher.maintain(), batcher.maintain())

    assert most[0] == 1


async def test_a_settled_tx_is_never_re_signed(batcher, chain, test_db):
    first = await batcher.submit(intent())
    chain.mine()
    await batcher.maintain()

    with pytest.raises(RuntimeError):
        batcher._record_version("0xnew", 0, "0x02")

    row = row_at(test_db, 0)
    assert (row["status"], row["tx_hash"], row["replaced"]) == ("mined", first.tx_hash, None)


async def test_one_failing_tx_leaves_the_rest_of_the_round(batcher, chain, test_db, monkeypatch):
    await asyncio.gather(*(batcher.submit(intent(owner=f"swap:{i}:lifi")) for i in range(3)))
    chain.pool.clear()
    chain.sent.clear()
    real = batcher.evm.rebroadcast

    def first_breaks(raw, on_signed):
        if nonce_of(raw) == 0:
            raise ConnectionError("rpc hiccup")
        return real(raw, on_signed)

    monkeypatch.setattr(batcher.evm, "rebroadcast", first_breaks)

    await batcher.maintain()

    assert [nonce_of(raw) for raw in chain.sent] == [1, 2]
