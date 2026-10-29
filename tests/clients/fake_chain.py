import threading
from collections import defaultdict
from types import SimpleNamespace
from typing import Optional

import cbor2
import rlp
from eth_account import Account
from eth_account._utils.legacy_transactions import Transaction
from eth_account.messages import encode_typed_data
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from nacl.public import PrivateKey, PublicKey
from privana.client.errors import AccountingApiError
from privana.types import TransactionSubmissionResponse
from sapphirepy.deoxysii import TAG_SIZE, DeoxysII
from sapphirepy.envelope import _derive_shared_secret
from web3 import Web3
from web3.exceptions import ContractCustomError, ContractLogicError, TransactionNotFound

from src.clients.base_evm import SAPPHIRE_CHAIN_IDS, EvmClient
from src.clients.evm_batcher import EvmBatcher
from src.clients.privana_lanes import run_transfer_lanes
from src.core.abi import load_abi
from src.core.eip712 import (
    ACCOUNTING_WITHDRAW_TYPES,
    _salted_domain,
    recover_transfer_signer,
)

CHAIN_ID = 8453
SAPPHIRE_CHAIN_ID = 23295
BLOCK_TIME_ZERO = 1_700_000_000

_ACCOUNTING = Web3().eth.contract(abi=load_abi("Accounting"))
_SWAP_MANAGER = Web3().eth.contract(abi=load_abi("SwapManager"))


class Unreachable(ConnectionError):
    """The RPC did not answer."""


class FakeChain:
    """A node that takes txs and mines them only when told to, or on its own
    once `auto_mine` is set. On Sapphire it reads calldata only as an envelope
    encrypted to the key it serves. Calls to `contracts` run when mined and
    revert as the contracts would."""

    def __init__(self, chain_id: int = CHAIN_ID) -> None:
        self.mined_count = 0
        self.pool: dict[int, str] = {}
        self.sent: list[str] = []
        self.receipts: dict[str, dict] = {}
        self.chain_id = chain_id
        self.max_priority_fee = 100
        self.base_fee = 1_000
        self.gas_price = 100
        self.auto_mine = False
        self.reverting_calls: set[str] = set()
        self.refuse: list[Exception] = []
        self.on_send = None
        # Called with each block's number once mined: one tx per block.
        self.on_mine = None
        self.contracts = None
        self.epoch = 1
        self._calldata_key = PrivateKey.generate()

    def get_transaction_count(self, address, block="latest"):
        if block == "pending":
            return max([self.mined_count, *[n + 1 for n in self.pool]])
        return self.mined_count

    def get_block(self, block):
        number = self.mined_count if block == "latest" else block
        return {"baseFeePerGas": self.base_fee, "timestamp": BLOCK_TIME_ZERO + number}

    def calldata_public_key(self):
        return {"key": "0x" + self._calldata_key.public_key.encode().hex(), "epoch": self.epoch,
                "checksum": "0x", "signature": "0x"}

    def envelope(self, data: str) -> dict:
        """The body of the envelope `data` must be on Sapphire."""
        envelope = cbor2.loads(HexBytes(data))
        assert envelope["format"] == 1
        return envelope["body"]

    def plaintext(self, data: str) -> str:
        if self.chain_id not in SAPPHIRE_CHAIN_IDS:
            return data
        body = self.envelope(data)
        cipher = DeoxysII(_derive_shared_secret(PublicKey(body["pk"]), self._calldata_key))
        plain = bytearray(len(body["data"]) - TAG_SIZE)
        assert cipher.decrypt(nonce=body["nonce"], dst=plain, ad=None, ciphertext=body["data"])
        return Web3.to_hex(cbor2.loads(bytes(plain))["body"])

    def call(self, tx, *_):
        if tx["data"] == "0xdown":
            raise Unreachable("rpc timed out")
        if tx["data"] in self.reverting_calls:
            raise ContractLogicError("execution reverted")
        if self.contracts is not None and not self.contracts.run(tx["to"], tx["data"], dry=True):
            # As Sapphire answers a contract's own error: its code, no "revert" text.
            raise ContractCustomError("0x39996567", data="0x39996567")
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
            tx = decode(raw)
            to = Web3.to_checksum_address(tx["to"])
            outcome = status
            if outcome == 1 and self.contracts is not None:
                outcome = int(self.contracts.run(to, self.plaintext(Web3.to_hex(tx["data"]))))
            self.mined_count += 1
            self.receipts[hash_of(raw)] = {"status": outcome, "logs": [], "blockNumber": self.mined_count, "to": to}
            if self.on_mine is not None:
                self.on_mine(self.mined_count)

    def get_transaction(self, tx_hash):
        for raw in self.pool.values():
            if hash_of(raw) == tx_hash:
                return decode(raw)
        raise TransactionNotFound(tx_hash)

    def get_transaction_receipt(self, tx_hash):
        if self.auto_mine:
            while self.mined_count in self.pool:
                self.mine()
        if tx_hash not in self.receipts:
            raise TransactionNotFound(tx_hash)
        return self.receipts[tx_hash]


