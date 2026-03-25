import logging
from typing import Optional

from eth_account import Account
from sapphirepy.wrapper import send_encrypted_sapphire_tx
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
        self.rpc_url = settings.sapphire_rpc_url
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        self.account = Account.from_key(settings.liquidity_provider_private_key)
        self.private_key = settings.liquidity_provider_private_key.removeprefix("0x")
        self.contract_address = Web3.to_checksum_address(settings.swap_manager_contract_address)
        self.contract = self.w3.eth.contract(
            address=self.contract_address,
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
        calldata = self.contract.encode_abi(
            "swap",
            args=[
                Web3.to_checksum_address(user),
                input_token_id,
                input_amount,
                input_nonce,
                input_signature,
                output_token_id,
                output_amount,
                output_nonce,
                output_signature,
            ],
        )

        gas_price_gwei = self.w3.eth.gas_price // 10**9
        nonce = self.w3.eth.get_transaction_count(self.account.address)

        result_code, result_str = send_encrypted_sapphire_tx(
            pk=self.private_key,
            sender=self.account.address,
            recipient=self.contract_address,
            rpc_url=self.rpc_url,
            eth_amount=0,
            gas_limit=SWAP_GAS_LIMIT,
            data=calldata,
            gas_cost_gwei=max(int(gas_price_gwei), 100),
            nonce=nonce,
        )

        if result_code != 0:
            raise RuntimeError(f"Sapphire encrypted tx failed: {result_str}")

        tx_hash = result_str
        logger.info(f"Swap tx sent (encrypted): {tx_hash}")

        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)

        if receipt["status"] != 1:
            raise RuntimeError(f"Swap transaction reverted: {tx_hash}")

        logger.info(f"Swap tx confirmed: {tx_hash}")
        return tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"


_client_instance: Optional[SapphireClient] = None


def get_sapphire_client() -> SapphireClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = SapphireClient()
    return _client_instance
