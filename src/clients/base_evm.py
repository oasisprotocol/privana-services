import logging
from typing import Optional

from eth_account import Account
from eth_typing import HexStr
from web3 import Web3
from web3.exceptions import TransactionNotFound
from web3.types import TxReceipt

from src.core.abi import load_abi
from src.core.config import load_settings

logger = logging.getLogger(__name__)

ERC20_ABI = load_abi("ERC20")

# Settings naming the RPC of each chain the LP account sends on.
RPC_SETTINGS = ("base_rpc_url", "ethereum_rpc_url", "hyperevm_rpc_url")

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

    def balance_of(self, token: Optional[str], owner: str) -> int:
        owner = Web3.to_checksum_address(owner)
        if is_native(token):
            return self.w3.eth.get_balance(owner)
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        return contract.functions.balanceOf(owner).call()

    def get_receipt(self, tx_hash: str) -> Optional[TxReceipt]:
        try:
            return self.w3.eth.get_transaction_receipt(HexStr(tx_hash))
        except TransactionNotFound:
            return None

    def fees(self) -> dict:
        # Type 2: the cap rides out base fee rises. A send pays the base fee
        # due plus the tip, never more than the cap.
        tip = self.w3.eth.max_priority_fee
        base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
        return {"maxFeePerGas": 2 * base_fee + tip, "maxPriorityFeePerGas": tip}

    def sign(self, tx: dict) -> tuple[str, str]:
        """`tx` signed by the account, as its hash and hex-encoded bytes."""
        signed = self._account.sign_transaction(tx)
        return Web3.to_hex(Web3.keccak(signed.raw_transaction)), Web3.to_hex(signed.raw_transaction)

    def max_gas_cost(self, gas: int) -> int:
        """`gas` at the fee cap a send sets now."""
        return gas * self.fees()["maxFeePerGas"]

_clients: dict[str, EvmClient] = {}
_chain_ids: dict[str, int] = {}


def get_evm_client(chain_id: int) -> EvmClient:
    """The LP client for `chain_id`, picked by asking each configured RPC
    which chain it serves, so a testnet RPC answers for its testnet chain and
    a misconfigured URL can never sign for the wrong one."""
    settings = load_settings()
    for rpc_url in (getattr(settings, setting) for setting in RPC_SETTINGS):
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
