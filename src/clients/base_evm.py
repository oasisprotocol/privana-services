import asyncio
import logging
from typing import Optional

from eth_account import Account
from web3 import Web3

from src.core.abi import load_abi
from src.core.config import load_settings

logger = logging.getLogger(__name__)

ERC20_ABI = load_abi("ERC20")
TRANSFER_GAS_LIMIT = 100_000
APPROVE_GAS_LIMIT = 80_000

base_tx_lock = asyncio.Lock()


class BaseEvmClient:
    def __init__(self, rpc_url: str, secret_key: str) -> None:
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self._account = Account.from_key(secret_key)
        self.address = self._account.address

    def erc20_balance(self, token: str, owner: str) -> int:
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        return contract.functions.balanceOf(Web3.to_checksum_address(owner)).call()

    def transfer_erc20(self, token: str, to: str, amount: int) -> str:
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        fn = contract.functions.transfer(Web3.to_checksum_address(to), amount)
        return self._send(fn.build_transaction(self._tx_params(gas=TRANSFER_GAS_LIMIT)))

    def ensure_allowance(self, token: str, spender: str, amount: int) -> Optional[str]:
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        current = contract.functions.allowance(
            self.address, Web3.to_checksum_address(spender)
        ).call()
        if current >= amount:
            return None
        fn = contract.functions.approve(Web3.to_checksum_address(spender), amount)
        return self._send(fn.build_transaction(self._tx_params(gas=APPROVE_GAS_LIMIT)))

    def send_transaction_request(self, tx_request: dict) -> str:
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
            "chainId": self.w3.eth.chain_id,
        }
        return self._send(tx)

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
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        tx_hex = Web3.to_hex(tx_hash)
        if receipt.status != 1:
            raise RuntimeError(f"transaction reverted: {tx_hex}")
        return tx_hex


_client_instance: Optional[BaseEvmClient] = None


def get_base_evm_client() -> BaseEvmClient:
    global _client_instance
    if _client_instance is None:
        settings = load_settings()
        _client_instance = BaseEvmClient(
            settings.base_sepolia_rpc_url, settings.liquidity_provider_secret_key
        )
    return _client_instance
