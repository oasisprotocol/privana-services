import time
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.config import load_settings

USDC_TOKEN_ID = "0x330ba47d00c7ce3018deee017b319fd7cc6473a2ddc9e6eba6ebb4207be15279"
POOL_ID_HEX = "0x" + "ab" * 32
POOL_ADDRESS = "0x152E6a7125665764a4F1F1df80E8f5D49Bf0239c"
USER_ADDRESS = "0xd8991364507FAfC256EafF950d28618735753476"
USER_WITHDRAW_SIG = "0x" + "cc" * 65
SIWE_TOKEN = "0x" + "ee" * 32


def _make_service(registry=None):
    settings = replace(
        load_settings(),
        earn_manager_contract_address="0x1111111111111111111111111111111111111111",
        liquidity_provider_secret_key="0x4c0883a69102937d6231471b5dbb6204fe512961708279f69e0f0fcbf24b5830",
        liquidity_provider_address=POOL_ADDRESS,
        accounting_contract_address="0xad3C76e4E621C0cfF7540479Ee9B0A945723A642",
        accounting_chain_id=23295,
    )

    with patch("src.services.earn.vault_service.load_settings") as mock_settings, \
         patch("src.services.earn.vault_service.get_sapphire_client") as mock_saph, \
         patch("src.services.earn.vault_service.get_accounting_client") as mock_acct:
        mock_settings.return_value = settings

        saph = MagicMock()
        w3 = MagicMock()
        contract = MagicMock()
        w3.eth.contract.return_value = contract
        saph.w3 = w3
        saph.execute_contract_call = MagicMock(return_value="0x" + "ff" * 32)
        mock_saph.return_value = saph

        acct = MagicMock()
        acct.get_transfer_nonce = AsyncMock(return_value=7)
        mock_acct.return_value = acct

        # Default the on-chain withdraw nonce to 0 so withdraw tests can pass
        # ``nonce=0`` without tripping the stale-nonce pre-flight check. Tests
        # that need a different value override
        # ``contract.functions.withdrawNonces.return_value.call.return_value``.
        contract.functions.withdrawNonces.return_value.call.return_value = 0

        from src.services.earn.vault_service import VaultService
        service = VaultService(registry=registry)
        service.contract = contract
        return service, contract, saph, acct


class TestGetPool:
    def test_returns_pool_data(self):
        service, contract, _, _ = _make_service()
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000,
            1050,
            True,
        )
        pool_id_bytes = bytes.fromhex(POOL_ID_HEX[2:])
        pool = service.get_pool(pool_id_bytes)
        assert pool["token_id"] == USDC_TOKEN_ID
        assert pool["pool_address"] == POOL_ADDRESS
        assert pool["total_shares"] == 1000
        assert pool["total_assets"] == 1050
        assert pool["active"] is True


class TestListPools:
    def test_returns_all_pools(self):
        service, contract, _, _ = _make_service()
        pool_id_bytes = bytes.fromhex(POOL_ID_HEX[2:])
        contract.functions.getPoolCount.return_value.call.return_value = 1
        contract.functions.poolIds.return_value.call.return_value = pool_id_bytes
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000,
            1050,
            True,
        )

        pools = service.list_pools()
        assert len(pools) == 1
        assert pools[0]["pool_id"] == POOL_ID_HEX
        assert pools[0]["token_id"] == USDC_TOKEN_ID

    def test_returns_empty_when_no_pools(self):
        service, contract, _, _ = _make_service()
        contract.functions.getPoolCount.return_value.call.return_value = 0
        assert service.list_pools() == []


class TestConvertFunctions:
    def test_convert_to_shares(self):
        service, contract, _, _ = _make_service()
        contract.functions.convertToShares.return_value.call.return_value = 952
        pool_id_bytes = bytes.fromhex(POOL_ID_HEX[2:])
        assert service.convert_to_shares(pool_id_bytes, 1000) == 952

    def test_convert_to_assets(self):
        service, contract, _, _ = _make_service()
        contract.functions.convertToAssets.return_value.call.return_value = 1050
        pool_id_bytes = bytes.fromhex(POOL_ID_HEX[2:])
        assert service.convert_to_assets(pool_id_bytes, 1000) == 1050


