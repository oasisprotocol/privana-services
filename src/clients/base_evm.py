import asyncio
import logging
from typing import Callable, Optional

from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_typing import HexStr
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TransactionNotFound
from web3.types import TxReceipt

from src.core.abi import load_abi
from src.core.config import load_settings

logger = logging.getLogger(__name__)

ERC20_ABI = load_abi("ERC20")
TRANSFER_GAS_LIMIT = 100_000
NATIVE_TRANSFER_GAS_LIMIT = 21_000
APPROVE_GAS_LIMIT = 80_000

# LiFi's address for a chain's native coin.
NATIVE_TOKEN = "0x0000000000000000000000000000000000000000"


# Called with a signed tx's hash, nonce and hex-encoded bytes, before broadcast.
OnSigned = Callable[[str, int, str], None]


class TransactionPendingError(RuntimeError):
    """The transaction may have been sent but its receipt is unknown, so it may
    still be mined. Callers that must not send twice keep `tx_hash`."""

    def __init__(self, tx_hash: str) -> None:
        super().__init__(f"transaction {tx_hash} may have been sent, outcome unknown")
        self.tx_hash = tx_hash


def is_native(token: Optional[str]) -> bool:
    """Accounting reports a native coin with no contract address."""
    return not token or int(token, 16) == 0


