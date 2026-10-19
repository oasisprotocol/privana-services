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
        self.reject_next = 0
        self.on_send = None

    def get_transaction_count(self, address, block="latest"):
        if block == "pending":
            return max([self.mined_count, *[n + 1 for n in self.pool]])
        return self.mined_count

    def get_block(self, _):
        return {"baseFeePerGas": 1_000}

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


async def test_a_cancelled_caller_does_not_stop_the_batcher(batcher, chain):
    waiting = asyncio.create_task(batcher.submit(intent(owner="swap:a:lifi")))
    await asyncio.sleep(0)
    waiting.cancel()

    sent = await asyncio.wait_for(batcher.submit(intent(owner="swap:b:lifi")), timeout=5)

    assert sent.nonce in (0, 1)


def test_each_chain_gets_its_own_batcher(monkeypatch):
    import src.clients.evm_batcher as module

    monkeypatch.setattr(module, "_batchers", {})
    monkeypatch.setattr(module, "get_evm_client", lambda chain_id: EvmClient("http://localhost:1", KEY))

    assert module.get_evm_batcher(1) is module.get_evm_batcher(1)
    assert module.get_evm_batcher(1) is not module.get_evm_batcher(8453)