class TestDepositQuote:
    async def test_returns_quote(self):
        service, contract, _, acct = _make_service()
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000,
            1050,
            True,
        )
        contract.functions.convertToShares.return_value.call.return_value = 952

        quote = await service.get_deposit_quote(POOL_ID_HEX, "1000", USER_ADDRESS)
        assert quote["shares_estimate"] == "952"
        assert quote["pool_address"] == POOL_ADDRESS
        assert quote["transfer_nonce"] == 7
        assert quote["quote_id"]
        assert quote["expires_at"] > int(time.time())

    async def test_rejects_missing_pool(self):
        service, contract, _, _ = _make_service()
        contract.functions.pools.return_value.call.return_value = (
            b"\x00" * 32,
            "0x0000000000000000000000000000000000000000",
            0, 0, False,
        )
        with pytest.raises(ValueError, match="not found"):
            await service.get_deposit_quote(POOL_ID_HEX, "1000", USER_ADDRESS)

    async def test_rejects_inactive_pool(self):
        service, contract, _, _ = _make_service()
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, False,
        )
        with pytest.raises(ValueError, match="not active"):
            await service.get_deposit_quote(POOL_ID_HEX, "1000", USER_ADDRESS)


class TestDeposit:
    async def test_successful_deposit(self, test_db):
        service, contract, saph, _ = _make_service()
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        result = await service.deposit(
            POOL_ID_HEX, USER_ADDRESS, "1000", 5, "0x" + "aa" * 65
        )
        # shares_minted is None now: per-user state is private on the contract,
        # so the backend can't compute the delta. Clients read it themselves.
        assert result["shares_minted"] is None
        assert result["tx_hash"] == "0x" + "ff" * 32
        assert result["status"] == "completed"

        row = test_db.execute("SELECT * FROM earn_transactions").fetchone()
        assert row["operation"] == "deposit"
        assert row["signer_address"] == USER_ADDRESS.lower()
        assert row["nonce"] == 5
        assert row["signature"] == "0x" + "aa" * 65
        assert row["status"] == "completed"
        assert row["tx_hash"] == "0x" + "ff" * 32
        assert result["deposit_id"] == row["id"]

    async def test_failed_deposit_returns_failed_status(self, test_db):
        service, contract, saph, _ = _make_service()
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        contract.functions.userShares.return_value.call.return_value = 0
        saph.execute_contract_call.side_effect = RuntimeError("onchain revert")

        result = await service.deposit(
            POOL_ID_HEX, USER_ADDRESS, "1000", 5, "0x" + "aa" * 65
        )
        assert result["status"] == "failed"
        assert result["tx_hash"] is None
        assert result["error"] is not None

        row = test_db.execute("SELECT * FROM earn_transactions").fetchone()
        assert row["status"] == "failed"
        assert "onchain revert" in row["error"]
        # /v1/operations/unsettled reports this row's id as operation_id, so
        # deposit_id has to be that same id for the two endpoints to agree.
        assert result["deposit_id"] == row["id"]


class TestWithdraw:
    async def test_successful_withdraw(self, test_db):
        service, contract, saph, acct = _make_service()
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )

        with patch("src.services.earn.vault_service.sign_transfer", return_value="0x" + "bb" * 65):
            result = await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "500", 0, USER_WITHDRAW_SIG)

        assert result["status"] == "completed"
        assert result["tx_hash"] == "0x" + "ff" * 32
        # shares_burned is None: per-user state is private on the contract.
        assert result["shares_burned"] is None

        row = test_db.execute("SELECT * FROM earn_transactions").fetchone()
        assert row["operation"] == "withdraw"
        assert row["signer_address"] == POOL_ADDRESS.lower()
        assert row["nonce"] == 7
        assert row["status"] == "completed"
        assert result["withdraw_id"] == row["id"]

    async def test_failed_withdraw_returns_failed_status(self, test_db):
        service, contract, saph, acct = _make_service()
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        saph.execute_contract_call.side_effect = RuntimeError("insufficient funds for gas")

        with patch("src.services.earn.vault_service.sign_transfer", return_value="0x" + "bb" * 65):
            result = await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "500", 0, USER_WITHDRAW_SIG)

        assert result["status"] == "failed"
        assert result["tx_hash"] is None
        assert result["error"] == "Insufficient gas funds for transaction"

        row = test_db.execute("SELECT * FROM earn_transactions").fetchone()
        assert row["status"] == "failed"
        assert row["error"] == "Insufficient gas funds for transaction"
        assert result["withdraw_id"] == row["id"]


