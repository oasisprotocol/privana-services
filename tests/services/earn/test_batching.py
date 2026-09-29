"""End to end: the worker and the idle deployer running side by side against
fake pools, the way they run in production."""
import asyncio
import random
import time
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.db import db_write, get_db
from src.services.earn.idle_deployer import IdleDeployer
from src.services.earn.registry import StrategyRegistry
from src.services.earn.strategies.base import BaseStrategy
from src.services.earn.worker import EarnWorker
from tests.services.earn.test_vault_service import POOL_ADDRESS, USDC_TOKEN_ID, _make_service

POOLS = ("0x" + "a1" * 32, "0x" + "b2" * 32)
BRIDGE_SEC = 0.02


class FakeStrategy(BaseStrategy):
    """A pool account and a protocol position, with bridges that take time.

    A deploy debits the pool account as soon as it starts and credits the
    position when it lands; a reclaim is the reverse, so either one leaves
    money visible on neither side while it runs.
    """

    def __init__(self) -> None:
        self.pool_balance = 0
        self.deployed = 0
        self.lowest_balance = 0
        self.moves: list[int] = []
        self.reclaiming = False

    @property
    def name(self) -> str:
        return "fake"

    async def get_apy_bps(self) -> int:
        return 500

    async def deposit_to_earn(self, amount: int) -> None:
        self.pool_balance -= amount
        self.lowest_balance = min(self.lowest_balance, self.pool_balance)
        await asyncio.sleep(BRIDGE_SEC)
        self.deployed += amount
        self.moves.append(amount)

    async def withdraw_from_earn(self, amount: int) -> None:
        self.reclaiming = True
        self.deployed -= amount
        assert self.deployed >= 0
        await asyncio.sleep(BRIDGE_SEC)
        self.pool_balance += amount
        self.moves.append(-amount)
        self.reclaiming = False

    async def total_assets(self) -> int:
        return self.deployed

    async def idle_assets(self) -> int:
        return self.pool_balance

    async def is_healthy(self) -> bool:
        return True


def _schedule(tx_id, operation, pool, user, amount, created):
    db_write(
        get_db(),
        """INSERT INTO earn_transactions
           (id, operation, pool_id, user_address, token_id, amount, signer_address,
            nonce, signature, input_nonce, input_signature, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0, ?, 'scheduled', ?, ?)""",
        (tx_id, operation, pool, user, USDC_TOKEN_ID, str(amount), user,
         "0x" + "aa" * 65, "0x" + "aa" * 65, created, created),
    )


def _service(strategies):
    registry = StrategyRegistry()
    for pool, strategy in strategies.items():
        registry.register(pool, strategy)
    service, contract, _, _ = _make_service(registry=registry)
    contract.functions.pools.return_value.call.return_value = (
        bytes.fromhex(USDC_TOKEN_ID[2:]), POOL_ADDRESS, 1000, 1000, True,
    )
    service.settings = replace(service.settings, earn_buffer_min=300, earn_buffer_bps=0)
    service.sync_total_assets = AsyncMock(return_value=1)
    service.list_pools = MagicMock(return_value=[{"pool_id": p, "active": True} for p in POOLS])
    service.sapphire.w3.eth.get_transaction_receipt = MagicMock(
        return_value={"status": 1, "blockNumber": 5, "to": service.contract_address}
    )
    sapphire_ops = []

    async def submit(tx_id, *, function_name, args):
        pool = "0x" + args[0].hex()
        strategy = strategies[pool]
        # A reclaim spots its credit by the pool balance rising, so nothing
        # may move that balance while one runs.
        assert not strategy.reclaiming
        if function_name == "deposit":
            strategy.pool_balance += args[2]
        else:
            strategy.pool_balance -= args[1]
            strategy.lowest_balance = min(strategy.lowest_balance, strategy.pool_balance)
        sapphire_ops.append(function_name)
        await asyncio.sleep(0)
        return "0x" + tx_id.encode().hex().ljust(64, "0")[:64]

    service._submit_and_settle = submit
    return service, sapphire_ops


def _unsettled():
    return get_db().execute(
        "SELECT COUNT(*) FROM earn_transactions WHERE status NOT IN ('completed', 'failed')"
    ).fetchone()[0]


