import logging
from functools import cached_property
from typing import Optional

from eth_account import Account
from eth_typing import HexStr
from hexbytes import HexBytes
from sapphirepy import sapphire
from sapphirepy.envelope import TransactionCipher
from web3 import Web3
from web3.exceptions import TransactionNotFound
from web3.types import RPCEndpoint, TxReceipt

from src.clients.sapphire import sapphire_http_provider
from src.core.abi import load_abi
from src.core.config import load_settings

logger = logging.getLogger(__name__)

ERC20_ABI = load_abi("ERC20")

# Settings naming the RPC of each chain the LP account sends on.
RPC_SETTINGS = ("base_rpc_url", "ethereum_rpc_url", "hyperevm_rpc_url")

# Sapphire localnet, mainnet and testnet. Sapphire has no base fee and keeps
# calldata private only when the sender encrypts it.
SAPPHIRE_CHAIN_IDS = (23293, 23294, 23295)

# LiFi's address for a chain's native coin.
NATIVE_TOKEN = "0x0000000000000000000000000000000000000000"


def is_native(token: Optional[str]) -> bool:
    """Accounting reports a native coin with no contract address."""
    return not token or int(token, 16) == 0


class EvmClient:
    """One account on one EVM chain. Native coins and ERC-20s go through
    the same calls, told apart by `is_native`. `headers` go to this RPC
    only, such as a Sapphire gateway's auth."""

    def __init__(self, rpc_url: str, secret_key: str, headers: Optional[dict] = None) -> None:
        self._rpc_url = rpc_url
        self._headers = headers or {}
        self.w3 = Web3(self._provider())
        self._account = Account.from_key(secret_key)
        self.address = self._account.address

    @cached_property
    def chain_id(self) -> int:
        return self.w3.eth.chain_id

    @property
    def sapphire(self) -> bool:
        return self.chain_id in SAPPHIRE_CHAIN_IDS

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
        if self.sapphire:
            return {"gasPrice": self.w3.eth.gas_price}
        # Type 2: the cap rides out base fee rises. A send pays the base fee
        # due plus the tip, never more than the cap.
        tip = self.w3.eth.max_priority_fee
        base_fee = self.w3.eth.get_block("latest")["baseFeePerGas"]
        return {"maxFeePerGas": 2 * base_fee + tip, "maxPriorityFeePerGas": tip}

    def sign(self, tx: dict) -> tuple[str, str]:
        """`tx` signed by the account, as its hash and hex-encoded bytes. On
        Sapphire its calldata is encrypted first, to the key the node serves
        now: a signed tx carries it as is."""
        if self.sapphire:
            key = self.calldata_public_key()
            cipher = TransactionCipher(peer_pubkey=key["key"], peer_epoch=key["epoch"])
            tx = {**tx, "data": Web3.to_hex(cipher.encrypt(HexBytes(tx["data"])))}
        signed = self._account.sign_transaction(tx)
        return Web3.to_hex(Web3.keccak(signed.raw_transaction)), Web3.to_hex(signed.raw_transaction)

    def calldata_public_key(self) -> dict:
        """Sapphire's current calldata key, as sapphirepy's middleware fetches
        it. It rotates every epoch, so it is fetched for each use."""
        return self.w3.manager.request_blocking(RPCEndpoint("oasis_callDataPublicKey"), [])

    def simulate(self, tx: dict) -> None:
        """Run `tx` as a call, raising what its send would. Sapphire answers
        a call as its sender only when the call is signed, so it goes through
        sapphirepy. That fails on the hex gas value web3 passes it, so the call
        runs at sapphirepy's own gas limit, above any limit sent here."""
        if not self.sapphire:
            self.w3.eth.call(tx)
            return
        self._signed_calls.eth.call({key: tx[key] for key in ("from", "to", "data", "value")})

    @cached_property
    def _signed_calls(self) -> Web3:
        return sapphire.wrap(Web3(self._provider()), self._account)

    def _provider(self) -> Web3.HTTPProvider:
        if self._headers:
            return sapphire_http_provider(self._rpc_url, self._headers)
        return Web3.HTTPProvider(self._rpc_url)

    def max_gas_cost(self, gas: int) -> int:
        """`gas` at the fee cap a send sets now."""
        fees = self.fees()
        return gas * (fees["gasPrice"] if self.sapphire else fees["maxFeePerGas"])


_clients: dict[str, EvmClient] = {}
_chain_ids: dict[str, int] = {}


def rpc_chain_id(setting: str) -> Optional[int]:
    """The chain the RPC named by `setting` serves, as it answers; None when
    it is not configured or not reachable yet."""
    settings = load_settings()
    rpc_url = getattr(settings, setting)
    if not rpc_url:
        return None
    if rpc_url not in _clients:
        _clients[rpc_url] = EvmClient(rpc_url, settings.liquidity_provider_secret_key)
    if rpc_url not in _chain_ids:
        try:
            _chain_ids[rpc_url] = _clients[rpc_url].w3.eth.chain_id
        except Exception as exc:
            # Type only: the error text can carry the RPC URL and its key.
            logger.warning("EVM RPC unreachable while resolving its chain id: %s", type(exc).__name__)
            return None
    return _chain_ids[rpc_url]


def get_evm_client(chain_id: int) -> EvmClient:
    """The LP client for `chain_id`, picked by asking each configured RPC
    which chain it serves, so a testnet RPC answers for its testnet chain and
    a misconfigured URL can never sign for the wrong one."""
    settings = load_settings()
    for setting in RPC_SETTINGS:
        if rpc_chain_id(setting) == chain_id:
            return _clients[getattr(settings, setting)]
    raise ValueError(f"No RPC is configured for chain {chain_id}")
