import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_account import Account
from web3 import Web3

from src.core.eip712 import sign_transfer
from src.models.common import Balance
from src.models.swap import SwapStatus

USER_SK = "0x" + "11" * 32
USER_ADDRESS = Account.from_key(USER_SK).address

SUFFICIENT_BALANCE = Balance(
    user_address="0xlp", token_id="0xbbbb", balance="999999999999999999999"
)


def _make_executor(settings):
    with patch("src.services.swap.executor.get_accounting_client") as mock_acct, \
         patch("src.services.swap.executor.get_sapphire_client") as mock_saph, \
         patch("src.services.swap.executor.load_settings") as mock_settings:
        mock_settings.return_value = settings

        acct = AsyncMock()
        acct.get_transfer_nonce = AsyncMock(return_value=0)
        acct.get_lp_balance = AsyncMock(return_value=SUFFICIENT_BALANCE)
        mock_acct.return_value = acct

        saph = MagicMock()
        saph.execute_contract_call = MagicMock(return_value="0x" + "ff" * 32)
        mock_saph.return_value = saph

        from src.services.swap.executor import SwapExecutor
        executor = SwapExecutor()
        return executor


class TestSwapStatus:
    def test_six_states(self):
        assert len(SwapStatus) == 6


class TestValidateQuote:
    def test_expired_quote_raises(self, test_db, settings, insert_quote):
        insert_quote("expired_q", expires_at=int(time.time()) - 10, user_address="0xuser")
        executor = _make_executor(settings)
        with pytest.raises(ValueError, match="Quote has expired"):
            executor._validate_quote("expired_q")

    def test_missing_quote_raises(self, test_db, settings):
        executor = _make_executor(settings)
        with pytest.raises(ValueError, match="Quote not found"):
            executor._validate_quote("nonexistent")

    def test_valid_quote_returns_dict(self, test_db, settings, insert_quote):
        user = "0xd8991364507fafC256EafF950d28618735753476"
        insert_quote(
            "q_valid",
            user_address=user.lower(),
            from_token_id="0xaaaa",
            to_token_id="0xbbbb",
            from_amount="1000000",
            to_amount_gross="45000000000000000",
            to_amount_estimate="44000000000000000",
            to_amount_min="43000000000000000",
            route_tool="okx",
        )
        executor = _make_executor(settings)
        result = executor._validate_quote("q_valid")
        assert isinstance(result, dict)
        assert result["id"] == "q_valid"


