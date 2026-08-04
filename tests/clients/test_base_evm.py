from unittest.mock import MagicMock, patch

import pytest
from web3 import Web3

TOKEN = "0x" + "aa" * 20
SPENDER = "0x" + "bb" * 20
OWNER = "0x" + "cc" * 20
RECIPIENT = "0x" + "dd" * 20
TOKEN_CHECKSUMMED = Web3.to_checksum_address(TOKEN)


def _make_client(w3):
    from src.clients.base_evm import BaseEvmClient
    client = BaseEvmClient("http://localhost:1", "0x" + "11" * 32)
    client.w3 = w3
    return client


def _w3(receipt_status=1):
    w3 = MagicMock()
    w3.eth.get_transaction_count.return_value = 7
    w3.eth.chain_id = 84532
    w3.eth.gas_price = 1_000_000_000
    w3.eth.send_raw_transaction.return_value = bytes.fromhex("ab" * 32)
    w3.eth.wait_for_transaction_receipt.return_value = MagicMock(status=receipt_status)
    return w3


class TestSendTransactionRequest:
    TX_REQUEST = {
        "to": "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE",
        "data": "0xdead",
        "value": "0x0",
        "gasLimit": "0x15fcbf",
        "gasPrice": "0x3b9aca00",
    }

    def test_success_returns_prefixed_hash(self):
        w3 = _w3()
        client = _make_client(w3)
        tx_hash = client.send_transaction_request(self.TX_REQUEST)
        assert tx_hash == "0x" + "ab" * 32
        assert w3.eth.send_raw_transaction.called

    def test_builds_tx_from_request_fields(self):
        w3 = _w3()
        client = _make_client(w3)
        with patch.object(client._account, "sign_transaction") as mock_sign:
            mock_sign.return_value = MagicMock(raw_transaction=b"raw")
            client.send_transaction_request(self.TX_REQUEST)
            tx = mock_sign.call_args.args[0]
        assert tx["to"] == self.TX_REQUEST["to"]
        assert tx["data"] == "0xdead"
        assert tx["value"] == 0
        assert tx["gas"] == 0x15FCBF
        assert tx["gasPrice"] == 0x3B9ACA00
        assert tx["nonce"] == 7
        assert tx["chainId"] == 84532

    def test_reverted_receipt_raises(self):
        client = _make_client(_w3(receipt_status=0))
        with pytest.raises(RuntimeError, match="reverted"):
            client.send_transaction_request(self.TX_REQUEST)


class TestEnsureAllowance:
    def test_sufficient_allowance_skips_approve(self):
        w3 = _w3()
        contract = MagicMock()
        contract.functions.allowance.return_value.call.return_value = 10**18
        w3.eth.contract.return_value = contract
        client = _make_client(w3)
        assert client.ensure_allowance(TOKEN, SPENDER, 1000) is None
        contract.functions.approve.assert_not_called()

    def test_short_allowance_sends_approve(self):
        w3 = _w3()
        contract = MagicMock()
        contract.functions.allowance.return_value.call.return_value = 0
        contract.functions.approve.return_value.build_transaction.return_value = {
            "nonce": 7, "gas": 80_000,
            "gasPrice": 1_000_000_000, "chainId": 84532, "value": 0,
            "to": TOKEN_CHECKSUMMED, "data": "0x",
        }
        w3.eth.contract.return_value = contract
        client = _make_client(w3)
        tx_hash = client.ensure_allowance(TOKEN, SPENDER, 1000)
        assert tx_hash == "0x" + "ab" * 32


class TestErc20Helpers:
    def test_erc20_balance(self):
        w3 = _w3()
        contract = MagicMock()
        contract.functions.balanceOf.return_value.call.return_value = 555
        w3.eth.contract.return_value = contract
        client = _make_client(w3)
        assert client.erc20_balance(TOKEN, OWNER) == 555

    def test_transfer_erc20_returns_hash(self):
        w3 = _w3()
        contract = MagicMock()
        contract.functions.transfer.return_value.build_transaction.return_value = {
            "nonce": 7, "gas": 100_000,
            "gasPrice": 1_000_000_000, "chainId": 84532, "value": 0,
            "to": TOKEN_CHECKSUMMED, "data": "0x",
        }
        w3.eth.contract.return_value = contract
        client = _make_client(w3)
        assert client.transfer_erc20(TOKEN, RECIPIENT, 42) == "0x" + "ab" * 32


class TestModuleLock:
    def test_base_tx_lock_exists(self):
        import asyncio

        from src.clients.base_evm import base_tx_lock
        assert isinstance(base_tx_lock, asyncio.Lock)
