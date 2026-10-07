from unittest.mock import MagicMock, patch

import pytest
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound

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
    w3.eth.max_priority_fee = 100
    w3.eth.get_block.return_value = {"baseFeePerGas": 1_000}
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
        assert tx["nonce"] == 7
        assert tx["chainId"] == 84532

    def test_prices_gas_from_the_chain_not_from_lifi(self):
        client = _make_client(_w3())
        with patch.object(client._account, "sign_transaction") as mock_sign:
            mock_sign.return_value = MagicMock(raw_transaction=b"raw")
            client.send_transaction_request(self.TX_REQUEST)
            tx = mock_sign.call_args.args[0]
        assert "gasPrice" not in tx
        assert tx["maxPriorityFeePerGas"] == 100
        assert tx["maxFeePerGas"] == 2 * 1_000 + 100

    def test_max_gas_cost_uses_the_fee_cap_a_send_sets(self):
        client = _make_client(_w3())
        assert client.max_gas_cost(0x15FCBF) == 0x15FCBF * (2 * 1_000 + 100)

    def test_hands_over_the_signed_tx_before_broadcast(self):
        w3 = _w3()
        client = _make_client(w3)
        handed = []
        on_signed = MagicMock(side_effect=lambda *args: handed.append(
            (args, w3.eth.send_raw_transaction.called)))
        with patch.object(client._account, "sign_transaction") as mock_sign:
            mock_sign.return_value = MagicMock(raw_transaction=b"raw")
            client.send_transaction_request(self.TX_REQUEST, on_signed=on_signed)
        assert handed == [((Web3.to_hex(Web3.keccak(b"raw")), 7, Web3.to_hex(b"raw")), False)]

    def test_a_failed_hand_over_sends_nothing(self):
        w3 = _w3()
        client = _make_client(w3)
        with pytest.raises(OSError):
            client.send_transaction_request(
                self.TX_REQUEST, on_signed=MagicMock(side_effect=OSError("disk full")))
        w3.eth.send_raw_transaction.assert_not_called()

    def test_reverted_receipt_raises(self):
        client = _make_client(_w3(receipt_status=0))
        with pytest.raises(RuntimeError, match="reverted"):
            client.send_transaction_request(self.TX_REQUEST)

    @pytest.mark.parametrize("error", [TimeExhausted("not in the chain"), ConnectionError("rpc down")])
    def test_a_failed_receipt_wait_raises_with_the_sent_hash(self, error):
        from src.clients.base_evm import TransactionPendingError
        w3 = _w3()
        w3.eth.wait_for_transaction_receipt.side_effect = error
        client = _make_client(w3)
        with pytest.raises(TransactionPendingError) as exc:
            client.send_transaction_request(self.TX_REQUEST)
        assert exc.value.tx_hash == "0x" + "ab" * 32

    # A node lacking the tx proves nothing: another backend may hold it.
    @pytest.mark.parametrize("lookup", [None, TransactionNotFound("unknown"), ConnectionError("rpc down")])
    def test_a_failed_send_raises_with_the_signed_hash(self, lookup):
        from src.clients.base_evm import TransactionPendingError
        w3 = _w3()
        w3.eth.send_raw_transaction.side_effect = ValueError("already known")
        w3.eth.get_transaction.side_effect = lookup
        client = _make_client(w3)
        with patch.object(client._account, "sign_transaction") as mock_sign:
            mock_sign.return_value = MagicMock(raw_transaction=b"raw")
            with pytest.raises(TransactionPendingError) as exc:
                client.send_transaction_request(self.TX_REQUEST)
        assert exc.value.tx_hash == Web3.to_hex(Web3.keccak(b"raw"))
        w3.eth.wait_for_transaction_receipt.assert_not_called()


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

    def test_allowance_is_what_the_spender_may_move_for_this_account(self):
        w3 = _w3()
        contract = MagicMock()
        contract.functions.allowance.return_value.call.return_value = 555
        w3.eth.contract.return_value = contract
        client = _make_client(w3)
        assert client.allowance(TOKEN, SPENDER) == 555
        contract.functions.allowance.assert_called_once_with(
            client.address, Web3.to_checksum_address(SPENDER))


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
        assert tx["maxFeePerGas"] == 2 * 1_000 + 100
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

    def _lost(self, w3):
        """A transfer signed at a base fee of 1_000 and handed over, its broadcast lost."""
        from src.clients.base_evm import TransactionPendingError
        on_signed = MagicMock()
        w3.eth.send_raw_transaction.side_effect = ConnectionError("rpc down")
        with pytest.raises(TransactionPendingError):
            _make_client(w3).transfer(None, RECIPIENT, 42, on_signed=on_signed)
        w3.eth.send_raw_transaction.side_effect = None
        return on_signed.call_args.args

    def test_rebroadcast_sends_the_bytes_handed_over_at_signing(self):
        w3 = _w3()
        _, _, raw = self._lost(w3)
        on_signed = MagicMock()
        # A fresh client, as after a restart.
        _make_client(w3).rebroadcast(raw, on_signed)
        first, again = w3.eth.send_raw_transaction.call_args_list
        assert HexBytes(again.args[0]) == HexBytes(first.args[0]) == HexBytes(raw)
        on_signed.assert_not_called()

    def test_a_refused_rebroadcast_is_not_an_error(self):
        w3 = _w3()
        _, _, raw = self._lost(w3)
        w3.eth.send_raw_transaction.side_effect = ValueError("already known")
        _make_client(w3).rebroadcast(raw, MagicMock())

    def test_a_tx_the_base_fee_priced_out_is_re_signed_at_its_nonce(self):
        from eth_account import Account
        from eth_account.typed_transactions import TypedTransaction
        w3 = _w3()
        _, nonce, raw = self._lost(w3)
        # Past the cap of 2 * 1_000 + 100.
        w3.eth.get_block.return_value = {"baseFeePerGas": 3_000}
        w3.eth.get_balance.return_value = 10**18
        signer = _make_client(w3)
        handed = []

        def on_signed(*version):
            # Saved before any node sees it.
            assert w3.eth.send_raw_transaction.call_count == 1
            handed.append(version)

        signer.rebroadcast(raw, on_signed)

        [(tx_hash, new_nonce, new_raw)] = handed
        assert HexBytes(w3.eth.send_raw_transaction.call_args.args[0]) == HexBytes(new_raw)
        assert tx_hash == Web3.to_hex(Web3.keccak(hexstr=new_raw))
        assert Account.recover_transaction(new_raw) == signer.address
        old, new = (TypedTransaction.from_bytes(HexBytes(r)).as_dict() for r in (raw, new_raw))
        assert new_nonce == nonce == new["nonce"]
        for field in ("chainId", "to", "value", "data", "gas", "accessList"):
            assert new[field] == old[field]
        # Priced as a send now, with both fees raised by at least the 10% nodes require.
        assert new["maxFeePerGas"] == 2 * 3_000 + new["maxPriorityFeePerGas"]
        for fee in ("maxFeePerGas", "maxPriorityFeePerGas"):
            assert new[fee] * 100 >= old[fee] * 110

    def test_a_replacement_the_balance_cannot_pay_leaves_the_tx_as_signed(self):
        w3 = _w3()
        _, _, raw = self._lost(w3)
        w3.eth.get_block.return_value = {"baseFeePerGas": 3_000}
        w3.eth.get_balance.return_value = 0
        on_signed = MagicMock()

        _make_client(w3).rebroadcast(raw, on_signed)

        on_signed.assert_not_called()
        assert HexBytes(w3.eth.send_raw_transaction.call_args.args[0]) == HexBytes(raw)


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
