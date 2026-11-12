from unittest.mock import MagicMock

import pytest
from web3 import Web3
from web3.exceptions import TransactionNotFound

TOKEN = "0x" + "aa" * 20
OWNER = "0x" + "cc" * 20
RECIPIENT = "0x" + "dd" * 20


def _make_client(w3):
    from src.clients.base_evm import EvmClient
    client = EvmClient("http://localhost:1", "0x" + "11" * 32)
    client.w3 = w3
    return client


def _w3():
    w3 = MagicMock()
    w3.eth.max_priority_fee = 100
    w3.eth.get_block.return_value = {"baseFeePerGas": 1_000}
    return w3


class TestSending:
    def test_fees_price_gas_from_the_chain(self):
        client = _make_client(_w3())

        assert client.fees() == {"maxFeePerGas": 2 * 1_000 + 100, "maxPriorityFeePerGas": 100}
        assert client.max_gas_cost(0x15FCBF) == 0x15FCBF * (2 * 1_000 + 100)

    def test_a_signed_tx_is_named_by_the_hash_of_its_bytes(self):
        client = _make_client(_w3())
        tx = {"to": Web3.to_checksum_address(RECIPIENT), "value": 1, "gas": 21_000, "nonce": 0, "chainId": 8453,
              "maxFeePerGas": 2_100, "maxPriorityFeePerGas": 100}

        tx_hash, raw = client.sign(tx)

        assert tx_hash == Web3.to_hex(Web3.keccak(hexstr=raw))


class TestTokenHelpers:
    def test_token_balance(self):
        w3 = _w3()
        contract = MagicMock()
        contract.functions.balanceOf.return_value.call.return_value = 555
        w3.eth.contract.return_value = contract
        client = _make_client(w3)
        assert client.balance_of(TOKEN, OWNER) == 555


class TestNativeCoins:
    NATIVE = "0x0000000000000000000000000000000000000000"

    @pytest.mark.parametrize("token", [None, "", NATIVE])
    def test_balance_is_the_account_balance(self, token):
        w3 = _w3()
        w3.eth.get_balance.return_value = 777
        client = _make_client(w3)

        assert client.balance_of(token, OWNER) == 777
        w3.eth.contract.assert_not_called()


class TestOutcome:
    TX = "0x" + "ab" * 32

    def test_no_receipt_until_a_block_holds_the_tx(self):
        w3 = _w3()
        w3.eth.get_transaction_receipt.side_effect = TransactionNotFound("unknown")
        assert _make_client(w3).get_receipt(self.TX) is None

    def test_the_receipt_once_mined(self):
        w3 = _w3()
        w3.eth.get_transaction_receipt.return_value = {"status": 1}
        assert _make_client(w3).get_receipt(self.TX) == {"status": 1}


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

