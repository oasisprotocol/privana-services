from unittest.mock import MagicMock

import pytest

from src.services.user_queue import StaleNonceError, assert_nonce_unspent

USER = "0x" + "11" * 20


def _accounting(current=None, error=None):
    accounting = MagicMock()
    accounting.transfer_nonce = MagicMock(return_value=current, side_effect=error)
    return accounting


def test_a_nonce_below_the_chain_is_refused():
    with pytest.raises(StaleNonceError, match="Nonce 2 has already been used; sign again with nonce 5"):
        assert_nonce_unspent(_accounting(5), USER, 2)


@pytest.mark.parametrize("nonce", [5, 6])
def test_the_current_nonce_and_later_ones_pass(nonce):
    assert_nonce_unspent(_accounting(5), USER, nonce)


def test_a_failed_read_lets_the_request_through():
    assert_nonce_unspent(_accounting(error=OSError("rpc down")), USER, 0)