class TestExchangeRateZeroShares:
    async def test_deposit_quote_with_zero_shares_pool(self):
        service, contract, _, acct = _make_service()
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            0, 0, True,
        )
        contract.functions.convertToShares.return_value.call.return_value = 1000

        quote = await service.get_deposit_quote(POOL_ID_HEX, "1000", USER_ADDRESS)
        assert quote["exchange_rate"] == "1.0"

    @pytest.mark.asyncio
    async def test_balance_with_zero_shares_pool(self):
        service, contract, _, _ = _make_service()
        pool_id_bytes = bytes.fromhex(POOL_ID_HEX[2:])
        contract.functions.getPoolCount.return_value.call.return_value = 1
        contract.functions.poolIds.return_value.call.return_value = pool_id_bytes
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            0, 0, True,
        )
        contract.functions.getUserShares.return_value.call.return_value = 0

        balances = await service.get_all_balances(SIWE_TOKEN)
        assert balances == []


class TestGetAllBalances:
    @pytest.mark.asyncio
    async def test_returns_balances_for_user(self):
        service, contract, _, _ = _make_service()
        pool_id_bytes = bytes.fromhex(POOL_ID_HEX[2:])
        contract.functions.getPoolCount.return_value.call.return_value = 1
        contract.functions.poolIds.return_value.call.return_value = pool_id_bytes
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        contract.functions.getUserShares.return_value.call.return_value = 500
        contract.functions.convertToAssets.return_value.call.return_value = 525

        balances = await service.get_all_balances(SIWE_TOKEN)
        assert len(balances) == 1
        assert balances[0]["shares"] == "500"
        assert balances[0]["underlying_amount"] == "525"

    @pytest.mark.asyncio
    async def test_skips_zero_shares(self):
        service, contract, _, _ = _make_service()
        pool_id_bytes = bytes.fromhex(POOL_ID_HEX[2:])
        contract.functions.getPoolCount.return_value.call.return_value = 1
        contract.functions.poolIds.return_value.call.return_value = pool_id_bytes
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        contract.functions.getUserShares.return_value.call.return_value = 0

        balances = await service.get_all_balances(SIWE_TOKEN)
        assert balances == []


