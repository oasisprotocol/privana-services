import asyncio
import logging
from typing import Optional

from eth_account import Account
from web3 import Web3
from web3.exceptions import TransactionNotFound

from src.core.abi import load_abi
from src.core.config import load_settings

logger = logging.getLogger(__name__)

ERC20_ABI = load_abi("ERC20")
TRANSFER_GAS_LIMIT = 100_000
NATIVE_TRANSFER_GAS_LIMIT = 21_000
APPROVE_GAS_LIMIT = 80_000

# LiFi's address for a chain's native coin.
NATIVE_TOKEN = "0x0000000000000000000000000000000000000000"


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

    def transfer(self, token: Optional[str], to: str, amount: int) -> str:
        if is_native(token):
            tx = self._tx_params(gas=NATIVE_TRANSFER_GAS_LIMIT)
            tx.update(to=Web3.to_checksum_address(to), value=amount)
            return self._send(tx)
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        fn = contract.functions.transfer(Web3.to_checksum_address(to), amount)
        return self._send(fn.build_transaction(self._tx_params(gas=TRANSFER_GAS_LIMIT)))

    def ensure_allowance(self, token: Optional[str], spender: str, amount: int) -> Optional[str]:
        # A native coin travels as the transaction's value; nothing to approve.
        if is_native(token):
            return None
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        current = contract.functions.allowance(
            self.address, Web3.to_checksum_address(spender)
        ).call()
        if current >= amount:
            return None
        fn = contract.functions.approve(Web3.to_checksum_address(spender), amount)
        return self._send(fn.build_transaction(self._tx_params(gas=APPROVE_GAS_LIMIT)))

    def send_transaction_request(self, tx_request: dict) -> str:
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
            "gasPrice": (
                int(tx_request["gasPrice"], 16)
                if "gasPrice" in tx_request
                else self.w3.eth.gas_price
            ),
            "nonce": self.w3.eth.get_transaction_count(self.address, "pending"),
            "chainId": chain_id,
        }
        return self._send(tx)

    def gas_cost(self, tx_hash: str) -> int:
        receipt = self.w3.eth.get_transaction_receipt(tx_hash)
        return receipt["gasUsed"] * receipt["effectiveGasPrice"]

    def _tx_params(self, gas: int) -> dict:
        return {
            "from": self.address,
            "nonce": self.w3.eth.get_transaction_count(self.address, "pending"),
            "gas": gas,
            "gasPrice": self.w3.eth.gas_price,
            "chainId": self.w3.eth.chain_id,
        }

    def _send(self, tx: dict) -> str:
        signed = self._account.sign_transaction(tx)
        tx_hash = Web3.to_hex(Web3.keccak(signed.raw_transaction))
        logger.info("Sending transaction %s via %s: %s", tx_hash, self.w3.provider.endpoint_uri, tx)
        try:
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info("Transaction %s accepted by %s", tx_hash, self.w3.provider.endpoint_uri)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        except Exception as exc:
            self._log_send_failure(tx_hash, tx, exc)
            raise
        tx_hex = Web3.to_hex(tx_hash)
        if receipt.status != 1:
            raise RuntimeError(f"transaction reverted: {tx_hex}")
        logger.info("Transaction %s succeeded in block %s", tx_hex, receipt.blockNumber)
        return tx_hex

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
