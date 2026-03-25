import logging
from typing import Optional

from eth_account import Account
from web3 import Web3

from src.config import load_settings

logger = logging.getLogger(__name__)

SWAP_MANAGER_ABI = [
    {
        "inputs": [
            {"name": "user", "type": "address"},
            {"name": "inputTokenId", "type": "bytes32"},
            {"name": "inputAmount", "type": "uint256"},
            {"name": "inputNonce", "type": "uint256"},
            {"name": "inputSignature", "type": "bytes"},
            {"name": "outputTokenId", "type": "bytes32"},
            {"name": "outputAmount", "type": "uint256"},
            {"name": "outputNonce", "type": "uint256"},
            {"name": "outputSignature", "type": "bytes"},
        ],
        "name": "swap",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

SWAP_GAS_LIMIT = 500_000


class SapphireClient:
    def __init__(self) -> None:
        settings = load_settings()
        self.w3 = Web3(Web3.HTTPProvider(settings.sapphire_rpc_url))
        self.account = Account.from_key(settings.liquidity_provider_private_key)
        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(settings.liq_manager_contract_address),
            abi=SWAP_MANAGER_ABI,
        )
        self.chain_id = self.w3.eth.chain_id

    def is_connected(self) -> bool:
        try:
            self.w3.eth.block_number
            return True
        except Exception:
            return False

    def execute_swap(
        self,
        user: str,
        input_token_id: bytes,
        input_amount: int,
        input_nonce: int,
        input_signature: bytes,
        output_token_id: bytes,
        output_amount: int,
        output_nonce: int,
        output_signature: bytes,
    ) -> str:
        tx_data = self.contract.functions.swap(
            Web3.to_checksum_address(user),
            input_token_id,
            input_amount,
            input_nonce,
            input_signature,
            output_token_id,
            output_amount,
            output_nonce,
            output_signature,
        ).build_transaction(
            {
                "chainId": self.chain_id,
                "gas": SWAP_GAS_LIMIT,
                "gasPrice": self.w3.eth.gas_price,
                "nonce": self.w3.eth.get_transaction_count(self.account.address),
            }
        )

        signed_tx = self.account.sign_transaction(tx_data)
        tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)

        logger.info(f"Swap tx sent: {tx_hash.hex()}")

        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)

        if receipt["status"] != 1:
            raise RuntimeError(f"Swap transaction reverted: 0x{tx_hash.hex()}")

        logger.info(f"Swap tx confirmed: 0x{tx_hash.hex()}")
        return f"0x{tx_hash.hex()}"


_client_instance: Optional[SapphireClient] = None


def get_sapphire_client() -> SapphireClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = SapphireClient()
    return _client_instance
