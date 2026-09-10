import time
from unittest.mock import AsyncMock, MagicMock, patch

from eth_account import Account
from web3 import Web3

from src.core.db import db_write
from src.core.eip712 import sign_transfer
from src.models.common import Balance
from src.models.swap import SwapRecord, SwapStatus

USER_SK = "0x" + "11" * 32
USER_ADDRESS = Account.from_key(USER_SK).address
FROM_TOKEN = "0xaaaa"
TO_TOKEN = "0xbbbb"
FROM_AMOUNT = "1000000"
TO_AMOUNT = "44000000000000000"
TX_HASH = "0x" + "ff" * 32

SUFFICIENT_BALANCE = Balance(
    user_address="0xlp", token_id=TO_TOKEN, balance="999999999999999999999"
)


def _make_pipeline(settings):
    from src.services.swap.internal import InternalSwap

    accounting = AsyncMock()
    accounting.get_transfer_nonce = AsyncMock(return_value=0)
    accounting.get_lp_balance = AsyncMock(return_value=SUFFICIENT_BALANCE)

    sapphire = MagicMock()
    sapphire.execute_contract_call = MagicMock(return_value=TX_HASH)

    pipeline = InternalSwap(accounting=accounting, sapphire=sapphire)
    pipeline.settings = settings
    return pipeline


def _input_signature(settings):
    return sign_transfer(
        private_key=USER_SK,
        chain_id=settings.accounting_chain_id,
        verifying_contract=settings.accounting_contract_address,
        to_address=settings.liquidity_provider_address,
        token_id=FROM_TOKEN,
        amount=int(FROM_AMOUNT),
        nonce=0,
    )


def _scheduled_swap(test_db, settings, swap_id="s_int"):
    """A swap row as the executor queues it, before the pipeline picks it up."""
    now = int(time.time())
    db_write(
        test_db,
        """INSERT INTO swaps
           (id, quote_id, user_address, from_token_id, to_token_id, from_amount,
            to_amount_estimate, input_nonce, input_signature, status, venue,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (swap_id, "q_int", USER_ADDRESS.lower(), FROM_TOKEN, TO_TOKEN, FROM_AMOUNT,
         TO_AMOUNT, 0, _input_signature(settings), SwapStatus.SCHEDULED.value,
         "internal", now, now),
    )
    row = test_db.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
    return SwapRecord(**dict(row))


class TestExecuteSwap:
    async def test_successful_swap(self, test_db, settings):
        pipeline = _make_pipeline(settings)
        swap = _scheduled_swap(test_db, settings)

        await pipeline.execute_swap(swap)

        result = pipeline._get_swap(swap.id)
        assert result.status == SwapStatus.COMPLETED.value
        assert result.swap_tx_hash == TX_HASH

    async def test_broadcast_failure_fails_the_swap(self, test_db, settings):
        pipeline = _make_pipeline(settings)
        pipeline.sapphire.execute_contract_call = MagicMock(
            side_effect=RuntimeError("tx reverted")
        )
        swap = _scheduled_swap(test_db, settings)

        await pipeline.execute_swap(swap)

        result = pipeline._get_swap(swap.id)
        assert result.status == SwapStatus.FAILED.value
        assert "reverted" in result.error.lower()

    async def test_swap_that_would_revert_is_not_broadcast(self, test_db, settings):
        pipeline = _make_pipeline(settings)
        pipeline.sapphire.simulate_contract_call = MagicMock(
            side_effect=RuntimeError("execution reverted: InsufficientBalance")
        )
        swap = _scheduled_swap(test_db, settings)

        await pipeline.execute_swap(swap)

        result = pipeline._get_swap(swap.id)
        assert result.status == SwapStatus.FAILED.value
        assert result.error is not None
        pipeline.sapphire.execute_contract_call.assert_not_called()

    async def test_simulation_runs_before_execution(self, test_db, settings):
        pipeline = _make_pipeline(settings)
        calls = []
        pipeline.sapphire.simulate_contract_call = MagicMock(
            side_effect=lambda **kw: calls.append("simulate")
        )
        pipeline.sapphire.execute_contract_call = MagicMock(
            side_effect=lambda **kw: calls.append("execute") or TX_HASH
        )
        swap = _scheduled_swap(test_db, settings)

        await pipeline.execute_swap(swap)

        assert calls == ["simulate", "execute"]

    async def test_insufficient_liquidity_fails_before_signing(self, test_db, settings):
        pipeline = _make_pipeline(settings)
        pipeline.accounting.get_lp_balance = AsyncMock(
            return_value=Balance(user_address="0xlp", token_id=TO_TOKEN, balance="1")
        )
        swap = _scheduled_swap(test_db, settings)

        await pipeline.execute_swap(swap)

        result = pipeline._get_swap(swap.id)
        assert result.status == SwapStatus.FAILED.value
        assert "Insufficient liquidity" in result.error
        pipeline.sapphire.execute_contract_call.assert_not_called()

    async def test_passes_correct_params_to_sapphire(self, test_db, settings):
        pipeline = _make_pipeline(settings)
        swap = _scheduled_swap(test_db, settings)

        await pipeline.execute_swap(swap)

        call_kwargs = pipeline.sapphire.execute_contract_call.call_args
        assert call_kwargs.kwargs["function_name"] == "swap"
        swap_args = call_kwargs.kwargs["args"]
        assert swap_args[0] == Web3.to_checksum_address(USER_ADDRESS)
        assert swap_args[1] == bytes.fromhex("aaaa")
        assert swap_args[2] == int(FROM_AMOUNT)
        assert swap_args[3] == 0
        assert swap_args[4] == bytes.fromhex(_input_signature(settings)[2:])
        assert swap_args[5] == bytes.fromhex("bbbb")
        assert swap_args[6] == int(TO_AMOUNT)
        assert swap_args[7] == 0

    async def test_signs_output_transfer_with_lp_key(self, test_db, settings):
        pipeline = _make_pipeline(settings)
        pipeline.accounting.get_transfer_nonce = AsyncMock(return_value=7)
        swap = _scheduled_swap(test_db, settings)

        with patch(
            "src.services.swap.internal.sign_transfer",
            return_value="0x" + "cc" * 65,
        ) as mock_sign:
            await pipeline.execute_swap(swap)

        mock_sign.assert_called_once_with(
            private_key=settings.liquidity_provider_secret_key,
            chain_id=settings.accounting_chain_id,
            verifying_contract=settings.accounting_contract_address,
            to_address=USER_ADDRESS,
            token_id=TO_TOKEN,
            amount=int(TO_AMOUNT),
            nonce=7,
        )
        row = test_db.execute(
            "SELECT output_nonce, output_signature FROM swaps WHERE id = ?", (swap.id,)
        ).fetchone()
        assert row["output_nonce"] == 7
        assert row["output_signature"] == "0x" + "cc" * 65

    async def test_retries_swap_interrupted_mid_execution(self, test_db, settings):
        pipeline = _make_pipeline(settings)
        swap = _scheduled_swap(test_db, settings)
        pipeline._update_swap(swap.id, status=SwapStatus.EXECUTING.value)

        await pipeline.execute_swap(pipeline._get_swap(swap.id))

        result = pipeline._get_swap(swap.id)
        assert result.status == SwapStatus.COMPLETED.value
        assert result.swap_tx_hash == TX_HASH
