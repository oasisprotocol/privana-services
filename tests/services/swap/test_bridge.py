import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from privana.client.errors import NetworkError


@pytest.fixture(autouse=True)
def fresh_withdrawal_lock(monkeypatch):
    """Each test runs on its own event loop; the service runs on one."""
    monkeypatch.setattr("src.services.swap.bridge.withdrawal_lock", asyncio.Lock())


def _pending(indices):
    resp = MagicMock()
    resp.pending_withdrawals = [MagicMock(index=i) for i in indices]
    return resp


def _make_bridge(client):
    from src.services.swap.bridge import AccountingBridge

    async def factory():
        return client

    return AccountingBridge(poll_interval_sec=0.0, client_factory=factory)


class TestWithdrawToChain:
    async def test_returns_index_when_resolved(self):
        client = AsyncMock()
        client.get_pending_withdrawals = AsyncMock(side_effect=[_pending([]), _pending([5])])
        client.get_withdrawal_nonce = AsyncMock(return_value=MagicMock(nonce=9))
        client.request_withdrawal = AsyncMock(return_value=MagicMock(status="submitted", detail=None))
        client.get_withdrawal_info = AsyncMock(return_value=MagicMock(resolved=True, tx_identifier="0x11"))
        bridge = _make_bridge(client)
        index = await bridge.withdraw_to_chain("0x" + "aa" * 32, 1_000_000)
        assert index == 5

    async def test_rejected_submission_raises(self):
        client = AsyncMock()
        client.get_pending_withdrawals = AsyncMock(return_value=_pending([]))
        client.get_withdrawal_nonce = AsyncMock(return_value=MagicMock(nonce=9))
        client.request_withdrawal = AsyncMock(return_value=MagicMock(status="rejected", detail="nope"))
        bridge = _make_bridge(client)
        with pytest.raises(RuntimeError, match="rejected"):
            await bridge.withdraw_to_chain("0x" + "aa" * 32, 1_000_000)

    async def test_network_errors_retried_on_reads(self):
        client = AsyncMock()
        client.get_pending_withdrawals = AsyncMock(
            side_effect=[NetworkError("drop", None), _pending([]), _pending([3])])
        client.get_withdrawal_nonce = AsyncMock(return_value=MagicMock(nonce=9))
        client.request_withdrawal = AsyncMock(return_value=MagicMock(status="submitted", detail=None))
        client.get_withdrawal_info = AsyncMock(return_value=MagicMock(resolved=True, tx_identifier="0x11"))
        bridge = _make_bridge(client)
        assert await bridge.withdraw_to_chain("0x" + "aa" * 32, 1_000_000) == 3

    async def test_unresolved_after_max_polls_raises(self):
        client = AsyncMock()
        client.get_pending_withdrawals = AsyncMock(return_value=_pending([]))
        client.get_withdrawal_nonce = AsyncMock(return_value=MagicMock(nonce=9))
        client.request_withdrawal = AsyncMock(return_value=MagicMock(status="submitted", detail=None))
        bridge = _make_bridge(client)
        bridge._max_poll_attempts = 3
        with pytest.raises(RuntimeError, match="unresolved"):
            await bridge.withdraw_to_chain("0x" + "aa" * 32, 1_000_000)


class FakeAccounting:
    """The LP's withdrawal nonce and pending list the way the relay keeps them:
    a request signed with anything but the current nonce is rejected, and an
    accepted one shows up in the pending list a moment later."""

    def __init__(self):
        self.nonce = 9
        self.pending = []
        self.arriving = []
        self.next_index = 40
        self.token_by_index = {}

    async def get_withdrawal_nonce(self, _address):
        await asyncio.sleep(0)
        return MagicMock(nonce=self.nonce)

    async def get_pending_withdrawals(self, _address):
        await asyncio.sleep(0)
        # An accepted request is listed from the second poll after it, so a
        # parallel task gets a turn in between.
        listed = _pending(list(self.pending))
        self.pending.extend(self.arriving)
        self.arriving = []
        return listed

    async def request_withdrawal(self, request):
        await asyncio.sleep(0)
        if request.nonce != self.nonce:
            return MagicMock(
                status="error",
                detail="Transaction reverted: InvalidNonce (module: evm, code: 8)",
            )
        self.nonce += 1
        index = self.next_index
        self.next_index += 1
        self.token_by_index[index] = request.token_id
        self.arriving.append(index)
        return MagicMock(status="submitted", detail=None)

    async def get_withdrawal_info(self, index):
        await asyncio.sleep(0)
        return MagicMock(resolved=index in self.pending, tx_identifier="0x11")