class TestStrategyRouting:
    async def test_deposit_routes_to_strategy(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.deposit_to_earn = AsyncMock()
        strategy.total_assets = AsyncMock(return_value=1050)
        strategy.idle_assets = AsyncMock(return_value=0)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, _, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        contract.functions.userShares.return_value.call.side_effect = [0, 952]

        result = await service.deposit(
            POOL_ID_HEX, USER_ADDRESS, "1000", 5, "0x" + "aa" * 65
        )

        assert result["status"] == "completed"
        strategy.deposit_to_earn.assert_awaited_once_with(1000)

    async def test_deposit_reports_undeployed_when_strategy_routing_fails(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.deposit_to_earn = AsyncMock(side_effect=RuntimeError("aave rpc down"))
        strategy.total_assets = AsyncMock(return_value=1050)
        strategy.idle_assets = AsyncMock(return_value=0)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, _, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        contract.functions.userShares.return_value.call.side_effect = [0, 952]

        result = await service.deposit(
            POOL_ID_HEX, USER_ADDRESS, "1000", 5, "0x" + "aa" * 65
        )

        # Shares were minted, so this is not a "failed" deposit, but the funds
        # never reached the strategy and must not be reported as settled.
        assert result["status"] == "undeployed"
        assert result["error"] is not None
        assert result["tx_hash"] is not None
        strategy.deposit_to_earn.assert_awaited_once()

        row = test_db.execute(
            "SELECT status, error FROM earn_transactions WHERE id = ?",
            (result["deposit_id"],),
        ).fetchone()
        assert row["status"] == "undeployed"
        assert row["error"] is not None

    async def test_rate_snapshot_pairs_assets_with_the_shares_of_one_instant(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.total_assets = AsyncMock(return_value=1800)
        strategy.idle_assets = AsyncMock(return_value=200)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, _, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1500, True,
        )

        assets, shares = await service.rate_snapshot(POOL_ID_HEX)

        # Deployed plus idle, so an undeployed deposit's shares stay backed.
        assert assets == 2000
        assert shares == 1000

    async def test_rate_snapshot_refuses_when_shares_move_mid_read(self):
        """A deposit landing between the share read and the asset read would
        pair new assets with old shares and invent a jump in value."""
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.total_assets = AsyncMock(return_value=2000)
        strategy.idle_assets = AsyncMock(return_value=0)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, _, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.side_effect = [
            (bytes.fromhex(USDC_TOKEN_ID[2:]), POOL_ADDRESS, 1000, 1000, True),
            (bytes.fromhex(USDC_TOKEN_ID[2:]), POOL_ADDRESS, 2000, 2000, True),
        ]

        assert await service.rate_snapshot(POOL_ID_HEX) is None

    async def test_rate_snapshot_is_none_when_the_strategy_cannot_be_read(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.total_assets = AsyncMock(side_effect=RuntimeError("rpc down"))
        strategy.idle_assets = AsyncMock(return_value=0)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, _, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )

        assert await service.rate_snapshot(POOL_ID_HEX) is None

    async def test_deposit_manual_strategy_skips_routing(self, test_db):
        service, contract, _, _ = _make_service()
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        contract.functions.userShares.return_value.call.side_effect = [0, 952]

        result = await service.deposit(
            POOL_ID_HEX, USER_ADDRESS, "1000", 5, "0x" + "aa" * 65
        )

        assert result["status"] == "completed"

    async def _spy_sync_lock_state(self, service):
        """Replace sync_total_assets with a spy that records whether the LP
        lock was held at each call, so a test can prove the sync is serialized
        with strategy movement rather than racing it (EA-Products C-0017).
        Returns a confirmed value so the deposit fail-closed guard passes."""
        held = []

        async def spy(pool_id_hex):
            held.append(service._lp_tx_lock.locked())
            return 1050

        service.sync_total_assets = spy
        return held

    async def test_deposit_syncs_under_the_lock(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.deposit_to_earn = AsyncMock()
        registry.register(POOL_ID_HEX, strategy)

        service, contract, _, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        held = await self._spy_sync_lock_state(service)

        await service.deposit(POOL_ID_HEX, USER_ADDRESS, "1000", 5, "0x" + "aa" * 65)

        assert held == [True]

    async def test_failed_withdraw_resyncs_under_the_lock(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.withdraw_from_earn = AsyncMock()
        strategy.deposit_to_earn = AsyncMock()
        registry.register(POOL_ID_HEX, strategy)

        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        sapphire.execute_contract_call.side_effect = RuntimeError("InsufficientShares")
        held = await self._spy_sync_lock_state(service)

        with patch("src.services.earn.vault_service.sign_transfer", return_value="0x" + "bb" * 65):
            result = await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "500", 0, USER_WITHDRAW_SIG)

        assert result["status"] == "failed"
        # Once before the reclaim, once after the rollback; both under the lock.
        assert held == [True, True]
        strategy.deposit_to_earn.assert_awaited_once_with(500)

    async def test_deposit_refuses_to_mint_when_aum_unconfirmed(self, test_db):
        """Fail closed: if the pool valuation cannot be confirmed, minting
        against a stale or manipulated denominator is refused (C-0017)."""
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.deposit_to_earn = AsyncMock()
        strategy.total_assets = AsyncMock(side_effect=RuntimeError("base rpc down"))
        strategy.idle_assets = AsyncMock(return_value=0)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, _, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )

        with pytest.raises(ValueError, match="could not be confirmed"):
            await service.deposit(POOL_ID_HEX, USER_ADDRESS, "1000", 5, "0x" + "aa" * 65)

        strategy.deposit_to_earn.assert_not_awaited()
        assert test_db.execute("SELECT COUNT(*) c FROM earn_transactions").fetchone()["c"] == 0

    async def test_failed_withdraw_resync_never_understates_the_denominator(self, test_db):
        """The exploit's finisher: a failed withdraw whose rollback also fails
        leaves the reclaimed funds idle. Backing is conserved (Aave + idle ==
        the original total), so the idle-inclusive resync must not push a
        denominator below it. An external-only resync would write the reduced
        Aave balance and inflate the next deposit (C-0017)."""
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.withdraw_from_earn = AsyncMock()
        # Rollback re-supply fails, so the reclaimed funds stay idle.
        strategy.deposit_to_earn = AsyncMock(side_effect=RuntimeError("rollback failed"))
        # Before the reclaim all 1000 is in Aave; after the failed rollback most
        # of it (800) is idle and only 200 remains in Aave.
        strategy.total_assets = AsyncMock(side_effect=[1000, 200])
        strategy.idle_assets = AsyncMock(side_effect=[0, 800])
        registry.register(POOL_ID_HEX, strategy)

        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1000, True,
        )

        def exec_call(**kwargs):
            if kwargs["function_name"] == "withdraw":
                raise RuntimeError("InsufficientShares")
            return "0x" + "ab" * 32

        sapphire.execute_contract_call.side_effect = exec_call

        with patch("src.services.earn.vault_service.sign_transfer", return_value="0x" + "bb" * 65):
            result = await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "800", 0, USER_WITHDRAW_SIG)

        assert result["status"] == "failed"
        # The resync ran after the rollback and read the idle funds too.
        assert strategy.idle_assets.await_count == 2
        # No sync ever pushed a denominator below the true 1000 backing.
        writes = [
            c.kwargs["args"][1]
            for c in sapphire.execute_contract_call.call_args_list
            if c.kwargs.get("function_name") == "syncTotalAssets"
        ]
        assert all(w >= 1000 for w in writes)

    async def test_withdraw_reclaims_from_strategy(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.withdraw_from_earn = AsyncMock()
        strategy.total_assets = AsyncMock(return_value=1050)
        strategy.idle_assets = AsyncMock(return_value=0)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, _, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        contract.functions.userShares.return_value.call.side_effect = [500, 500, 25]
        contract.functions.convertToAssets.return_value.call.return_value = 525

        with patch("src.services.earn.vault_service.sign_transfer", return_value="0x" + "bb" * 65):
            result = await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "500", 0, USER_WITHDRAW_SIG)

        assert result["status"] == "completed"
        strategy.withdraw_from_earn.assert_awaited_once_with(500)

    async def test_withdraw_strategy_failure_blocks_onchain_burn(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.withdraw_from_earn = AsyncMock(side_effect=RuntimeError("aave rpc down"))
        registry.register(POOL_ID_HEX, strategy)

        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        contract.functions.userShares.return_value.call.return_value = 500
        contract.functions.convertToAssets.return_value.call.return_value = 525

        with patch("src.services.earn.vault_service.sign_transfer", return_value="0x" + "bb" * 65):
            with pytest.raises(RuntimeError, match="aave rpc down"):
                await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "500", 0, USER_WITHDRAW_SIG)

        strategy.withdraw_from_earn.assert_awaited_once()
        sapphire.execute_contract_call.assert_not_called()

    async def test_withdraw_onchain_revert_resupplies_reclaimed_funds(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.withdraw_from_earn = AsyncMock()
        strategy.deposit_to_earn = AsyncMock()
        registry.register(POOL_ID_HEX, strategy)

        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        contract.functions.userShares.return_value.call.return_value = 500
        contract.functions.convertToAssets.return_value.call.return_value = 525
        sapphire.execute_contract_call.side_effect = RuntimeError("InvalidWithdrawSignature")

        with patch("src.services.earn.vault_service.sign_transfer", return_value="0x" + "bb" * 65):
            result = await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "500", 0, USER_WITHDRAW_SIG)

        assert result["status"] == "failed"
        strategy.withdraw_from_earn.assert_awaited_once_with(500)
        strategy.deposit_to_earn.assert_awaited_once_with(500)


