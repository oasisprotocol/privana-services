import logging
from typing import Optional

from web3 import Web3

from src.clients.sapphire import get_sapphire_client
from src.core.config import load_settings

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


_service_instance: Optional[VaultService] = None


def get_vault_service() -> VaultService:
    global _service_instance
    if _service_instance is None:
        _service_instance = VaultService()
    return _service_instance