async def _run(service):
    """The worker and the idle deployer, concurrently, until the queue drains.
    The worker gets through many requests per round, as it does against a
    five minute interval in production."""
    worker = EarnWorker()
    deployer = IdleDeployer(service=service)

    async def run_worker():
        for _ in range(5_000):
            if not _unsettled():
                return
            await worker.run_once()
            await asyncio.sleep(0.001)

    async def run_deployer():
        while _unsettled():
            await deployer.deploy_once()
            await asyncio.sleep(0.1)
        await deployer.deploy_once()

    with patch("src.services.earn.worker.get_vault_service", return_value=service), \
         patch("src.services.earn.worker.read_share_settlement",
               return_value={"shares_delta": None, "exchange_rate": None}), \
         patch("src.services.earn.worker.LIQUIDITY_RECHECK_SEC", 0), \
         patch("src.services.earn.vault_service.sign_transfer", return_value="0x" + "bb" * 65):
        await asyncio.wait_for(asyncio.gather(run_worker(), run_deployer()), timeout=30)


def _statuses():
    return [r[0] for r in get_db().execute("SELECT status FROM earn_transactions").fetchall()]


@pytest.mark.asyncio
async def test_a_burst_of_mixed_requests_settles_in_a_few_moves(test_db):
    rng = random.Random(7)
    strategies = {pool: FakeStrategy() for pool in POOLS}
    service, sapphire_ops = _service(strategies)
    deposited = {pool: 0 for pool in POOLS}
    withdrawn = {pool: 0 for pool in POOLS}
    now = int(time.time())

    # Thirty users each deposit, then half of them take some back out.
    users = [f"0x{i:040x}" for i in range(1, 31)]
    ops = []
    for i, user in enumerate(users):
        pool = POOLS[i % 2]
        amount = rng.randint(100, 1_000)
        ops.append(("deposit", pool, user, amount))
        deposited[pool] += amount
        if i % 2 == 0:
            out = rng.randint(50, amount)
            ops.append(("withdraw", pool, user, out))
            withdrawn[pool] += out
    for n, (operation, pool, user, amount) in enumerate(ops):
        _schedule(f"op{n}", operation, pool, user, amount, now + n)

    await _run(service)

    assert _statuses() == ["completed"] * len(ops)
    assert len(sapphire_ops) == len(ops)
    for pool, strategy in strategies.items():
        # No payout ever overdrew the pool account, and nothing was lost.
        assert strategy.lowest_balance >= 0
        assert strategy.pool_balance + strategy.deployed == deposited[pool] - withdrawn[pool]
        # The protocol saw a handful of net moves, not one per request.
        assert len(strategy.moves) < len([o for o in ops if o[1] == pool]) / 2
        # Whatever is over the buffer is working.
        assert strategy.pool_balance <= 300


@pytest.mark.asyncio
async def test_a_run_on_the_pool_is_paid_by_a_few_shared_reclaims(test_db):
    rng = random.Random(11)
    strategies = {pool: FakeStrategy() for pool in POOLS}
    for strategy in strategies.values():
        strategy.pool_balance = 300
        strategy.deployed = 20_000
    service, sapphire_ops = _service(strategies)
    now = int(time.time())

    ops = []
    for i in range(24):
        ops.append(("withdraw", POOLS[i % 2], f"0x{i + 1:040x}", rng.randint(100, 900)))
    for n, (operation, pool, user, amount) in enumerate(ops):
        _schedule(f"op{n}", operation, pool, user, amount, now + n)

    await _run(service)

    assert _statuses() == ["completed"] * len(ops)
    assert len(sapphire_ops) == len(ops)
    for pool, strategy in strategies.items():
        out = sum(o[3] for o in ops if o[1] == pool)
        reclaims = [m for m in strategy.moves if m < 0]
        assert strategy.lowest_balance >= 0
        assert strategy.pool_balance + strategy.deployed == 20_300 - out
        assert 0 < len(reclaims) < len([o for o in ops if o[1] == pool]) / 2
        # Money reclaimed for a withdrawal is never deployed back before it pays.
        assert len(reclaims) == len(strategy.moves)