class FakeAccounting:
    """Accounting and SwapManager, as far as transfers go: each
    signed transfer runs only at its signer's current nonce, and a call runs
    all of its transfers or none. Also the nonce readers of AccountingClient."""

    def __init__(self, settings, chain_id: int) -> None:
        self.settings = settings
        self.chain_id = chain_id
        self.transfer_nonces: dict[str, int] = defaultdict(int)
        self.withdrawal_nonces: dict[str, int] = defaultdict(int)
        self.reverting_recipients: set[str] = set()
        # Recipients whose transfer passes a simulation but reverts once mined.
        self.reverting_when_mined: set[str] = set()
        # (signer, to, nonce) of every transfer that ran.
        self.landed: list[tuple[str, str, int]] = []
        # Balances by (account, token id). Once set, a transfer must fit.
        self.balances: Optional[dict[tuple[str, str], int]] = None

    def transfer_nonce(self, address: str) -> int:
        return self.transfer_nonces[address.lower()]

    def withdrawal_nonce(self, address: str) -> int:
        return self.withdrawal_nonces[address.lower()]

    def run(self, to: str, data: str, dry: bool = False) -> bool:
        nonces, landed, moved = {}, [], defaultdict(int)
        for leg_to, token, amount, nonce, signature in self._transfers(to, data):
            signer = recover_transfer_signer(self.chain_id, self.settings.accounting_contract_address, leg_to,
                                             token, amount, nonce, signature).lower()
            if (nonce != nonces.get(signer, self.transfer_nonces[signer]) or leg_to.lower() in self.reverting_recipients
                    or not dry and leg_to.lower() in self.reverting_when_mined):
                return False
            nonces[signer] = nonce + 1
            landed.append((signer, leg_to.lower(), nonce))
            moved[(signer, token)] -= amount
            moved[(leg_to.lower(), token)] += amount
        if self.balances is not None and any(
                self.balances.get(key, 0) + change < 0 for key, change in moved.items()):
            return False
        if not dry:
            self.transfer_nonces.update(nonces)
            self.landed.extend(landed)
            if self.balances is not None:
                for key, change in moved.items():
                    self.balances[key] = self.balances.get(key, 0) + change
        return True

    def _transfers(self, to: str, data: str) -> list[tuple]:
        settings = self.settings
        if to.lower() == settings.accounting_contract_address.lower():
            _, a = _ACCOUNTING.decode_function_input(data)
            return [(a["toAddress"], _hex(a["tokenId"]), a["amount"], a["nonce"], a["signature"])]
        assert to.lower() == settings.swap_manager_contract_address.lower(), f"no contract at {to}"
        _, a = _SWAP_MANAGER.decode_function_input(data)
        return [
            (settings.liquidity_provider_address, _hex(a["inputTokenId"]), a["inputAmount"], a["inputNonce"],
             a["inputSignature"]),
            (a["user"], _hex(a["outputTokenId"]), a["outputAmount"], a["outputNonce"], a["outputSignature"]),
        ]


def _hex(value: bytes) -> str:
    return "0x" + value.hex()


def decode(raw: str) -> dict:
    """A signed tx's fields, typed or legacy."""
    data = HexBytes(raw)
    if data[0] > 0x7F:
        return rlp.decode(bytes(data), Transaction).as_dict()
    return TypedTransaction.from_bytes(data).as_dict()


def nonce_of(raw: str) -> int:
    return decode(raw)["nonce"]


def hash_of(raw: str) -> str:
    return Web3.to_hex(Web3.keccak(hexstr=raw))


def sapphire_sender(chain: FakeChain, db, secret_key: str, signed_calls=None) -> EvmBatcher:
    """The batcher of `secret_key`'s account on a Sapphire `chain`, whose
    signed calls go to `signed_calls` when given."""
    evm = EvmClient("http://localhost:1", secret_key)
    evm.w3.eth = chain
    evm.calldata_public_key = chain.calldata_public_key
    evm.__dict__["_signed_calls"] = SimpleNamespace(eth=signed_calls or chain)
    return EvmBatcher(evm, db, threading.Event(), run_transfer_lanes)


def check_withdrawal_signature(settings, signer: str, request) -> None:
    """Refuse a withdrawal not signed by `signer` for its own token, amount
    and nonce, as the API does."""
    signable = encode_typed_data(
        domain_data=_salted_domain("AccountingModule", settings.accounting_chain_id,
                                   settings.accounting_contract_address),
        message_types=ACCOUNTING_WITHDRAW_TYPES,
        message_data={"tokenId": HexBytes(request.token_id), "amount": request.amount, "nonce": request.nonce},
    )
    if Account.recover_message(signable, signature=request.signature).lower() != signer.lower():
        raise AccountingApiError("API request failed: 400 Bad Request", 400, "invalid signature")


class FakeApi:
    """The Privana API's withdraw endpoint. Each request takes the next of
    `replies`, (whether the withdrawal lands, what the API answers)."""

    def __init__(self, contracts, signer):
        self.contracts = contracts
        self.signer = signer
        self.requests = []
        self.replies = []
        self.on_request = None

    async def request_withdrawal(self, request):
        self.requests.append(request)
        check_withdrawal_signature(self.contracts.settings, self.signer, request)
        if self.on_request is not None:
            self.on_request(request)
        lands, reply = self.replies.pop(0) if self.replies else (True, None)
        if request.nonce != self.contracts.withdrawal_nonces[self.signer]:
            lands, reply = False, AccountingApiError("API request failed: 400 Bad Request", 400, "invalid nonce")
        balances = self.contracts.balances
        if lands and balances is not None and balances.get((self.signer, request.token_id), 0) < request.amount:
            lands, reply = False, AccountingApiError("API request failed: 422", 422, "InsufficientBalance")
        if lands:
            self.contracts.withdrawal_nonces[self.signer] += 1
            if balances is not None:
                balances[(self.signer, request.token_id)] -= request.amount
        if isinstance(reply, Exception):
            raise reply
        return reply or TransactionSubmissionResponse(submission_id="s", status="submitted")