class TestConcurrentWithdrawals:
    async def test_parallel_swaps_each_get_their_own_nonce_and_index(self):
        accounting = FakeAccounting()
        bridge = _make_bridge(accounting)
        tokens = ["0x" + "aa" * 32, "0x" + "bb" * 32, "0x" + "cc" * 32]

        # LiFi swaps run as parallel tasks; before, two of them read the same
        # withdrawal nonce and the relay rejected one with InvalidNonce.
        indices = await asyncio.gather(
            *(bridge.withdraw_to_chain(token, 1_000_000) for token in tokens)
        )

        assert accounting.nonce == 12
        assert len(set(indices)) == 3
        assert [accounting.token_by_index[i] for i in indices] == tokens

    async def test_a_second_bridge_instance_waits_its_turn_too(self):
        accounting = FakeAccounting()
        first, second = _make_bridge(accounting), _make_bridge(accounting)

        await asyncio.gather(
            first.withdraw_to_chain("0x" + "aa" * 32, 1),
            second.withdraw_to_chain("0x" + "bb" * 32, 1),
        )

        assert accounting.nonce == 11


class TestAwaitDepositCredit:
    async def test_normalizes_tx_hash_and_polls_to_credit(self):
        client = AsyncMock()
        client.check_deposit = AsyncMock(return_value=MagicMock(status="pending", detail=None))
        client.get_balance = AsyncMock(side_effect=[MagicMock(balance="100"), MagicMock(balance="1000100")])
        bridge = _make_bridge(client)
        await bridge.await_deposit_credit(84532, "ab" * 32, 1_000_000, "0x" + "aa" * 32, pre_balance=100)
        sent = client.check_deposit.call_args.args[0]
        assert sent.tx_hash.startswith("0x")

    async def test_check_deposit_error_is_tolerated(self):
        client = AsyncMock()
        client.check_deposit = AsyncMock(side_effect=Exception("400"))
        client.get_balance = AsyncMock(return_value=MagicMock(balance="1000100"))
        bridge = _make_bridge(client)
        await bridge.await_deposit_credit(84532, "0x" + "ab" * 32, 1_000_000, "0x" + "aa" * 32, pre_balance=100)

    async def test_credit_never_observed_raises(self):
        client = AsyncMock()
        client.check_deposit = AsyncMock(return_value=MagicMock(status="pending", detail=None))
        client.get_balance = AsyncMock(return_value=MagicMock(balance="100"))
        bridge = _make_bridge(client)
        bridge._max_poll_attempts = 3
        with pytest.raises(RuntimeError, match="credit"):
            await bridge.await_deposit_credit(84532, "0x" + "ab" * 32, 1_000_000, "0x" + "aa" * 32, pre_balance=100)


class TestHelpers:
    async def test_get_deposit_address(self):
        client = AsyncMock()
        client.get_deposit_address = AsyncMock(return_value=MagicMock(deposit_address="0xdeposit"))
        bridge = _make_bridge(client)
        assert await bridge.get_deposit_address() == "0xdeposit"

    async def test_lp_internal_balance(self):
        client = AsyncMock()
        client.get_balance = AsyncMock(return_value=MagicMock(balance="42"))
        bridge = _make_bridge(client)
        assert await bridge.lp_internal_balance("0x" + "aa" * 32) == 42
