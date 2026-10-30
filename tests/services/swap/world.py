"""Where swap stages run in tests: the LP's real Sapphire sender, transfer
lane and withdraw lane on the shared fake Sapphire, and real EVM senders on
fake EVM chains whose wallet and LiFi diamond move balances as mined."""

import json
import threading
import time
from collections import defaultdict
from dataclasses import replace
from types import SimpleNamespace
from typing import Optional

import httpx
from eth_abi import encode
from eth_account import Account
from hexbytes import HexBytes
from web3 import Web3

import src.clients.evm_batcher as evm_batcher
import src.clients.privana_lanes as privana_lanes
import src.services.swap.internal_pipeline as internal_pipeline
import src.services.swap.lifi_pipeline as lifi_pipeline
import src.services.swap.stage as stage
from src.clients.base_evm import ERC20_ABI, EvmClient
from src.clients.evm_batcher import EvmBatcher
from src.clients.privana_lanes import WithdrawLane
from src.core.db import db_write, get_db
from src.core.eip712 import sign_transfer
from src.core.fee_policy import FeeDecision
from src.models.common import Balance, TokenInfo
from src.services.swap.executor import SwapExecutor
from src.services.swap.lifi_pipeline import SWAP_COMPLETED_DATA, SWAP_COMPLETED_TOPIC
from src.services.swap.worker import CHAIN_STAGES, STAGES
from tests.clients.fake_chain import (
    CHAIN_ID,
    SAPPHIRE_CHAIN_ID,
    FakeAccounting,
    FakeApi,
    FakeChain,
    decode,
    hash_of,
)

