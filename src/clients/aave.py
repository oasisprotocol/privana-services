import logging
from typing import Optional

from eth_account import Account
from web3 import Web3

from src.core.abi import load_abi
from src.core.config import load_settings

logger = logging.getLogger(__name__)

AAVE_POOL_ABI = load_abi("AaveV3Pool")
ERC20_ABI = load_abi("ERC20")

RAY = 10**27
RAY_TO_BPS = 10**23

DEFAULT_GAS_LIMIT = 500_000
AAVE_REFERRAL_CODE = 0


class AaveClient:
    """Aave V3 Pool client. Reads rates/balances and writes supply/withdraw.

    Reads are free (no signer). Writes use the LP EOA on Base Sepolia via
    standard web3 signing (non-confidential chain, no sapphirepy wrapper).

    Single class by design: each protocol adapter has exactly one strategy
    consuming it, and the strategy needs both read and write surfaces, so a
    split would only push the same coupling one layer down.
    """

    def __init__(self) -> None:
        settings = load_settings()
        self.w3 = Web3(Web3.HTTPProvider(settings.base_sepolia_rpc_url))
        self.pool_address = Web3.to_checksum_address(settings.aave_pool_address)
        self.pool = self.w3.eth.contract(
            address=self.pool_address,
            abi=AAVE_POOL_ABI,
        )
        self._account: Optional[Account] = None
        sk = settings.liquidity_provider_secret_key
        if sk:
            self._account = Account.from_key(sk)

    @property
    def account_address(self) -> str:
        if self._account is None:
            raise RuntimeError("AaveClient has no signer configured")
        return self._account.address

    def get_supply_apy_bps(self, asset_address: str) -> int:
        """Current supply rate for the given asset, in basis points.

        Aave V3 returns currentLiquidityRate as APR (linear annualized) in
        RAY units (1e27). We convert to bps via integer division and treat
        the result as APY. The APR→APY gap at typical supply rates (<10%)
        is under ~2 bps and is rounded away — intentional precision
        trade-off, since this value is used for display only and routing
        decisions don't depend on sub-bps accuracy.
        """
        reserve = self._get_reserve_data(asset_address)
        current_liquidity_rate = reserve[2]
        return current_liquidity_rate // RAY_TO_BPS

    def get_aToken_address(self, asset_address: str) -> str:
        """aToken contract address for the given underlying asset."""
        reserve = self._get_reserve_data(asset_address)
        return Web3.to_checksum_address(reserve[8])

    def get_aToken_balance(self, asset_address: str, holder: str) -> int:
        """Current aToken balance of `holder`, which equals underlying principal
        plus accrued yield at the moment of the call.
        """
        atoken = Web3.to_checksum_address(self.get_aToken_address(asset_address))
        holder = Web3.to_checksum_address(holder)
        contract = self.w3.eth.contract(address=atoken, abi=ERC20_ABI)
        return contract.functions.balanceOf(holder).call()

    def supply(self, asset_address: str, amount: int) -> str:
        """Supply `amount` of `asset_address` to the pool on behalf of the LP EOA.

        Caller is responsible for ensuring the pool has ERC20 allowance; see
        `approve_pool` for the approval helper.
        """
        asset = Web3.to_checksum_address(asset_address)
        return self._send_pool_tx("supply", [asset, amount, self.account_address, AAVE_REFERRAL_CODE])

    def withdraw(self, asset_address: str, amount: int, to: Optional[str] = None) -> str:
        """Withdraw `amount` of `asset_address` from the pool. Funds go to `to`
        (defaults to the LP EOA).
        """
        asset = Web3.to_checksum_address(asset_address)
        recipient = Web3.to_checksum_address(to) if to else self.account_address
        return self._send_pool_tx("withdraw", [asset, amount, recipient])

    def approve_pool(self, asset_address: str, amount: int) -> str:
        """Approve the pool to spend `amount` of `asset_address` from the LP EOA."""
        asset = Web3.to_checksum_address(asset_address)
        contract = self.w3.eth.contract(address=asset, abi=ERC20_ABI)
        return self._send_write_tx(asset, contract, "approve", [self.pool_address, amount])

    def transfer_erc20(self, asset_address: str, to_address: str, amount: int) -> str:
        """Plain ERC20.transfer from the LP EOA. Used to push redeemed
        Aave funds back to the privana deposit address on Base.
        """
        asset = Web3.to_checksum_address(asset_address)
        recipient = Web3.to_checksum_address(to_address)
        contract = self.w3.eth.contract(address=asset, abi=ERC20_ABI)
        return self._send_write_tx(asset, contract, "transfer", [recipient, amount])

    def get_allowance(self, asset_address: str, owner: Optional[str] = None) -> int:
        """Current ERC20 allowance the pool has to pull `asset_address` from `owner`
        (defaults to the LP EOA).
        """
        asset = Web3.to_checksum_address(asset_address)
        holder = Web3.to_checksum_address(owner) if owner else self.account_address
        contract = self.w3.eth.contract(address=asset, abi=ERC20_ABI)
        return contract.functions.allowance(holder, self.pool_address).call()

    def _get_reserve_data(self, asset_address: str) -> tuple:
        asset = Web3.to_checksum_address(asset_address)
        return self.pool.functions.getReserveData(asset).call()

    def _send_pool_tx(self, function_name: str, args: list) -> str:
        return self._send_write_tx(self.pool_address, self.pool, function_name, args)

    def _send_write_tx(self, to_address: str, contract, function_name: str, args: list) -> str:
        if self._account is None:
            raise RuntimeError("AaveClient has no signer configured")
        fn = getattr(contract.functions, function_name)(*args)
        nonce = self.w3.eth.get_transaction_count(self._account.address, "pending")
        tx = fn.build_transaction({
            "from": self._account.address,
            "nonce": nonce,
            "gas": DEFAULT_GAS_LIMIT,
            "gasPrice": self.w3.eth.gas_price,
            "chainId": self.w3.eth.chain_id,
        })
        signed = self._account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)
        logger.info("Aave tx sent: fn=%s to=%s hash=%s", function_name, to_address, tx_hash_hex)

        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt["status"] != 1:
            raise RuntimeError(f"Aave {function_name} tx reverted: {tx_hash_hex}")

        logger.info("Aave tx confirmed: fn=%s hash=%s", function_name, tx_hash_hex)
        return tx_hash_hex if tx_hash_hex.startswith("0x") else f"0x{tx_hash_hex}"


_client_instance: Optional[AaveClient] = None


def get_aave_client() -> AaveClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = AaveClient()
    return _client_instance
