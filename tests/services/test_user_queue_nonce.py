import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.user_queue import StaleNonceError, assert_nonce_unspent

USER = "0x" + "11" * 20


def _accounting(current=None, error=None):
    accounting = MagicMock()
    accounting.get_transfer_nonce = AsyncMock(return_value=current, side_effect=error)
    return accounting


async def test_a_nonce_below_the_chain_is_refused():
    with pytest.raises(StaleNonceError, match="Nonce 2 has already been used; sign again with nonce 5"):
        await assert_nonce_unspent(_accounting(5), USER, 2)


@pytest.mark.parametrize("nonce", [5, 6])
async def test_the_current_nonce_and_later_ones_pass(nonce):
    await assert_nonce_unspent(_accounting(5), USER, nonce)


async def test_a_failed_read_lets_the_request_through():
    await assert_nonce_unspent(_accounting(error=OSError("rpc down")), USER, 0)


async def test_a_slow_read_lets_the_request_through(monkeypatch):
    import src.services.user_queue as module

    monkeypatch.setattr(module, "NONCE_READ_TIMEOUT_SEC", 0.01)

    async def slow(_user):
        await asyncio.sleep(1)
        return 99

    accounting = MagicMock()
    accounting.get_transfer_nonce = slow

    await assert_nonce_unspent(accounting, USER, 0)