class TestExecuteSwap:
    @pytest.fixture
    def user_address(self):
        return USER_ADDRESS

    @pytest.fixture
    def input_signature(self, settings):
        return sign_transfer(
            private_key=USER_SK,
            chain_id=settings.accounting_chain_id,
            verifying_contract=settings.accounting_contract_address,
            to_address=settings.liquidity_provider_address,
            token_id="0xaaaa",
            amount=1000000,
            nonce=0,
        )

    async def test_signer_mismatch_rejected(
        self, test_db, settings, insert_quote, user_address, input_signature
    ):
        insert_quote(
            "q_stranger",
            user_address="0x" + "9" * 40,
            from_token_id="0xaaaa",
            to_token_id="0xbbbb",
            from_amount="1000000",
            to_amount_gross="45000000000000000",
            to_amount_estimate="44000000000000000",
            to_amount_min="43000000000000000",
            route_tool="okx",
        )
        executor = _make_executor(settings)
        with pytest.raises(ValueError, match="Quote was not created for this user"):
            await executor.execute_swap("q_stranger", 0, input_signature)

    async def test_successful_swap(self, test_db, settings, insert_quote, user_address, input_signature):
        insert_quote(
            "q_success",
            user_address=user_address.lower(),
            from_token_id="0xaaaa",
            to_token_id="0xbbbb",
            from_amount="1000000",
            to_amount_gross="45000000000000000",
            to_amount_estimate="44000000000000000",
            to_amount_min="43000000000000000",
            route_tool="okx",
        )
        executor = _make_executor(settings)
        result = await executor.execute_swap("q_success", 0, input_signature)
        assert result.status == SwapStatus.COMPLETED.value
        assert result.swap_tx_hash == "0x" + "ff" * 32

    async def test_failed_swap(self, test_db, settings, insert_quote, user_address, input_signature):
        insert_quote(
            "q_fail",
            user_address=user_address.lower(),
            from_token_id="0xaaaa",
            to_token_id="0xbbbb",
            from_amount="1000000",
            to_amount_gross="45000000000000000",
            to_amount_estimate="44000000000000000",
            to_amount_min="43000000000000000",
            route_tool="okx",
        )
        executor = _make_executor(settings)
        executor.sapphire.execute_contract_call = MagicMock(
            side_effect=RuntimeError("tx reverted")
        )
        result = await executor.execute_swap("q_fail", 0, input_signature)
        assert result.status == SwapStatus.FAILED.value
        assert "reverted" in result.error.lower()

    async def test_swap_that_would_revert_is_rejected_before_broadcast(
        self, test_db, settings, insert_quote, user_address, input_signature
    ):
        insert_quote(
            "q_sim_revert",
            user_address=user_address.lower(),
            from_token_id="0xaaaa",
            to_token_id="0xbbbb",
            from_amount="1000000",
            to_amount_gross="45000000000000000",
            to_amount_estimate="44000000000000000",
            to_amount_min="43000000000000000",
            route_tool="okx",
        )
        executor = _make_executor(settings)
        executor.sapphire.simulate_contract_call = MagicMock(
            side_effect=RuntimeError("execution reverted: InsufficientBalance")
        )

        with pytest.raises(ValueError, match="would revert on-chain"):
            await executor.execute_swap("q_sim_revert", 0, input_signature)

        executor.sapphire.execute_contract_call.assert_not_called()

    async def test_simulation_runs_before_execution(
        self, test_db, settings, insert_quote, user_address, input_signature
    ):
        insert_quote(
            "q_sim_order",
            user_address=user_address.lower(),
            from_token_id="0xaaaa",
            to_token_id="0xbbbb",
            from_amount="1000000",
            to_amount_gross="45000000000000000",
            to_amount_estimate="44000000000000000",
            to_amount_min="43000000000000000",
            route_tool="okx",
        )
        executor = _make_executor(settings)
        calls = []
        executor.sapphire.simulate_contract_call = MagicMock(
            side_effect=lambda **kw: calls.append("simulate")
        )
        executor.sapphire.execute_contract_call = MagicMock(
            side_effect=lambda **kw: calls.append("execute") or ("0x" + "ff" * 32)
        )

        await executor.execute_swap("q_sim_order", 0, input_signature)

        assert calls == ["simulate", "execute"]

    async def test_insufficient_liquidity_raises_value_error(
        self, test_db, settings, insert_quote, user_address, input_signature
    ):
        insert_quote(
            "q_no_liq",
            user_address=user_address.lower(),
            from_token_id="0xaaaa",
            to_token_id="0xbbbb",
            from_amount="1000000",
            to_amount_gross="45000000000000000",
            to_amount_estimate="44000000000000000",
            to_amount_min="43000000000000000",
            route_tool="okx",
        )
        executor = _make_executor(settings)
        low_balance = Balance(user_address="0xlp", token_id="0xbbbb", balance="1")
        executor.accounting.get_lp_balance = AsyncMock(return_value=low_balance)
        with pytest.raises(ValueError, match="Insufficient liquidity"):
            await executor.execute_swap("q_no_liq", 0, input_signature)

    async def test_creates_swap_record_before_calling_sapphire(
        self, test_db, settings, insert_quote, user_address, input_signature
    ):
        insert_quote(
            "q_record",
            user_address=user_address.lower(),
            from_token_id="0xaaaa",
            to_token_id="0xbbbb",
            from_amount="1000000",
            to_amount_gross="45000000000000000",
            to_amount_estimate="44000000000000000",
            to_amount_min="43000000000000000",
            route_tool="okx",
        )
        executor = _make_executor(settings)

        original_execute = executor.sapphire.execute_contract_call

        def check_record_exists(*args, **kwargs):
            row = test_db.execute(
                "SELECT COUNT(*) as cnt FROM swaps WHERE quote_id = ?", ("q_record",)
            ).fetchone()
            assert row["cnt"] == 1
            return original_execute(*args, **kwargs)

        executor.sapphire.execute_contract_call = MagicMock(side_effect=check_record_exists)
        await executor.execute_swap("q_record", 0, input_signature)

    async def test_passes_correct_params_to_sapphire(
        self, test_db, settings, insert_quote, user_address, input_signature
    ):
        insert_quote(
            "q_params",
            user_address=user_address.lower(),
            from_token_id="0xaaaa",
            to_token_id="0xbbbb",
            from_amount="1000000",
            to_amount_gross="45000000000000000",
            to_amount_estimate="44000000000000000",
            to_amount_min="43000000000000000",
            route_tool="okx",
        )
        executor = _make_executor(settings)
        await executor.execute_swap("q_params", 0, input_signature)

        call_kwargs = executor.sapphire.execute_contract_call.call_args
        assert call_kwargs.kwargs["function_name"] == "swap"
        swap_args = call_kwargs.kwargs["args"]
        assert swap_args[0] == Web3.to_checksum_address(user_address)
        assert swap_args[1] == bytes.fromhex("aaaa")
        assert swap_args[2] == 1000000
        assert swap_args[3] == 0
        assert swap_args[5] == bytes.fromhex("bbbb")
        assert swap_args[6] == 44000000000000000
        assert swap_args[7] == 0

    async def test_signs_output_transfer_with_lp_key(
        self, test_db, settings, insert_quote, user_address, input_signature
    ):
        insert_quote(
            "q_sign",
            user_address=user_address.lower(),
            from_token_id="0xaaaa",
            to_token_id="0xbbbb",
            from_amount="1000000",
            to_amount_gross="45000000000000000",
            to_amount_estimate="44000000000000000",
            to_amount_min="43000000000000000",
            route_tool="okx",
        )

        with patch("src.services.swap.executor.get_accounting_client") as mock_acct, \
             patch("src.services.swap.executor.get_sapphire_client") as mock_saph, \
             patch("src.services.swap.executor.load_settings") as mock_settings, \
             patch("src.services.swap.executor.sign_transfer") as mock_sign:
            mock_settings.return_value = settings

            acct = AsyncMock()
            acct.get_transfer_nonce = AsyncMock(return_value=7)
            acct.get_lp_balance = AsyncMock(return_value=SUFFICIENT_BALANCE)
            mock_acct.return_value = acct

            saph = MagicMock()
            saph.execute_contract_call = MagicMock(return_value="0x" + "ff" * 32)
            mock_saph.return_value = saph

            mock_sign.return_value = "0x" + "cc" * 65

            from src.services.swap.executor import SwapExecutor
            executor = SwapExecutor()
            await executor.execute_swap("q_sign", 0, input_signature)

            mock_sign.assert_called_once_with(
                private_key=settings.liquidity_provider_secret_key,
                chain_id=settings.accounting_chain_id,
                verifying_contract=settings.accounting_contract_address,
                to_address=user_address,
                token_id="0xbbbb",
                amount=44000000000000000,
                nonce=7,
            )

    async def test_swap_lock_prevents_concurrent_nonce_reads(
        self, test_db, settings, insert_quote, user_address, input_signature
    ):
        insert_quote(
            "q_lock1",
            user_address=user_address.lower(),
            from_token_id="0xaaaa",
            to_token_id="0xbbbb",
            from_amount="1000000",
            to_amount_gross="45000000000000000",
            to_amount_estimate="44000000000000000",
            to_amount_min="43000000000000000",
            route_tool="okx",
        )
        insert_quote(
            "q_lock2",
            user_address=user_address.lower(),
            from_token_id="0xaaaa",
            to_token_id="0xbbbb",
            from_amount="1000000",
            to_amount_gross="45000000000000000",
            to_amount_estimate="44000000000000000",
            to_amount_min="43000000000000000",
            route_tool="okx",
        )

        executor = _make_executor(settings)

        nonce_call_times = []
        original_get_nonce = executor.accounting.get_transfer_nonce

        async def tracked_get_nonce(*args, **kwargs):
            nonce_call_times.append(time.monotonic())
            await asyncio.sleep(0.05)
            return await original_get_nonce(*args, **kwargs)

        executor.accounting.get_transfer_nonce = AsyncMock(side_effect=tracked_get_nonce)

        await asyncio.gather(
            executor.execute_swap("q_lock1", 0, input_signature),
            executor.execute_swap("q_lock2", 0, input_signature),
        )

        assert len(nonce_call_times) == 2
        assert abs(nonce_call_times[1] - nonce_call_times[0]) >= 0.04
