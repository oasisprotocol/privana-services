from unittest.mock import MagicMock, patch

import pytest
from web3 import Web3

import src.clients.sapphire as sapphire_module

LP_KEY = "0x" + "11" * 32
ADMIN_KEY = "0x" + "22" * 32


def _settings(pool_admin_key: str = "") -> MagicMock:
    settings = MagicMock()
    settings.liquidity_provider_secret_key = LP_KEY
    settings.pool_admin_secret_key = pool_admin_key
    return settings


@pytest.fixture(autouse=True)
def reset_singletons():
    sapphire_module._client_instance = None
    sapphire_module._pool_admin_client_instance = None
    yield
    sapphire_module._client_instance = None
    sapphire_module._pool_admin_client_instance = None


class TestSapphireHttpProvider:
    def test_no_headers_keeps_web3_defaults(self):
        provider = sapphire_module.sapphire_http_provider("http://localhost:1", {})
        headers = provider.get_request_kwargs()["headers"]
        assert headers["Content-Type"] == "application/json"

    def test_extra_headers_are_merged_over_defaults(self):
        provider = sapphire_module.sapphire_http_provider(
            "http://localhost:1", {"Authorization": "Bearer secret-token"}
        )
        headers = provider.get_request_kwargs()["headers"]
        assert headers["Authorization"] == "Bearer secret-token"
        # Custom request_kwargs replace web3's defaults, so the helper must
        # merge Content-Type back in or JSON-RPC endpoints reject the request.
        assert headers["Content-Type"] == "application/json"

    def test_returns_a_plain_http_provider(self):
        provider = sapphire_module.sapphire_http_provider("http://localhost:1", {})
        assert isinstance(provider, Web3.HTTPProvider)


class TestGetPoolAdminSapphireClient:
    def test_falls_back_to_lp_client_when_admin_key_unset(self):
        lp_client = MagicMock()
        with patch.object(
            sapphire_module, "load_settings", return_value=_settings("")
        ), patch.object(
            sapphire_module, "get_sapphire_client", return_value=lp_client
        ):
            assert sapphire_module.get_pool_admin_sapphire_client() is lp_client

    def test_shares_lp_client_when_keys_match(self):
        lp_client = MagicMock()
        with patch.object(
            sapphire_module, "load_settings", return_value=_settings(LP_KEY)
        ), patch.object(
            sapphire_module, "get_sapphire_client", return_value=lp_client
        ):
            assert sapphire_module.get_pool_admin_sapphire_client() is lp_client

    def test_builds_separate_client_when_admin_key_differs(self):
        with patch.object(
            sapphire_module, "load_settings", return_value=_settings(ADMIN_KEY)
        ), patch.object(sapphire_module, "SapphireClient") as client_cls:
            client = sapphire_module.get_pool_admin_sapphire_client()
            client_cls.assert_called_once_with(secret_key=ADMIN_KEY)
            assert client is client_cls.return_value

    def test_reuses_the_admin_client_singleton(self):
        with patch.object(
            sapphire_module, "load_settings", return_value=_settings(ADMIN_KEY)
        ), patch.object(sapphire_module, "SapphireClient") as client_cls:
            first = sapphire_module.get_pool_admin_sapphire_client()
            second = sapphire_module.get_pool_admin_sapphire_client()
            assert first is second
            client_cls.assert_called_once()


class TestTransactionSubmission:
    def _client(self):
        client = sapphire_module.SapphireClient.__new__(sapphire_module.SapphireClient)
        client.account = MagicMock(address="0x" + "11" * 20)
        client.w3 = MagicMock()
        client.w3.eth.get_transaction_count.return_value = 9
        client.w3.eth.gas_price = 100
        contract = client.w3.eth.contract.return_value
        contract.functions.__getitem__.return_value.return_value.transact.return_value = bytes.fromhex("ab" * 32)
        return client

    def test_submit_uses_pending_nonce_without_waiting_for_receipt(self):
        client = self._client()
        tx_hash = client.submit_contract_call("0x" + "22" * 20, [], "swap", [1], 1_000_000)
        assert tx_hash == "0x" + "ab" * 32
        client.w3.eth.get_transaction_count.assert_called_once_with(client.account.address, "pending")
        call = client.w3.eth.contract.return_value.functions.__getitem__.return_value.return_value
        assert call.transact.call_args.args[0]["nonce"] == 9
        assert call.transact.call_args.args[0]["gas"] == 1_000_000
        client.w3.eth.wait_for_transaction_receipt.assert_not_called()

    def test_existing_execute_method_still_waits_and_checks_status(self):
        client = self._client()
        client.w3.eth.wait_for_transaction_receipt.return_value = {"status": 0}
        with pytest.raises(RuntimeError, match="Transaction reverted"):
            client.execute_contract_call("0x" + "22" * 20, [], "swap", [])
        client.w3.eth.wait_for_transaction_receipt.assert_called_once()

    def test_concurrent_submissions_allocate_distinct_evm_nonces(self):
        import time
        from concurrent.futures import ThreadPoolExecutor

        client = self._client()
        next_nonce = 9
        submitted = []

        def get_nonce(*args):
            value = next_nonce
            time.sleep(0.01)
            return value

        def submit(params):
            nonlocal next_nonce
            submitted.append(params["nonce"])
            next_nonce += 1
            return next_nonce.to_bytes(32, "big")

        client.w3.eth.get_transaction_count.side_effect = get_nonce
        call = client.w3.eth.contract.return_value.functions.__getitem__.return_value.return_value
        call.transact.side_effect = submit
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(client.submit_contract_call, "0x" + "22" * 20, [], "swap", []) for _ in range(5)]
            hashes = [f.result() for f in futures]
        assert submitted == [9, 10, 11, 12, 13]
        assert len(set(hashes)) == 5


class _RecordingProvider(Web3.HTTPProvider):
    def __init__(self):
        super().__init__("http://localhost:1")
        self.calls = []

    def make_request(self, method, params):
        self.calls.append((method, params))
        result = hex(23294) if method == "eth_chainId" else "0x" + "00" * 32
        return {"jsonrpc": "2.0", "id": 1, "result": result}


def test_the_reader_sends_historical_calls_unsigned():
    # Sapphire rejects signed queries pinned to a past block.
    settings = _settings()
    settings.accounting_chain_id = 23294
    providers = []

    def provider(*_args):
        providers.append(_RecordingProvider())
        return providers[-1]

    with patch.object(sapphire_module, "load_settings", return_value=settings), \
         patch.object(sapphire_module, "sapphire_http_provider", side_effect=provider):
        client = sapphire_module.SapphireClient()

    call = {"to": "0x" + "33" * 20, "data": "0x1234"}
    client.reader.eth.call(call, 100)

    method, params = next(c for p in providers for c in p.calls if c[0] == "eth_call")
    assert method == "eth_call"
    assert "from" not in params[0]
    assert params[0]["data"] == "0x1234"
    assert params[1] == hex(100)