class TestEffectiveTotalAssets:
    async def test_manual_strategy_returns_on_chain_value(self):
        service, _, _, _ = _make_service()

        assert await service.effective_total_assets(POOL_ID_HEX, 1050) == 1050

    async def test_active_strategy_overrides_with_atoken_balance(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.total_assets = AsyncMock(return_value=1100)
        registry.register(POOL_ID_HEX, strategy)

        service, _, _, _ = _make_service(registry=registry)

        assert await service.effective_total_assets(POOL_ID_HEX, 1000) == 1100

    async def test_strategy_failure_falls_back_to_on_chain(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.total_assets = AsyncMock(side_effect=RuntimeError("rpc down"))
        registry.register(POOL_ID_HEX, strategy)

        service, _, _, _ = _make_service(registry=registry)

        assert await service.effective_total_assets(POOL_ID_HEX, 1234) == 1234

    async def test_strategy_zero_balance_falls_back_to_on_chain(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.total_assets = AsyncMock(return_value=0)
        registry.register(POOL_ID_HEX, strategy)

        service, _, _, _ = _make_service(registry=registry)

        assert await service.effective_total_assets(POOL_ID_HEX, 500) == 500


class TestStrategyApyBpsSafe:
    async def test_manual_strategy_returns_zero(self):
        service, _, _, _ = _make_service()

        assert await service.strategy_apy_bps_safe(POOL_ID_HEX) == 0

    async def test_aave_strategy_returns_real_bps(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.get_apy_bps = AsyncMock(return_value=487)
        registry.register(POOL_ID_HEX, strategy)

        service, _, _, _ = _make_service(registry=registry)

        assert await service.strategy_apy_bps_safe(POOL_ID_HEX) == 487

    async def test_strategy_failure_degrades_to_zero(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.get_apy_bps = AsyncMock(side_effect=RuntimeError("rpc down"))
        registry.register(POOL_ID_HEX, strategy)

        service, _, _, _ = _make_service(registry=registry)

        # Failure must not crash the listing endpoint; surface 0 instead.
        assert await service.strategy_apy_bps_safe(POOL_ID_HEX) == 0


class TestSyncTotalAssets:
    async def test_manual_strategy_returns_on_chain_authoritative(self):
        service, contract, sapphire, _ = _make_service()
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1500, True,
        )

        result = await service.sync_total_assets(POOL_ID_HEX)

        # No external capital, so the on-chain total is already authoritative.
        assert result == 1500
        sapphire.execute_contract_call.assert_not_called()

    async def test_skips_when_backing_matches_on_chain(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.total_assets = AsyncMock(return_value=1400)
        strategy.idle_assets = AsyncMock(return_value=100)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1500, True,
        )

        result = await service.sync_total_assets(POOL_ID_HEX)

        # strategy 1400 + idle 100 == on-chain 1500, nothing to write.
        assert result == 1500
        sapphire.execute_contract_call.assert_not_called()

    async def test_writes_strategy_plus_idle_when_drifted(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.total_assets = AsyncMock(return_value=1700)
        strategy.idle_assets = AsyncMock(return_value=200)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1500, True,
        )

        result = await service.sync_total_assets(POOL_ID_HEX)

        # Idle funds back existing shares, so they must be in the denominator.
        assert result == 1900
        sapphire.execute_contract_call.assert_called_once()
        call_kwargs = sapphire.execute_contract_call.call_args.kwargs
        assert call_kwargs["function_name"] == "syncTotalAssets"
        assert call_kwargs["args"][1] == 1900

    async def test_refuses_to_zero_a_nonzero_denominator(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.total_assets = AsyncMock(return_value=0)
        strategy.idle_assets = AsyncMock(return_value=0)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1500, True,
        )

        result = await service.sync_total_assets(POOL_ID_HEX)

        assert result is None
        sapphire.execute_contract_call.assert_not_called()

    async def test_strategy_read_failure_returns_none(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.total_assets = AsyncMock(side_effect=RuntimeError("rpc down"))
        registry.register(POOL_ID_HEX, strategy)

        service, _, sapphire, _ = _make_service(registry=registry)

        result = await service.sync_total_assets(POOL_ID_HEX)

        assert result is None
        sapphire.execute_contract_call.assert_not_called()

    async def test_contract_failure_returns_none(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.total_assets = AsyncMock(return_value=1700)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1500, True,
        )
        sapphire.execute_contract_call.side_effect = RuntimeError("sapphire timeout")

        result = await service.sync_total_assets(POOL_ID_HEX)

        assert result is None

    async def test_zero_external_skips_sync(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.total_assets = AsyncMock(return_value=0)
        registry.register(POOL_ID_HEX, strategy)

        service, _, sapphire, _ = _make_service(registry=registry)

        result = await service.sync_total_assets(POOL_ID_HEX)

        assert result is None
        sapphire.execute_contract_call.assert_not_called()


class TestLiveAUMInResponses:
    async def test_deposit_quote_uses_live_aum(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.total_assets = AsyncMock(return_value=1100)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, _, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1000, True,
        )
        contract.functions.convertToShares.return_value.call.return_value = 909

        quote = await service.get_deposit_quote(POOL_ID_HEX, "1000", USER_ADDRESS)
        assert quote["exchange_rate"] == "1.1"

    async def test_get_all_balances_uses_live_aum(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.total_assets = AsyncMock(return_value=1200)
        strategy.idle_assets = AsyncMock(return_value=0)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, _, _ = _make_service(registry=registry)
        pool_id_bytes = bytes.fromhex(POOL_ID_HEX[2:])
        contract.functions.getPoolCount.return_value.call.return_value = 1
        contract.functions.poolIds.return_value.call.return_value = pool_id_bytes
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1000, True,
        )
        contract.functions.userShares.return_value.call.return_value = 500
        contract.functions.convertToAssets.return_value.call.return_value = 600

        balances = await service.get_all_balances(USER_ADDRESS)
        assert len(balances) == 1
        assert balances[0]["exchange_rate"] == "1.2"


class TestGetAllBalancesChange:
    def _seed_pool_contract(self, contract, total_assets=1050, total_shares=1000):
        pool_id_bytes = bytes.fromhex(POOL_ID_HEX[2:])
        contract.functions.getPoolCount.return_value.call.return_value = 1
        contract.functions.poolIds.return_value.call.return_value = pool_id_bytes
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            total_shares, total_assets, True,
        )
        contract.functions.getUserShares.return_value.call.return_value = 500
        contract.functions.convertToAssets.return_value.call.return_value = 525

    @pytest.mark.asyncio
    async def test_change_fields_populated_with_identity(self, test_db):
        import time as time_module

        from src.services.pool_rate_history import PoolRatePoint, store_point

        service, contract, _, _ = _make_service()
        self._seed_pool_contract(contract)
        # rate 1.0 a day ago vs 1.05 now
        store_point(
            POOL_ID_HEX,
            PoolRatePoint(int(time_module.time()) - 86400 - 21600, "1000", "1000"),
        )

        balances = await service.get_all_balances(
            SIWE_TOKEN, user_address="0x" + "d" * 40
        )
        assert len(balances) == 1
        assert balances[0]["change_24h"] == "25"
        assert balances[0]["change_24h_pct"] == "0.050000"

    @pytest.mark.asyncio
    async def test_change_fields_null_without_identity(self, test_db):
        import time as time_module

        from src.services.pool_rate_history import PoolRatePoint, store_point

        service, contract, _, _ = _make_service()
        self._seed_pool_contract(contract)
        store_point(
            POOL_ID_HEX,
            PoolRatePoint(int(time_module.time()) - 86400 - 21600, "1000", "1000"),
        )

        balances = await service.get_all_balances(SIWE_TOKEN)
        assert len(balances) == 1
        assert balances[0]["change_24h"] is None
        assert balances[0]["change_24h_pct"] is None

    @pytest.mark.asyncio
    async def test_change_fields_null_without_history(self, test_db):
        service, contract, _, _ = _make_service()
        self._seed_pool_contract(contract)

        balances = await service.get_all_balances(
            SIWE_TOKEN, user_address="0x" + "d" * 40
        )
        assert len(balances) == 1
        assert balances[0]["shares"] == "500"
        assert balances[0]["change_24h"] is None
        assert balances[0]["change_24h_pct"] is None
