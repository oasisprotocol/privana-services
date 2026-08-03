import logging
from typing import Optional

from eth_account import Account
from sapphirepy import sapphire
from web3 import Web3
from web3.middleware import SignAndSendRawMiddlewareBuilder

from src.core.config import load_settings

logger = logging.getLogger(__name__)

DEFAULT_GAS_LIMIT = 500_000


class SapphireClient:
    """Web3 client for Oasis Sapphire confidential EVM.

    Uses the official ``oasis-sapphire-py`` middleware: a signing middleware
    is added first so transactions get signed locally, then ``sapphire.wrap``
    layers calldata encryption on top. The chain treats encrypted calldata
    the same as plain calldata at the contract level, so callers just use
    the standard ``contract.functions.foo(...).transact(...)`` flow and the
    middleware stack handles encryption + signing transparently.
    """

    def __init__(self) -> None:
        settings = load_settings()
        self.rpc_url = settings.sapphire_rpc_url
        self.account = Account.from_key(settings.liquidity_provider_secret_key)
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        self.w3.middleware_onion.add(SignAndSendRawMiddlewareBuilder.build(self.account))
        self.w3 = sapphire.wrap(self.w3, self.account)
        self.w3.eth.default_account = self.account.address
        self.chain_id = self.w3.eth.chain_id
        if self.chain_id != settings.accounting_chain_id:
            raise RuntimeError(
                "ACCOUNTING_CHAIN_ID does not match SAPPHIRE_RPC_URL: "
                f"env={settings.accounting_chain_id} rpc={self.chain_id}. "
                "Either fix the env var or point SAPPHIRE_RPC_URL at the matching network."
            )

    def is_connected(self) -> bool:
        try:
            self.w3.eth.block_number
            return True
        except Exception:
            return False

    def simulate_contract_call(
        self,
        contract_address: str,
        abi: list,
        function_name: str,
        args: list,
    ) -> None:
        """Dry-run a call via ``eth_call`` and raise if it would revert.

        Balances on the accounting ledger are confidential, so the service
        cannot read a user's balance to pre-check it. A simulation gets the
        same answer from the contract itself without spending gas, and covers
        every revert cause at once (insufficient balance either side, a
        consumed nonce, a bad signature) rather than just the one we thought
        to check.

        No gas field is set: the Sapphire middleware re-encodes call params
        and chokes on an explicit hex gas value, and eth_call does not need
        one.
        """
        address = Web3.to_checksum_address(contract_address)
        contract = self.w3.eth.contract(address=address, abi=abi)
        contract.functions[function_name](*args).call({"from": self.account.address})

    def execute_contract_call(
        self,
        contract_address: str,
        abi: list,
        function_name: str,
        args: list,
        gas_limit: int = DEFAULT_GAS_LIMIT,
    ) -> str:
        address = Web3.to_checksum_address(contract_address)
        contract = self.w3.eth.contract(address=address, abi=abi)
        tx_hash = contract.functions[function_name](*args).transact({
            "from": self.account.address,
            "gas": gas_limit,
            "gasPrice": self.w3.eth.gas_price,
        })
        tx_hash_hex = tx_hash.hex()
        if not tx_hash_hex.startswith("0x"):
            tx_hash_hex = "0x" + tx_hash_hex
        logger.info(f"Tx sent (encrypted): {tx_hash_hex}")
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt["status"] != 1:
            raise RuntimeError(f"Transaction reverted: {tx_hash_hex}")
        logger.info(f"Tx confirmed: {tx_hash_hex}")
        return tx_hash_hex


_client_instance: Optional[SapphireClient] = None


def get_sapphire_client() -> SapphireClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = SapphireClient()
    return _client_instance
