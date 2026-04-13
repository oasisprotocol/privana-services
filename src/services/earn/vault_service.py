import asyncio
import logging
from typing import Optional

from web3 import Web3

from src.clients.accounting import get_accounting_client
from src.clients.sapphire import get_sapphire_client
from src.core.config import load_settings
from src.core.validation import validate_address, validate_amount, validate_token_id

logger = logging.getLogger(__name__)

EARN_MANAGER_ABI = [
    {
        "inputs": [{"name": "poolId", "type": "bytes32"}],
        "name": "getPool",
        "outputs": [
            {
                "components": [
                    {"name": "tokenId", "type": "bytes32"},
                    {"name": "poolAddress", "type": "address"},
                    {"name": "totalShares", "type": "uint256"},
                    {"name": "totalAssets", "type": "uint256"},
                    {"name": "active", "type": "bool"},
                ],
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "user", "type": "address"},
            {"name": "poolId", "type": "bytes32"},
            {"name": "token", "type": "bytes"},
        ],
        "name": "getUserShares",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getPoolCount",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "", "type": "uint256"}],
        "name": "poolIds",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "poolId", "type": "bytes32"},
            {"name": "assets", "type": "uint256"},
        ],
        "name": "convertToShares",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "poolId", "type": "bytes32"},
            {"name": "shares", "type": "uint256"},
        ],
        "name": "convertToAssets",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "poolId", "type": "bytes32"},
            {"name": "user", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "nonce", "type": "uint256"},
            {"name": "signature", "type": "bytes"},
        ],
        "name": "deposit",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "poolId", "type": "bytes32"},
            {"name": "user", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "nonce", "type": "uint256"},
            {"name": "signature", "type": "bytes"},
        ],
        "name": "withdraw",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "poolId", "type": "bytes32"},
            {"name": "yieldAmount", "type": "uint256"},
        ],
        "name": "harvest",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


class VaultService:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.sapphire = get_sapphire_client()
        self.accounting = get_accounting_client()
        self.contract_address = Web3.to_checksum_address(
            self.settings.earn_manager_contract_address
        )
        self.contract = self.sapphire.w3.eth.contract(
            address=self.contract_address,
            abi=EARN_MANAGER_ABI,
        )

    def get_pool(self, pool_id: bytes) -> dict:
        pool = self.contract.functions.getPool(pool_id).call()
        return {
            "token_id": "0x" + pool[0].hex(),
            "pool_address": pool[1],
            "total_shares": pool[2],
            "total_assets": pool[3],
            "active": pool[4],
        }

    def list_pools(self) -> list[dict]:
        count = self.contract.functions.getPoolCount().call()
        pools = []
        for i in range(count):
            pool_id = self.contract.functions.poolIds(i).call()
            pool = self.get_pool(pool_id)
            pool["pool_id"] = "0x" + pool_id.hex()
            pools.append(pool)
        return pools

    def get_user_shares(self, user_address: str, pool_id: bytes) -> int:
        return self.contract.functions.getUserShares(
            Web3.to_checksum_address(user_address),
            pool_id,
            b"",
        ).call()

    def convert_to_shares(self, pool_id: bytes, assets: int) -> int:
        return self.contract.functions.convertToShares(pool_id, assets).call()

    def convert_to_assets(self, pool_id: bytes, shares: int) -> int:
        return self.contract.functions.convertToAssets(pool_id, shares).call()

    def get_user_balance(self, user_address: str, pool_id: bytes) -> dict:
        shares = self.get_user_shares(user_address, pool_id)
        underlying = self.convert_to_assets(pool_id, shares) if shares > 0 else 0
        pool = self.get_pool(pool_id)
        exchange_rate = str(pool["total_assets"] / pool["total_shares"]) if pool["total_shares"] > 0 else "1.0"
        return {
            "pool_id": "0x" + pool_id.hex(),
            "token_id": pool["token_id"],
            "shares": str(shares),
            "underlying_amount": str(underlying),
            "exchange_rate": exchange_rate,
        }


    async def get_deposit_quote(
        self,
        pool_id_hex: str,
        amount: str,
        user_address: str,
    ) -> dict:
        validate_address(user_address, "user_address")
        validate_amount(amount, "amount")

        pool_id = bytes.fromhex(pool_id_hex.removeprefix("0x"))
        pool = self.get_pool(pool_id)
        if pool["pool_address"] == "0x0000000000000000000000000000000000000000":
            raise ValueError("Pool not found")
        if not pool["active"]:
            raise ValueError("Pool is not active")

        shares_estimate = self.convert_to_shares(pool_id, int(amount))
        total_shares = pool["total_shares"]
        total_assets = pool["total_assets"]
        exchange_rate = str(total_assets / total_shares) if total_shares > 0 else "1.0"

        transfer_nonce = await self.accounting.get_transfer_nonce(user_address)

        return {
            "pool_id": pool_id_hex,
            "token_id": pool["token_id"],
            "amount": amount,
            "shares_estimate": str(shares_estimate),
            "exchange_rate": exchange_rate,
            "pool_address": pool["pool_address"],
            "transfer_nonce": transfer_nonce,
        }

    async def deposit(
        self,
        pool_id_hex: str,
        user_address: str,
        amount: str,
        nonce: int,
        signature: str,
    ) -> dict:
        validate_address(user_address, "user_address")
        validate_amount(amount, "amount")

        pool_id = bytes.fromhex(pool_id_hex.removeprefix("0x"))
        pool = self.get_pool(pool_id)
        if pool["pool_address"] == "0x0000000000000000000000000000000000000000":
            raise ValueError("Pool not found")
        if not pool["active"]:
            raise ValueError("Pool is not active")

        sig_bytes = bytes.fromhex(signature.removeprefix("0x"))

        shares_before = self.get_user_shares(user_address, pool_id)

        tx_hash = await asyncio.to_thread(
            self.sapphire.execute_contract_call,
            contract_address=self.contract_address,
            abi=EARN_MANAGER_ABI,
            function_name="deposit",
            args=[
                pool_id,
                Web3.to_checksum_address(user_address),
                int(amount),
                nonce,
                sig_bytes,
            ],
        )

        shares_after = self.get_user_shares(user_address, pool_id)
        shares_minted = shares_after - shares_before

        pool_after = self.get_pool(pool_id)
        exchange_rate = str(pool_after["total_assets"] / pool_after["total_shares"]) if pool_after["total_shares"] > 0 else "1.0"

        return {
            "pool_id": pool_id_hex,
            "amount": amount,
            "shares_minted": str(shares_minted),
            "exchange_rate": exchange_rate,
            "tx_hash": tx_hash,
            "status": "completed",
        }


_service_instance: Optional[VaultService] = None


def get_vault_service() -> VaultService:
    global _service_instance
    if _service_instance is None:
        _service_instance = VaultService()
    return _service_instance
