import logging
from typing import Optional

from eth_account import Account
from web3 import Web3

from src.core.abi import load_abi
from src.core.config import load_settings

logger = logging.getLogger(__name__)

DEPOSIT_VAULT_ABI = load_abi("MidasDepositVault")
REDEMPTION_VAULT_ABI = load_abi("MidasRedemptionVault")
ORACLE_ABI = load_abi("ChronicleOracle")
ERC20_ABI = load_abi("ERC20")

DEFAULT_GAS_LIMIT = 500_000
ZERO_REFERRER_ID = b"\x00" * 32


class MidasClient:
    """Midas mTBILL client. Reads vault config + oracle, writes
    depositInstant / redeemInstant / approve / ERC20 transfer.

    Reads are free (no signer). Writes use the LP EOA on Base mainnet via
    standard web3 signing — same shape as AaveClient. The strategy holds the
    business logic; this class is a thin protocol wrapper.

    Four contracts are bound at construction so the strategy never reaches
    for raw addresses: issuance vault, redemption vault, mTBILL token,
    Chronicle MTBILL/USD oracle.
    """

    def __init__(self) -> None:
        settings = load_settings()
        self.w3 = Web3(Web3.HTTPProvider(settings.base_mainnet_rpc_url))

        self.issuance_vault_address = Web3.to_checksum_address(
            settings.midas_issuance_vault_address
        )
        self.redemption_vault_address = Web3.to_checksum_address(
            settings.midas_redemption_vault_address
        )
        self.mtbill_address = Web3.to_checksum_address(
            settings.midas_mtbill_token_address
        )
        self.oracle_address = Web3.to_checksum_address(settings.midas_oracle_address)

        self.issuance_vault = self.w3.eth.contract(
            address=self.issuance_vault_address, abi=DEPOSIT_VAULT_ABI,
        )
        self.redemption_vault = self.w3.eth.contract(
            address=self.redemption_vault_address, abi=REDEMPTION_VAULT_ABI,
        )
        self.mtbill = self.w3.eth.contract(
            address=self.mtbill_address, abi=ERC20_ABI,
        )
        self.oracle = self.w3.eth.contract(
            address=self.oracle_address, abi=ORACLE_ABI,
        )

        self._account: Optional[Account] = None
        sk = settings.liquidity_provider_secret_key
        if sk:
            self._account = Account.from_key(sk)

    @property
    def account_address(self) -> str:
        if self._account is None:
            raise RuntimeError("MidasClient has no signer configured")
        return self._account.address

    def get_oracle_answer(self) -> int:
        """Raw `latestAnswer` from Chronicle MTBILL/USD. Caller must normalize
        with `get_oracle_decimals()` because Chronicle feeds on Base are not
        guaranteed 18-decimal.
        """
        return self.oracle.functions.latestAnswer().call()

    def get_oracle_decimals(self) -> int:
        return self.oracle.functions.decimals().call()

    def get_oracle_round(self) -> tuple[int, int]:
        """Returns ``(answer, updated_at)`` from latestRoundData. Used by
        `is_healthy()` to refuse routing when the feed is stale relative to
        Chronicle's heartbeat.
        """
        round_id, answer, started_at, updated_at, answered_in_round = (
            self.oracle.functions.latestRoundData().call()
        )
        return int(answer), int(updated_at)

    def get_mtbill_balance(self, holder: str) -> int:
        holder = Web3.to_checksum_address(holder)
        return self.mtbill.functions.balanceOf(holder).call()

    def is_issuance_paused(self) -> bool:
        return bool(self.issuance_vault.functions.paused().call())

    def is_redemption_paused(self) -> bool:
        return bool(self.redemption_vault.functions.paused().call())

    def get_redemption_instant_fee_bps(self) -> int:
        """`instantFee` is stored in basis-points on the redemption vault.
        Strategy uses it to over-redeem just enough to cover the fee.
        """
        return int(self.redemption_vault.functions.instantFee().call())

    def get_issuance_min_amount(self) -> int:
        return int(self.issuance_vault.functions.minAmount().call())

    def get_redemption_min_amount(self) -> int:
        return int(self.redemption_vault.functions.minAmount().call())

    def get_allowance(self, asset_address: str, spender: str, owner: Optional[str] = None) -> int:
        asset = Web3.to_checksum_address(asset_address)
        spender_addr = Web3.to_checksum_address(spender)
        holder = Web3.to_checksum_address(owner) if owner else self.account_address
        contract = self.w3.eth.contract(address=asset, abi=ERC20_ABI)
        return contract.functions.allowance(holder, spender_addr).call()

    def approve(self, asset_address: str, spender: str, amount: int) -> str:
        asset = Web3.to_checksum_address(asset_address)
        spender_addr = Web3.to_checksum_address(spender)
        contract = self.w3.eth.contract(address=asset, abi=ERC20_ABI)
        return self._send_write_tx(asset, contract, "approve", [spender_addr, amount])

    def deposit_instant(
        self,
        token_in: str,
        amount: int,
        min_receive_amount: int,
        referrer_id: bytes = ZERO_REFERRER_ID,
    ) -> str:
        """Atomic mint via `IssuanceVault.depositInstant`. Pulls `amount` of
        `token_in` from the LP EOA and mints mTBILL to the LP EOA.

        The strategy is responsible for ensuring the vault has ERC20 allowance
        before calling — see `approve()`.
        """
        token = Web3.to_checksum_address(token_in)
        if len(referrer_id) != 32:
            raise ValueError("referrer_id must be exactly 32 bytes")
        return self._send_write_tx(
            self.issuance_vault_address,
            self.issuance_vault,
            "depositInstant",
            [token, amount, min_receive_amount, referrer_id],
        )

    def redeem_instant(
        self,
        token_out: str,
        amount_mtoken_in: int,
        min_receive_amount: int,
    ) -> str:
        """Atomic redeem via `RedemptionVault.redeemInstant`. Burns
        `amount_mtoken_in` of mTBILL from the LP EOA and sends `token_out`
        back to the LP EOA. Reverts if the daily instant limit is exhausted.
        """
        token = Web3.to_checksum_address(token_out)
        return self._send_write_tx(
            self.redemption_vault_address,
            self.redemption_vault,
            "redeemInstant",
            [token, amount_mtoken_in, min_receive_amount],
        )

    def transfer_erc20(self, asset_address: str, to_address: str, amount: int) -> str:
        """Plain ERC20.transfer from the LP EOA. Mirrors AaveClient's helper;
        the strategy uses it to forward redeemed USDC from the LP EOA to the
        flexvaults deposit address on Base.
        """
        asset = Web3.to_checksum_address(asset_address)
        recipient = Web3.to_checksum_address(to_address)
        contract = self.w3.eth.contract(address=asset, abi=ERC20_ABI)
        return self._send_write_tx(asset, contract, "transfer", [recipient, amount])

    def _send_write_tx(self, to_address: str, contract, function_name: str, args: list) -> str:
        if self._account is None:
            raise RuntimeError("MidasClient has no signer configured")
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
        logger.info("Midas tx sent: fn=%s to=%s hash=%s", function_name, to_address, tx_hash_hex)

        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt["status"] != 1:
            raise RuntimeError(f"Midas {function_name} tx reverted: {tx_hash_hex}")

        logger.info("Midas tx confirmed: fn=%s hash=%s", function_name, tx_hash_hex)
        return tx_hash_hex if tx_hash_hex.startswith("0x") else f"0x{tx_hash_hex}"


_client_instance: Optional[MidasClient] = None


def get_midas_client() -> MidasClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = MidasClient()
    return _client_instance


def reset_midas_client() -> None:
    """Test hook. Clears the module-level singleton so each test gets a
    fresh client.
    """
    global _client_instance
    _client_instance = None
