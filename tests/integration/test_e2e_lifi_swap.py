import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from eth_abi import encode
from eth_account import Account
from hexbytes import HexBytes

from src.core.config import load_settings
from src.core.eip712 import sign_transfer
from src.models.common import Balance, TokenInfo

USER_SK = "0x" + "11" * 32
USER = Account.from_key(USER_SK).address
LP_ADDRESS = "0x152E6a7125665764a4F1F1df80E8f5D49Bf0239c"
FROM_TOKEN = "0x" + "aa" * 32
TO_TOKEN = "0x" + "bb" * 32


def _sign_input(settings):
    return sign_transfer(
        private_key=USER_SK,
        chain_id=settings.accounting_chain_id,
        verifying_contract=settings.accounting_contract_address,
        to_address=LP_ADDRESS,
        token_id=FROM_TOKEN,
        amount=1000000,
        nonce=5,
    )


LOW_BALANCE = Balance(user_address="0xlp", token_id=TO_TOKEN, balance="1")
FROM_INFO = TokenInfo(
    token_id=FROM_TOKEN, token_type=1, token_type_name="ERC20", data="0x00",
    chain_id=84532, chain_name="Base Sepolia",
    token_address="0x8eEDCff0b07609Cfb5e2775dFf21EDbACc30D0df",
)
TO_INFO = TokenInfo(
    token_id=TO_TOKEN, token_type=1, token_type_name="ERC20", data="0x00",
    chain_id=84532, chain_name="Base Sepolia",
    token_address="0xA9B8D8039cb3FF9d9Fff6decD18EA7bb792e51D3",
)
PRICING_ROUTES = {
    "routes": [{"toAmount": "58000", "toAmountMin": "56000", "steps": [{"tool": "fly"}]}]
}
EXEC_QUOTE = {
    "tool": "fly",
    "transactionRequest": {
        "to": "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE",
        "data": "0xdead", "value": "0x0",
        "gasLimit": "0x15fcbf", "gasPrice": "0x3b9aca00",
    },
    "estimate": {
        "approvalAddress": "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE",
        "toAmount": "58000", "toAmountMin": "56000",
    },
}


@pytest.fixture(autouse=True)
def _an_rpc_for_every_chain(monkeypatch):
    """Quoting checks for an RPC on each chain. These tests reach none."""
    import src.services.swap.quote_service as qs_mod

    monkeypatch.setattr(qs_mod, "get_evm_client", MagicMock())


def _stub_quote_service(lifi_enabled=True):
    import src.services.swap.quote_service as qs_mod
    from src.services.swap.quote_service import QuoteService

    service = QuoteService.__new__(QuoteService)
    service.settings = replace(
        load_settings(),
        fee_bps=10,
        quote_ttl=300,
        liquidity_provider_address=LP_ADDRESS,
        lifi_execution_enabled=lifi_enabled,
        lifi_max_swap_amount_usd=0,
    )
    service._last_cleanup = 0
    service._token_map = {}
    service.accounting = MagicMock()
    service.accounting.get_transfer_nonce = AsyncMock(return_value=5)
    service.accounting.get_lp_balance = AsyncMock(return_value=LOW_BALANCE)
    service.accounting.get_token_info = AsyncMock(side_effect=[FROM_INFO, TO_INFO])
    service.lifi = MagicMock()
    service.lifi.get_routes = AsyncMock(return_value=PRICING_ROUTES)
    qs_mod._service_instance = service
    return service


def _sends(tx_hash, nonce, error=None):
    """A send that hands over the signed tx before broadcast, then mines or
    raises, as EvmClient does."""
    def send(*_, on_signed):
        on_signed(tx_hash, nonce, "0x02" + tx_hash[2:])
        if error is not None:
            raise error
        return tx_hash
    return send


def _stub_pipeline(settings, lifi_reverts=False):
    import src.services.swap.lifi_pipeline as lp_mod
    from src.services.swap.lifi_pipeline import (
        SWAP_COMPLETED_DATA,
        SWAP_COMPLETED_TOPIC,
        LifiSwapPipeline,
    )

    accounting = MagicMock()
    accounting.get_transfer_nonce = AsyncMock(side_effect=[6, 70, 71, 71])
    accounting.get_token_info = AsyncMock(side_effect=[FROM_INFO, TO_INFO, FROM_INFO])
    lifi = MagicMock()
    lifi.get_execution_quote = AsyncMock(return_value=EXEC_QUOTE)
    bridge = MagicMock()
    bridge.withdraw_to_chain = AsyncMock(return_value=17)
    bridge.get_deposit_address = AsyncMock(return_value="0x" + "dd" * 20)
    bridge.await_deposit_credit = AsyncMock(return_value=None)
    lifi_tx, deposit_tx = "0x" + "cd" * 32, "0x" + "ef" * 32
    diamond = EXEC_QUOTE["transactionRequest"]["to"]
    swapped = {
        "address": diamond,
        "topics": [SWAP_COMPLETED_TOPIC, HexBytes("01" * 32)],
        "data": encode(SWAP_COMPLETED_DATA, [
            "privana-services", "", LP_ADDRESS, FROM_INFO.token_address,
            TO_INFO.token_address, 1000000, 60000,
        ]),
    }
    lifi_receipt = {"status": 0, "to": diamond, "logs": []} if lifi_reverts else {
        "status": 1, "to": diamond, "logs": [swapped]}
    reverted = RuntimeError(f"transaction reverted: {lifi_tx}") if lifi_reverts else None
    evm = MagicMock()
    evm.address = "0x152E6a7125665764a4F1F1df80E8f5D49Bf0239c"
    evm.balance_of = MagicMock(return_value=1000000)
    evm.ensure_allowance = MagicMock(return_value=None)
    evm.send_transaction_request = MagicMock(side_effect=_sends(lifi_tx, 40, reverted))
    evm.tx_lock = asyncio.Lock()
    evm.transfer = MagicMock(side_effect=_sends(deposit_tx, 41))
    evm.get_receipt = MagicMock(side_effect=lambda tx_hash: (
        lifi_receipt if tx_hash == lifi_tx else {"status": 1, "to": None, "logs": []}))
    privana = MagicMock()
    privana.transfer_funds = AsyncMock(return_value=MagicMock(status="submitted", detail=None))

    async def privana_factory():
        return privana

    pipeline = LifiSwapPipeline(
        accounting=accounting, lifi=lifi, bridge=bridge, evm=evm,
        privana_factory=privana_factory, poll_interval_sec=0.0,
    )
    pipeline.settings = replace(settings, fee_bps=10)
    lp_mod._pipeline_instance = pipeline
    return pipeline


