from unittest.mock import MagicMock, patch

import pytest
from web3 import Web3

TOKEN = "0x" + "aa" * 20
SPENDER = "0x" + "bb" * 20
OWNER = "0x" + "cc" * 20
RECIPIENT = "0x" + "dd" * 20
TOKEN_CHECKSUMMED = Web3.to_checksum_address(TOKEN)


def _make_client(w3):
    from src.clients.base_evm import EvmClient
    client = EvmClient("http://localhost:1", "0x" + "11" * 32)
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


class TestTokenHelpers:
    def test_token_balance(self):
        w3 = _w3()
        contract = MagicMock()
        contract.functions.balanceOf.return_value.call.return_value = 555
        w3.eth.contract.return_value = contract
        client = _make_client(w3)
        assert client.balance_of(TOKEN, OWNER) == 555

    def test_token_transfer_returns_hash(self):
        w3 = _w3()
        contract = MagicMock()
        contract.functions.transfer.return_value.build_transaction.return_value = {
            "nonce": 7, "gas": 100_000,
            "gasPrice": 1_000_000_000, "chainId": 84532, "value": 0,
            "to": TOKEN_CHECKSUMMED, "data": "0x",
        }
        w3.eth.contract.return_value = contract
        client = _make_client(w3)
        assert client.transfer(TOKEN, RECIPIENT, 42) == "0x" + "ab" * 32


class TestNativeCoins:
    NATIVE = "0x0000000000000000000000000000000000000000"

    @pytest.mark.parametrize("token", [None, "", NATIVE])
    def test_balance_is_the_account_balance(self, token):
        w3 = _w3()
        w3.eth.get_balance.return_value = 777
        client = _make_client(w3)

        assert client.balance_of(token, OWNER) == 777
        w3.eth.contract.assert_not_called()

    def test_transfer_sends_value_with_no_calldata(self):
        w3 = _w3()
        client = _make_client(w3)
        with patch.object(client._account, "sign_transaction") as mock_sign:
            mock_sign.return_value = MagicMock(raw_transaction=b"raw")
            client.transfer(None, RECIPIENT, 42)
            tx = mock_sign.call_args.args[0]

        assert tx["to"] == Web3.to_checksum_address(RECIPIENT)
        assert tx["value"] == 42
        assert tx["gas"] == 21_000
        assert "data" not in tx
        w3.eth.contract.assert_not_called()

    def test_nothing_to_approve(self):
        w3 = _w3()
        client = _make_client(w3)

        assert client.ensure_allowance(self.NATIVE, SPENDER, 1000) is None
        w3.eth.contract.assert_not_called()


class TestChainGuard:
    def test_refuses_a_transaction_built_for_another_chain(self):
        w3 = _w3()
        client = _make_client(w3)
        request = {**TestSendTransactionRequest.TX_REQUEST, "chainId": 999}

        with pytest.raises(ValueError, match="built for chain 999"):
            client.send_transaction_request(request)
        w3.eth.send_raw_transaction.assert_not_called()

    def test_sends_one_built_for_its_own_chain(self):
        client = _make_client(_w3())
        request = {**TestSendTransactionRequest.TX_REQUEST, "chainId": 84532}

        assert client.send_transaction_request(request) == "0x" + "ab" * 32

    def test_gas_cost_is_what_the_receipt_paid(self):
        w3 = _w3()
        w3.eth.get_transaction_receipt.return_value = {"gasUsed": 21_000, "effectiveGasPrice": 3}
        client = _make_client(w3)

        assert client.gas_cost("0x" + "ab" * 32) == 63_000


class TestClientPerChain:
    @pytest.fixture
    def rpcs(self, monkeypatch):
        import src.clients.base_evm as module

        monkeypatch.setattr(module, "_clients", {})
        monkeypatch.setattr(module, "_chain_ids", {})
        answers = {"base": 8453, "eth": 1, "hyper": 999}

        def make(rpc_url, _key):
            client = MagicMock()
            if answers[rpc_url] is None:
                type(client.w3.eth).chain_id = property(lambda _: (_ for _ in ()).throw(OSError("down")))
            else:
                client.w3.eth.chain_id = answers[rpc_url]
            client.rpc = rpc_url
            return client

        settings = MagicMock(
            base_rpc_url="base", ethereum_rpc_url="eth", hyperevm_rpc_url="hyper",
            liquidity_provider_secret_key="0x" + "11" * 32,
        )
        monkeypatch.setattr(module, "EvmClient", make)
        monkeypatch.setattr(module, "load_settings", lambda: settings)
        return answers, settings

    def test_picks_the_rpc_that_serves_the_chain(self, rpcs):
        from src.clients.base_evm import get_evm_client

        assert get_evm_client(999).rpc == "hyper"
        assert get_evm_client(8453).rpc == "base"
        assert get_evm_client(999) is get_evm_client(999)

    def test_a_chain_nobody_serves_is_an_error(self, rpcs):
        from src.clients.base_evm import get_evm_client

        with pytest.raises(ValueError, match="chain 42161"):
            get_evm_client(42161)

    def test_an_unset_or_unreachable_rpc_is_skipped(self, rpcs):
        from src.clients.base_evm import get_evm_client

        answers, settings = rpcs
        settings.ethereum_rpc_url = ""
        answers["base"] = None

        assert get_evm_client(999).rpc == "hyper"
        with pytest.raises(ValueError):
            get_evm_client(1)


class TestTxLock:
    def test_each_chain_orders_its_own_sends(self):
        import asyncio

        first, second = _make_client(_w3()), _make_client(_w3())
        assert isinstance(first.tx_lock, asyncio.Lock)
        assert first.tx_lock is not second.tx_lock