BASE = CHAIN_ID
ETHEREUM = 1
SWAP_MANAGER = "0x" + "5a" * 20
DIAMOND = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"
DEPOSIT_ADDRESS = "0x" + "dd" * 20
USDC = "0x" + "aa" * 32
WETH = "0x" + "bb" * 32
ETH_WETH = "0x" + "cc" * 32
TOKENS = {
    USDC: TokenInfo(token_id=USDC, token_type=1, token_type_name="ERC20", data="0x", chain_id=BASE,
                    token_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
    WETH: TokenInfo(token_id=WETH, token_type=1, token_type_name="ERC20", data="0x", chain_id=BASE,
                    token_address="0x4200000000000000000000000000000000000006"),
    ETH_WETH: TokenInfo(token_id=ETH_WETH, token_type=1, token_type_name="ERC20", data="0x", chain_id=ETHEREUM,
                        token_address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
}
USERS = [Account.from_key("0x" + f"{i + 1:02x}" * 32) for i in range(4)]
AMOUNT = 1_000_000

_ERC20 = Web3().eth.contract(abi=ERC20_ABI)


class Ledger(FakeAccounting):
    """Accounting on the fake Sapphire, plus the API's token metadata and the
    LP's Privana balances."""

    def __init__(self, settings) -> None:
        super().__init__(settings, SAPPHIRE_CHAIN_ID)
        # (address, token) -> Privana balance: a transfer that would overdraw reverts.
        self.balances = defaultdict(int)
        for user in USERS:
            for token in TOKENS:
                self.balances[(user.address.lower(), token)] = 10 * AMOUNT

    def _lp(self, token_id: str) -> tuple[str, str]:
        return self.settings.liquidity_provider_address.lower(), token_id

    def set_lp_balance(self, token_id: str, amount: int) -> None:
        self.balances[self._lp(token_id)] = amount

    async def get_token_info(self, token_id: str) -> TokenInfo:
        return TOKENS[token_id]

    async def get_lp_balance(self, token_id: str) -> Balance:
        return Balance(user_address=self.settings.liquidity_provider_address, token_id=token_id,
                       balance=str(self.balances[self._lp(token_id)]))


class Wallet:
    """The LP's wallet on an EVM chain and LiFi's diamond there, as a fake
    chain's contracts. A transfer moves the wallet's balance; the diamond runs
    the swap LiFi quoted for its calldata, paying out on the output's chain."""

    def __init__(self, world: "World") -> None:
        self.world = world
        self.balances: dict[str, int] = defaultdict(int)
        # (token, spender) -> what the wallet approved it to take.
        self.allowances: dict[tuple[str, str], int] = defaultdict(int)
        self.logs: list[dict] = []

    def run(self, to: str, data: str, dry: bool = False, sender: Optional[str] = None) -> bool:
        if to.lower() == DIAMOND.lower():
            return self._lifi(data, dry)
        function, args = _ERC20.decode_function_input(data)
        token = to.lower()
        if function.fn_name == "approve":
            if not dry:
                self.allowances[(token, args["spender"].lower())] = args["amount"]
            return True
        if self.balances[token] < args["amount"]:
            return False
        if not dry:
            self.balances[token] -= args["amount"]
        return True

    def _lifi(self, data: str, dry: bool) -> bool:
        quote = self.world.lifi.quotes[data]
        pull = (quote["from_token"], DIAMOND.lower())
        erc20 = int(quote["from_token"], 16) != 0
        if self.balances[quote["from_token"]] < quote["from_amount"] or self.world.lifi.reverts:
            return False
        # The diamond pulls an ERC-20 input with transferFrom.
        if erc20 and self.allowances[pull] < quote["from_amount"]:
            return False
        if dry:
            return True
        if erc20:
            self.allowances[pull] -= quote["from_amount"]
        self.balances[quote["from_token"]] -= quote["from_amount"]
        self.world.wallets[quote["to_chain"]].balances[quote["to_token"]] += quote["to_amount"]
        if quote["to_chain"] == quote["from_chain"]:
            self.logs = [{
                "address": DIAMOND, "topics": [SWAP_COMPLETED_TOPIC, HexBytes("01" * 32)],
                "data": HexBytes(encode(SWAP_COMPLETED_DATA, [
                    "privana-services", "", self.world.lp_address, quote["from_token"], quote["to_token"],
                    quote["from_amount"], quote["to_amount"],
                ])),
            }]
        return True


class EvmChain(FakeChain):
    """A fake EVM chain whose receipts carry the logs its contracts emitted."""

    def mine(self, count: int = 1, status: int = 1) -> None:
        for _ in range(count):
            raw = self.pool[self.mined_count]
            self.contracts.logs = []
            super().mine(1, status)
            self.receipts[hash_of(raw)]["logs"] = self.contracts.logs


class Lifi:
    """LiFi: quotes `to_amount` for any input, at the slippage asked, and
    reports a bridge DONE once its tx is mined, unless `status` says else."""

    def __init__(self, world: "World") -> None:
        self.world = world
        self.to_amount = 60_000
        self.reverts = False
        self.status = None
        self.quotes: dict[str, dict] = {}
        self.status_calls = 0

    async def get_execution_quote(self, from_chain_id, to_chain_id, from_token_address, to_token_address,
                                  from_amount, from_address, slippage_bps):
        data = "0x" + f"{len(self.quotes) + 1:064x}"
        self.quotes[data] = {
            "from_chain": from_chain_id, "from_token": from_token_address.lower(), "from_amount": int(from_amount),
            "to_chain": to_chain_id, "to_token": to_token_address.lower(), "to_amount": self.to_amount,
        }
        return {
            "transactionRequest": {"to": DIAMOND, "data": data, "value": "0x0", "gasLimit": hex(300_000),
                                   "chainId": from_chain_id},
            "estimate": {"approvalAddress": DIAMOND, "toAmount": str(self.to_amount),
                         "toAmountMin": str(self.to_amount * (10_000 - slippage_bps) // 10_000)},
        }

    async def get_status(self, tx_hash, from_chain_id, to_chain_id):
        self.status_calls += 1
        if self.status is not None:
            return self.status
        if tx_hash not in self.world.chains[from_chain_id].receipts:
            raise httpx.HTTPStatusError("not indexed", request=httpx.Request("GET", "https://li.quest"),
                                        response=httpx.Response(404))
        data = get_db().execute("SELECT data FROM evm_transactions WHERE tx_hash = ?", (tx_hash,)).fetchone()[0]
        quote = self.quotes[data]
        return {"status": "DONE", "substatus": "COMPLETED", "toAddress": self.world.lp_address,
                "receiving": {"chainId": quote["to_chain"], "amount": str(quote["to_amount"]),
                              "token": {"address": quote["to_token"]}}}


class PrivanaApi(FakeApi):
    """The Privana API as the LP's client: its withdrawals reach the LP's
    wallet at once unless `relay_paused`, and a deposit is credited once its
    tx is mined."""

    def __init__(self, world: "World") -> None:
        super().__init__(world.ledger, world.lp_address.lower())
        self.world = world
        self.relay_paused = False
        self.credits = True
        self.credited: set[str] = set()

    async def request_withdrawal(self, request):
        before = self.contracts.withdrawal_nonces[self.signer]
        try:
            return await super().request_withdrawal(request)
        finally:
            if self.contracts.withdrawal_nonces[self.signer] > before and not self.relay_paused:
                info = TOKENS[request.token_id]
                self.world.wallets[info.chain_id].balances[info.token_address.lower()] += request.amount

    async def get_deposit_address(self, request=None):
        return SimpleNamespace(deposit_address=DEPOSIT_ADDRESS)

    async def check_deposit(self, request):
        chain = self.world.chains[request.chain_id]
        receipt = chain.receipts.get(request.tx_hash)
        raw = next(r for r in chain.sent if hash_of(r) == request.tx_hash)
        _, transfer = _ERC20.decode_function_input(Web3.to_hex(decode(raw)["data"]))
        # Only a transfer of the reported amount to the deposit address is credited.
        to_us = (transfer["to"].lower(), transfer["amount"]) == (DEPOSIT_ADDRESS.lower(), request.amount)
        credited = self.credits and receipt is not None and receipt["status"] == 1 and to_us
        if credited and request.tx_hash not in self.credited:
            self.credited.add(request.tx_hash)
            token = next(t for t, info in TOKENS.items() if info.chain_id == request.chain_id
                         and info.token_address.lower() == Web3.to_checksum_address(decode(raw)["to"]).lower())
            self.contracts.balances[(self.signer, token)] += request.amount
        return SimpleNamespace(status="credited" if credited else "pending", detail=None)


class World:
    def __init__(self, settings, test_db, monkeypatch) -> None:
        self.db = test_db
        self.monkeypatch = monkeypatch
        self.config = replace(settings, accounting_chain_id=SAPPHIRE_CHAIN_ID, swap_manager_contract_address=SWAP_MANAGER,
                              fee_bps=10, lifi_execution_enabled=True)
        self.lp_address = self.config.liquidity_provider_address
        monkeypatch.setattr(evm_batcher, "RECEIPT_POLL_SEC", 0)
        monkeypatch.setattr(evm_batcher, "RECEIPT_TIMEOUT_SEC", 0.05)
        for module in (privana_lanes, stage, internal_pipeline, lifi_pipeline):
            monkeypatch.setattr(module, "load_settings", lambda: self.config)
        self.ledger = Ledger(self.config)
        self.sapphire = FakeChain(SAPPHIRE_CHAIN_ID)
        self.sapphire.contracts = self.ledger
        self.chains = {SAPPHIRE_CHAIN_ID: self.sapphire}
        self.wallets: dict[int, Wallet] = {}
        for chain_id in (BASE, ETHEREUM):
            self.wallets[chain_id] = Wallet(self)
            self.chains[chain_id] = EvmChain(chain_id)
            self.chains[chain_id].contracts = self.wallets[chain_id]
        for chain in self.chains.values():
            chain.auto_mine = True
        self.lifi = Lifi(self)
        self.api = PrivanaApi(self)
        self.restart()
        for module in (privana_lanes, internal_pipeline, lifi_pipeline):
            monkeypatch.setattr(module, "get_accounting_client", lambda: self.ledger)
        monkeypatch.setattr(lifi_pipeline, "get_evm_client", lambda chain_id: self.senders[chain_id].evm)
        monkeypatch.setattr(lifi_pipeline, "get_lifi_client", lambda: self.lifi)
        monkeypatch.setattr(internal_pipeline, "_sapphire_lp", lambda: self.senders[SAPPHIRE_CHAIN_ID].evm)
        monkeypatch.setattr(lifi_pipeline, "get_swap_lp_privana_client", self._client)
        # The internal price: LiFi's gross output for any input.
        self.internal_gross = 60_000
        self.quotes = SimpleNamespace(
            accounting=self.ledger, _enforce_max_swap_size=lambda route: None,
            _price_route=self._price_route,
        )
        monkeypatch.setattr(internal_pipeline, "get_quote_service", lambda: self.quotes)
        monkeypatch.setattr(internal_pipeline, "resolve_internal_fee",
                            lambda user, now: FeeDecision(fee_bps=self.config.fee_bps))

    async def _price_route(self, *_):
        return {"routes": [{"toAmount": str(self.internal_gross), "fromAmountUSD": "1"}]}

    async def _client(self):
        return self.api

    def restart(self) -> None:
        """New senders and withdraw lane: what a restart starts with."""
        key = self.config.liquidity_provider_secret_key
        self.senders = {}
        for chain_id, chain in self.chains.items():
            evm = EvmClient("http://localhost:1", key)
            evm.w3.eth = chain
            evm.calldata_public_key = chain.calldata_public_key
            evm.__dict__["_signed_calls"] = SimpleNamespace(eth=chain)
            if chain_id != SAPPHIRE_CHAIN_ID:
                wallet = self.wallets[chain_id]
                evm.balance_of = lambda token, owner, wallet=wallet: wallet.balances[token.lower()]
                evm.max_gas_cost = lambda gas: gas
            hook = privana_lanes.run_transfer_lanes if chain_id == SAPPHIRE_CHAIN_ID else None
            self.senders[chain_id] = EvmBatcher(evm, self.db, threading.Event(), hook)
        self.withdraw_lane = WithdrawLane(self.lp_address.lower(), key, self._client)

    async def schedule(self, venue: str, user=USERS[0], from_token=USDC, to_token=WETH, nonce=0, floor=55_000,
                       quote_id=None) -> str:
        """A swap scheduled through the API's executor, signed by `user`."""
        quote_id = quote_id or f"q-{user.address[-6:]}-{nonce}-{from_token[-4:]}{to_token[-4:]}"
        now = int(time.time())
        db_write(self.db, """INSERT INTO quotes (id, user_address, from_token_id, to_token_id, from_chain_id,
            to_chain_id, from_amount, to_amount_gross, to_amount_estimate, to_amount_min, route_tool,
            liquidity_provider, expires_at, created_at, venue, slippage_bps)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fly', ?, ?, ?, ?, 50)""", (
            quote_id, user.address.lower(), from_token, to_token, TOKENS[from_token].chain_id,
            TOKENS[to_token].chain_id, str(AMOUNT), "60000", "59940", str(floor), self.lp_address,
            now + 300, now, venue))
        signature = sign_transfer(user.key, SAPPHIRE_CHAIN_ID, self.config.accounting_contract_address,
                                  self.lp_address, from_token, AMOUNT, nonce)
        executor = SwapExecutor()
        executor.settings = self.config
        return (await executor.schedule_swap(quote_id, nonce, signature)).id

    async def stage_rounds(self, *names: str) -> None:
        for name, run_round, _ in STAGES:
            if not names or name in names:
                await run_round()
        for name, run_round, _ in CHAIN_STAGES:
            if not names or name in names:
                for chain_id in (BASE, ETHEREUM):
                    await run_round(chain_id)

    async def run(self, rounds: int = 1, senders=None) -> None:
        """Every stage, then every sender and the withdraw lane, `rounds` times."""
        for _ in range(rounds):
            await self.stage_rounds()
            for chain_id, sender in self.senders.items():
                if senders is None or chain_id in senders:
                    sender.run_round()
            await self.withdraw_lane.run_round()

    async def until(self, swap_id: str, step: str, rounds: int = 30, **kwargs) -> dict:
        """Run until the swap is at `step` (a status once it has settled)."""
        for _ in range(rounds):
            swap = self.swap(swap_id)
            if step in (swap["step"], swap["status"]):
                return swap
            await self.run(**kwargs)
        raise AssertionError(f"swap {swap_id} never reached {step}: {self.swap(swap_id)}")

    def swap(self, swap_id: str) -> dict:
        return dict(get_db().execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone())

    def age(self, swap_id: str, seconds: int) -> None:
        db_write(self.db, "UPDATE swaps SET updated_at = updated_at - ? WHERE id = ?", (seconds, swap_id))

    def evm_owners(self, swap_id: str) -> list[str]:
        """What each tx queued for the swap is for, oldest first."""
        return [row["owner"].rsplit(":", 1)[1] for row in get_db().execute(
            "SELECT owner FROM evm_transactions WHERE owner LIKE ? ORDER BY id", (f"swap:{swap_id}:%",))]

    def lane_rows(self, swap_id: str) -> dict[str, dict]:
        return {row["owner"].rsplit(":", 1)[1]: dict(row) for row in get_db().execute(
            "SELECT * FROM privana_transactions WHERE owner LIKE ?", (f"swap:{swap_id}:%",))}

    def paid(self, user) -> list[tuple[str, int]]:
        """What the LP's landed writes paid `user`: (token, amount) each."""
        rows = get_db().execute(
            "SELECT payload FROM privana_transactions WHERE status = 'landed' AND kind IN ('transfer', 'swap') "
            "AND lower(json_extract(payload, '$.to')) = ? ORDER BY id", (user.address.lower(),))
        return [(p["token"], p["amount"]) for p in (json.loads(row["payload"]) for row in rows)]