def _stub_executor(settings):
    import src.services.swap.executor as se_mod
    from src.services.swap.executor import SwapExecutor

    executor = SwapExecutor()
    executor.settings = replace(settings, lifi_execution_enabled=True)
    se_mod._executor_instance = executor
    return executor


async def _drain_background(pipeline):
    from src.services.swap.worker import SwapWorker

    worker = SwapWorker()
    worker.settings = replace(pipeline.settings, lifi_execution_enabled=True)
    worker._pipeline = pipeline
    await worker.run_lifi_once()
    while pipeline._tasks:
        await asyncio.gather(*list(pipeline._tasks.values()), return_exceptions=True)


class TestLifiSwapEndToEnd:
    async def test_quote_returns_lifi_venue_when_lp_dry(self, api_client, settings):
        _stub_quote_service(lifi_enabled=True)
        resp = await api_client.get("/v1/quote", params={
            "from_token_id": FROM_TOKEN, "to_token_id": TO_TOKEN,
            "from_amount": "1000000", "user_address": USER,
        })
        assert resp.status_code == 200
        assert resp.json()["venue"] == "lifi"

    async def test_flag_off_lp_dry_returns_400(self, api_client, settings):
        _stub_quote_service(lifi_enabled=False)
        resp = await api_client.get("/v1/quote", params={
            "from_token_id": FROM_TOKEN, "to_token_id": TO_TOKEN,
            "from_amount": "1000000", "user_address": USER,
        })
        assert resp.status_code == 400
        assert "Insufficient liquidity" in resp.json()["detail"]

    async def test_full_lifi_swap_completes_via_polling(self, api_client, settings):
        _stub_quote_service(lifi_enabled=True)
        pipeline = _stub_pipeline(settings)
        _stub_executor(settings)

        quote_resp = await api_client.get("/v1/quote", params={
            "from_token_id": FROM_TOKEN, "to_token_id": TO_TOKEN,
            "from_amount": "1000000", "user_address": USER,
        })
        quote_id = quote_resp.json()["quote_id"]

        swap_resp = await api_client.post("/v1/swap", json={
            "quote_id": quote_id,
            "input_nonce": 5, "input_signature": _sign_input(settings),
        })
        assert swap_resp.status_code == 200
        body = swap_resp.json()
        assert body["status"] == "scheduled"

        await _drain_background(pipeline)

        status_resp = await api_client.get(f"/v1/swap/{body['swap_id']}/status")
        assert status_resp.status_code == 200
        status = status_resp.json()
        assert status["status"] == "completed"
        assert status["to_amount_actual"] == "59940"
        privana = await pipeline._privana_factory()
        credit = privana.transfer_funds.await_args.args[0]
        assert credit.to_address.lower() == USER.lower()
        assert (credit.token_id, credit.amount, credit.nonce) == (TO_TOKEN, 59940, 70)
        pipeline.evm.send_transaction_request.assert_called_once()
        pipeline.evm.transfer.assert_called_once()

    async def test_lifi_failure_ends_refunded(self, api_client, settings):
        _stub_quote_service(lifi_enabled=True)
        pipeline = _stub_pipeline(settings, lifi_reverts=True)
        _stub_executor(settings)

        quote_resp = await api_client.get("/v1/quote", params={
            "from_token_id": FROM_TOKEN, "to_token_id": TO_TOKEN,
            "from_amount": "1000000", "user_address": USER,
        })
        quote_id = quote_resp.json()["quote_id"]

        swap_resp = await api_client.post("/v1/swap", json={
            "quote_id": quote_id,
            "input_nonce": 5, "input_signature": _sign_input(settings),
        })
        assert swap_resp.status_code == 200
        assert swap_resp.json()["status"] == "scheduled"

        await _drain_background(pipeline)

        status_resp = await api_client.get(f"/v1/swap/{swap_resp.json()['swap_id']}/status")
        assert status_resp.json()["status"] == "refunded"
        privana = await pipeline._privana_factory()
        refund = privana.transfer_funds.await_args.args[0]
        assert refund.to_address.lower() == USER.lower()
        assert (refund.token_id, refund.amount, refund.nonce) == (FROM_TOKEN, 1000000, 70)
        pipeline.evm.send_transaction_request.assert_called_once()