class EvmClient:
    """The LP account on one EVM chain. Native coins and ERC-20s go through
    the same calls, told apart by `is_native`."""

    def __init__(self, rpc_url: str, secret_key: str) -> None:
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self._account = Account.from_key(secret_key)
        self.address = self._account.address
        # Nonces are per chain, so each chain orders its own sends.
        self.tx_lock = asyncio.Lock()

    def balance_of(self, token: Optional[str], owner: str) -> int:
        owner = Web3.to_checksum_address(owner)
        if is_native(token):
            return self.w3.eth.get_balance(owner)
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        return contract.functions.balanceOf(owner).call()

    def transfer(
        self, token: Optional[str], to: str, amount: int, on_signed: Optional[OnSigned] = None
    ) -> str:
        if is_native(token):
            tx = self._tx_params(gas=NATIVE_TRANSFER_GAS_LIMIT)
            tx.update(to=Web3.to_checksum_address(to), value=amount)
            return self._send(tx, on_signed)
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        fn = contract.functions.transfer(Web3.to_checksum_address(to), amount)
        return self._send(fn.build_transaction(self._tx_params(gas=TRANSFER_GAS_LIMIT)), on_signed)

    def allowance(self, token: str, spender: str) -> int:
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        return contract.functions.allowance(
            self.address, Web3.to_checksum_address(spender)
        ).call()

    def ensure_allowance(
        self, token: Optional[str], spender: str, amount: int, on_signed: Optional[OnSigned] = None
    ) -> Optional[str]:
        # A native coin travels as the transaction's value; nothing to approve.
        if token is None or is_native(token):
            return None
        if self.allowance(token, spender) >= amount:
            return None
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        fn = contract.functions.approve(Web3.to_checksum_address(spender), amount)
        return self._send(fn.build_transaction(self._tx_params(gas=APPROVE_GAS_LIMIT)), on_signed)

    def send_transaction_request(
        self, tx_request: dict, on_signed: Optional[OnSigned] = None
    ) -> str:
        chain_id = self.w3.eth.chain_id
        # LiFi builds the calldata for one chain. Signing it for another would
        # send it to the same address there, with the same value attached.
        requested = tx_request.get("chainId")
        if requested is not None and int(requested) != chain_id:
            raise ValueError(
                f"transaction built for chain {requested} cannot be sent on chain {chain_id}"
            )
        tx = {
            "from": self.address,
            "to": Web3.to_checksum_address(tx_request["to"]),
            "data": tx_request["data"],
            "value": int(tx_request.get("value", "0x0"), 16),
            "gas": int(tx_request["gasLimit"], 16),
            "nonce": self.w3.eth.get_transaction_count(self.address, "pending"),
            "chainId": chain_id,
            # Priced now: LiFi's gasPrice is a legacy price from quote time.
            **self._fees(),
        }
        return self._send(tx, on_signed)

    def get_receipt(self, tx_hash: str) -> Optional[TxReceipt]:
        try:
            return self.w3.eth.get_transaction_receipt(HexStr(tx_hash))
        except TransactionNotFound:
            return None

    def rebroadcast(self, raw: str, on_signed: OnSigned) -> None:
        """Broadcast a signed tx again for nodes that dropped it. Once the base fee
        prices it out, a version re-signed at fees priced now goes out instead,
        handed to `on_signed` first. Versions share the nonce, so at most one is
        mined and none pays twice."""
        tx = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
        base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
        # Priced out: at this base fee, the cap no longer covers the tip.
        if tx["maxFeePerGas"] < base_fee + tx["maxPriorityFeePerGas"]:
            # Nodes take a replacement only with both fees raised by at least 10%.
            tip = max(self.w3.eth.max_priority_fee, tx["maxPriorityFeePerGas"] * 9 // 8 + 1)
            cap = max(2 * base_fee + tip, tx["maxFeePerGas"] * 9 // 8 + 1)
            if self.w3.eth.get_balance(self.address) >= tx["value"] + tx["gas"] * cap:
                del tx["v"], tx["r"], tx["s"]
                signed = self._account.sign_transaction(
                    {**tx, "maxFeePerGas": cap, "maxPriorityFeePerGas": tip}
                )
                tx_hash = Web3.to_hex(Web3.keccak(signed.raw_transaction))
                logger.info(
                    "Re-signed transaction at nonce %s as %s: max fee %s, tip %s",
                    tx["nonce"], tx_hash, cap, tip,
                )
                raw = Web3.to_hex(signed.raw_transaction)
                on_signed(tx_hash, tx["nonce"], raw)
            else:
                logger.warning(
                    "Transaction at nonce %s is priced out, and the balance cannot pay a replacement",
                    tx["nonce"],
                )
        tx_hash = Web3.to_hex(Web3.keccak(hexstr=raw))
        try:
            self.w3.eth.send_raw_transaction(HexStr(raw))
            logger.info("Rebroadcast transaction %s", tx_hash)
        except Exception as exc:
            # A node or block already holding the tx refuses it ("already known", "nonce too low").
            logger.info("Rebroadcast of %s refused (%s): %s", tx_hash, type(exc).__name__, exc)

    def _fees(self) -> dict:
        # Type 2: the cap rides out base fee rises. A send pays the base fee
        # due plus the tip, never more than the cap.
        tip = self.w3.eth.max_priority_fee
        base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
        return {"maxFeePerGas": 2 * base_fee + tip, "maxPriorityFeePerGas": tip}

    def max_gas_cost(self, gas: int) -> int:
        """`gas` at the fee cap a send sets now."""
        return gas * self._fees()["maxFeePerGas"]

    def _tx_params(self, gas: int) -> dict:
        return {
            "from": self.address,
            "nonce": self.w3.eth.get_transaction_count(self.address, "pending"),
            "gas": gas,
            "chainId": self.w3.eth.chain_id,
            **self._fees(),
        }

    def _send(self, tx: dict, on_signed: Optional[OnSigned] = None) -> str:
        signed = self._account.sign_transaction(tx)
        tx_hash = Web3.to_hex(Web3.keccak(signed.raw_transaction))
        if on_signed is not None:
            # Before broadcast, so a tx that fails to record is never sent.
            on_signed(tx_hash, tx["nonce"], Web3.to_hex(signed.raw_transaction))
        logger.info("Sending transaction %s via %s: %s", tx_hash, self.w3.provider.endpoint_uri, tx)
        try:
            tx_hash = Web3.to_hex(self.w3.eth.send_raw_transaction(signed.raw_transaction))
            logger.info("Transaction %s accepted by %s", tx_hash, self.w3.provider.endpoint_uri)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        except Exception as exc:
            # Even a send that raised may have reached a node: web3 retries one
            # whose response was lost, and the retry fails as "already known".
            self._log_send_failure(tx_hash, tx, exc)
            raise TransactionPendingError(tx_hash) from exc
        if receipt.status != 1:
            raise RuntimeError(f"transaction reverted: {tx_hash}")
        logger.info("Transaction %s succeeded in block %s", tx_hash, receipt.blockNumber)
        return tx_hash

    def _log_send_failure(self, tx_hex: str, tx: dict, exc: Exception) -> None:
        """Record what the chain says about a send whose outcome is unknown."""
        try:
            latest = self.w3.eth.get_transaction_count(self.address, "latest")
            pending = self.w3.eth.get_transaction_count(self.address, "pending")
            try:
                self.w3.eth.get_transaction(tx_hex)
                known = True
            except TransactionNotFound:
                known = False
            logger.error(
                "Transaction %s failed (%s) via %s: from=%s nonce=%s latest_nonce=%s pending_nonce=%s known_to_node=%s",
                tx_hex, type(exc).__name__, self.w3.provider.endpoint_uri, self.address, tx.get("nonce"), latest, pending, known,
            )
        except Exception as probe_exc:
            logger.error(
                "Transaction %s failed (%s) via %s; state probe also failed (%s)",
                tx_hex, type(exc).__name__, self.w3.provider.endpoint_uri, type(probe_exc).__name__,
            )


_clients: dict[str, EvmClient] = {}
_chain_ids: dict[str, int] = {}


def get_evm_client(chain_id: int) -> EvmClient:
    """The LP client for `chain_id`, picked by asking each configured RPC
    which chain it serves, so a testnet RPC answers for its testnet chain and
    a misconfigured URL can never sign for the wrong one."""
    settings = load_settings()
    for rpc_url in (settings.base_rpc_url, settings.ethereum_rpc_url, settings.hyperevm_rpc_url):
        if not rpc_url:
            continue
        if rpc_url not in _clients:
            _clients[rpc_url] = EvmClient(rpc_url, settings.liquidity_provider_secret_key)
        if rpc_url not in _chain_ids:
            try:
                _chain_ids[rpc_url] = _clients[rpc_url].w3.eth.chain_id
            except Exception as exc:
                # Type only: the error text can carry the RPC URL and its key.
                logger.warning("EVM RPC unreachable while resolving its chain id: %s", type(exc).__name__)
                continue
        if _chain_ids[rpc_url] == chain_id:
            return _clients[rpc_url]
    raise ValueError(f"No RPC is configured for chain {chain_id}")
