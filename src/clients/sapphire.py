import ctypes
import logging
from typing import Optional

from eth_account import Account
from web3 import Web3

from src.core.config import load_settings

logger = logging.getLogger(__name__)

DEFAULT_GAS_LIMIT = 500_000


def _patch_sapphirepy_argtypes() -> None:
    """Sapphirepy <= 0.x ships a wrapper with truncated `argtypes` (7 fields),
    while the underlying C function takes 9 (pk, sender, recipient, rpc_url,
    eth_amount, gas_limit, data, gas_cost_gwei, nonce). With the short list,
    ctypes silently passes garbage for the trailing args, which manifests as
    a Sapphire runtime "invalid nonce" rejection. Append the missing two
    `c_int` types so the binary reads gas_cost_gwei and nonce correctly.
    Idempotent: skips when argtypes already match.
    """
    try:
        from sapphirepy.wrapper import lib
    except ImportError:
        return
    expected = 9
    current = list(lib.SendETHTransaction.argtypes or [])
    if len(current) >= expected:
        return
    missing = expected - len(current)
    lib.SendETHTransaction.argtypes = current + [ctypes.c_int] * missing
    logger.warning(
        "Patched sapphirepy.wrapper argtypes (was %d fields, now %d). "
        "Remove this shim once upstream sapphirepy ships the fix.",
        len(current), expected,
    )


_patch_sapphirepy_argtypes()


def _send_encrypted_tx(pk, sender, recipient, rpc_url, gas_limit, calldata, gas_gwei, nonce):
    from sapphirepy.wrapper import send_encrypted_sapphire_tx
    result_code, result_str = send_encrypted_sapphire_tx(
        pk=pk,
        sender=sender,
        recipient=recipient,
        rpc_url=rpc_url,
        eth_amount=0,
        gas_limit=gas_limit,
        data=calldata,
        gas_cost_gwei=gas_gwei,
        nonce=nonce,
    )
    if result_code != 0 or not result_str:
        raise RuntimeError(f"Encrypted tx failed: sapphirepy returned code={result_code}")
    return result_str


class SapphireClient:
    def __init__(self) -> None:
        settings = load_settings()
        self.rpc_url = settings.sapphire_rpc_url
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        self.account = Account.from_key(settings.liquidity_provider_private_key)
        self.private_key = settings.liquidity_provider_private_key.removeprefix("0x")
        self.chain_id = self.w3.eth.chain_id

    def is_connected(self) -> bool:
        try:
            self.w3.eth.block_number
            return True
        except Exception:
            return False

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
        calldata = contract.encode_abi(function_name, args=args)

        gas_price = self.w3.eth.gas_price
        gas_gwei = max(int(gas_price // 10**9), 100)
        nonce = self.w3.eth.get_transaction_count(self.account.address)

        tx_hash_hex = _send_encrypted_tx(
            self.private_key, self.account.address, address,
            self.rpc_url, gas_limit, calldata, gas_gwei, nonce,
        )

        logger.info(f"Tx sent (encrypted): {tx_hash_hex}")

        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash_hex)

        if receipt["status"] != 1:
            raise RuntimeError(f"Transaction reverted: {tx_hash_hex}")

        logger.info(f"Tx confirmed: {tx_hash_hex}")
        return tx_hash_hex if tx_hash_hex.startswith("0x") else f"0x{tx_hash_hex}"


_client_instance: Optional[SapphireClient] = None


def get_sapphire_client() -> SapphireClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = SapphireClient()
    return _client_instance
