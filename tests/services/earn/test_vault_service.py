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
BLOCK = 500


def _make_service(registry=None):
    settings = replace(
        load_settings(),
        earn_manager_contract_address="0x1111111111111111111111111111111111111111",
        liquidity_provider_secret_key="0x4c0883a69102937d6231471b5dbb6204fe512961708279f69e0f0fcbf24b5830",
        liquidity_provider_address=POOL_ADDRESS,
        # A correctly configured deployment signs for the account its pools
        # are created against.
        earn_pool_address=POOL_ADDRESS,
        accounting_contract_address="0xad3C76e4E621C0cfF7540479Ee9B0A945723A642",
        accounting_chain_id=23295,
    )

    with patch("src.services.earn.vault_service.load_settings") as mock_settings, \
         patch("src.services.earn.vault_service.get_pool_admin_sapphire_client") as mock_saph, \
         patch("src.services.earn.vault_service.get_accounting_client") as mock_acct:
        mock_settings.return_value = settings

        saph = MagicMock()
        w3 = MagicMock()
        contract = MagicMock()
        w3.eth.contract.return_value = contract
        saph.w3 = w3
        saph.w3_unwrapped = w3
        saph.execute_contract_call = MagicMock(return_value="0x" + "ff" * 32)
        # The earn flows broadcast and wait separately so a receipt timeout
        # still leaves a hash behind for the recovery pass to reconcile.
        # Broadcast delegates to execute_contract_call so a test that makes the
        # call revert still does, and call_args assertions keep working.
        saph.submit_contract_call = MagicMock(
            side_effect=lambda **kwargs: saph.execute_contract_call(**kwargs)
        )
        saph.wait_for_receipt = MagicMock(return_value={"status": 1, "blockNumber": BLOCK})
        w3.eth.get_block.side_effect = lambda n: {"timestamp": 1_000_000 + n}
        mock_saph.return_value = saph

        acct = MagicMock()
        acct.get_transfer_nonce = AsyncMock(return_value=7)
        acct.transfer_nonce = MagicMock(return_value=0)
        mock_acct.return_value = acct

        # Default the on-chain withdraw nonce to 0 so withdraw tests can pass
        # ``nonce=0`` without tripping the stale-nonce pre-flight check. Tests
        # that need a different value override
        # ``contract.functions.withdrawNonces.return_value.call.return_value``.
        contract.functions.withdrawNonces.return_value.call.return_value = 0

        # No protocol-owned seed by default, so the netting is a no-op and
        # every pre-seed expectation still reads as the plain backing figure.
        contract.functions.getSeededAssets.return_value.call.return_value = 0

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
    @staticmethod
    def _routing_strategy():
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.deposit_to_earn = AsyncMock()
        strategy.total_assets = AsyncMock(return_value=1050)
        strategy.idle_assets = AsyncMock(return_value=0)
        return strategy

    async def test_deposit_completes_without_waiting_on_the_strategy(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = self._routing_strategy()
        registry.register(POOL_ID_HEX, strategy)

        service, contract, _, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )

        result = await service.deposit(
            POOL_ID_HEX, USER_ADDRESS, "1000", 5, "0x" + "aa" * 65
        )

        # The funds stay on the pool's account for the idle deployer's next
        # round, so the depositor never waits on a bridge.
        assert result["status"] == "completed"
        assert result["error"] is None
        assert result["tx_hash"] is not None
        strategy.deposit_to_earn.assert_not_awaited()
        row = test_db.execute(
            "SELECT status, error FROM earn_transactions WHERE id = ?",
            (result["deposit_id"],),
        ).fetchone()
        assert row["status"] == "completed"
        assert row["error"] is None

    async def test_a_broken_bridge_does_not_touch_the_deposit(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = self._routing_strategy()
        strategy.deposit_to_earn = AsyncMock(side_effect=RuntimeError("bridge down"))
        registry.register(POOL_ID_HEX, strategy)

        service, contract, _, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )

        result = await service.deposit(
            POOL_ID_HEX, USER_ADDRESS, "1000", 5, "0x" + "aa" * 65
        )

        assert result["status"] == "completed"
        strategy.deposit_to_earn.assert_not_awaited()

    async def test_rate_snapshot_pairs_assets_with_the_shares_of_one_instant(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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
        with strategy movement rather than racing it.
        Returns a confirmed value so the deposit fail-closed guard passes."""
        held = []

        async def spy(pool_id_hex):
            held.append(service._pool_lock(POOL_ID_HEX).locked())
            return 1050

        service.sync_total_assets = spy
        return held

    async def test_deposit_syncs_under_the_lock(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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

    async def test_failed_withdraw_moves_nothing(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.idle_assets = AsyncMock(return_value=500)
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
        # Paid from the pool account, so a revert leaves nothing to put back.
        assert held == [True]
        strategy.withdraw_from_earn.assert_not_awaited()
        strategy.deposit_to_earn.assert_not_awaited()

    async def test_deposit_refuses_when_strategy_unhealthy(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=False)
        strategy.deposit_to_earn = AsyncMock()
        registry.register(POOL_ID_HEX, strategy)

        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )

        with pytest.raises(ValueError, match="unhealthy"):
            await service.deposit(POOL_ID_HEX, USER_ADDRESS, "1000", 5, "0x" + "aa" * 65)

        sapphire.execute_contract_call.assert_not_called()
        strategy.deposit_to_earn.assert_not_awaited()
        assert test_db.execute("SELECT COUNT(*) c FROM earn_transactions").fetchone()["c"] == 0

    async def test_deposit_refuses_to_mint_when_aum_unconfirmed(self, test_db):
        """Fail closed: if the pool valuation cannot be confirmed, minting
        against a stale or manipulated denominator is refused."""
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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
        Aave balance and inflate the next deposit."""
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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

    @staticmethod
    def _withdraw_service(idle):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "midas-mtbill"
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.withdraw_from_earn = AsyncMock()
        strategy.deposit_to_earn = AsyncMock()
        strategy.total_assets = AsyncMock(return_value=1050)
        if isinstance(idle, Exception):
            strategy.idle_assets = AsyncMock(side_effect=idle)
        else:
            strategy.idle_assets = AsyncMock(return_value=idle)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        contract.functions.userShares.return_value.call.side_effect = [500, 500, 25]
        contract.functions.convertToAssets.return_value.call.return_value = 525
        return service, strategy, sapphire

    async def test_withdraw_pays_from_the_pool_account(self, test_db):
        service, strategy, sapphire = self._withdraw_service(idle=500)

        with patch("src.services.earn.vault_service.sign_transfer", return_value="0x" + "bb" * 65):
            result = await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "500", 0, USER_WITHDRAW_SIG)

        assert result["status"] == "completed"
        strategy.withdraw_from_earn.assert_not_awaited()
        assert sapphire.execute_contract_call.call_args.kwargs["function_name"] == "withdraw"

    async def test_withdraw_short_of_liquidity_moves_nothing(self, test_db):
        from src.services.earn.strategies.base import LiquidityUnavailable

        service, strategy, sapphire = self._withdraw_service(idle=499)

        with pytest.raises(LiquidityUnavailable):
            await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "500", 0, USER_WITHDRAW_SIG)

        strategy.withdraw_from_earn.assert_not_awaited()
        assert all(
            c.kwargs["function_name"] != "withdraw"
            for c in sapphire.execute_contract_call.call_args_list
        )

    async def test_a_withdrawal_larger_than_the_pool_is_refused_not_held(self, test_db):
        service, _, sapphire = self._withdraw_service(idle=10**6)

        with pytest.raises(ValueError, match="larger than the whole pool"):
            await service.withdraw(POOL_ID_HEX, USER_ADDRESS, str(10**6 + 1), 0, USER_WITHDRAW_SIG)

        assert all(
            c.kwargs["function_name"] != "withdraw"
            for c in sapphire.execute_contract_call.call_args_list
        )

    async def test_an_unreadable_pool_balance_holds_the_withdrawal(self, test_db):
        from src.services.earn.strategies.base import LiquidityUnavailable

        service, strategy, sapphire = self._withdraw_service(idle=RuntimeError("accounting down"))
        service.sync_total_assets = AsyncMock(return_value=1050)

        # Waiting beats failing: the request keeps its signature and retries.
        with pytest.raises(LiquidityUnavailable):
            await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "500", 0, USER_WITHDRAW_SIG)

        sapphire.execute_contract_call.assert_not_called()

    async def test_withdraw_cannot_spend_funds_promised_to_a_bridge(self, test_db):
        from src.services.earn.strategies.base import LiquidityUnavailable

        service, _, _ = self._withdraw_service(idle=800)
        service._held[POOL_ID_HEX] = 400

        with pytest.raises(LiquidityUnavailable):
            await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "500", 0, USER_WITHDRAW_SIG)

        assert await service.withdrawal_payable(POOL_ID_HEX, 400)

    async def test_withdraw_skips_the_sync_while_a_bridge_is_running(self, test_db):
        service, _, _ = self._withdraw_service(idle=800)
        service._held[POOL_ID_HEX] = 100
        service.sync_total_assets = AsyncMock(return_value=None)
        service.get_seeded_assets = MagicMock(return_value=5_000)

        with patch("src.services.earn.vault_service.sign_transfer", return_value="0x" + "bb" * 65):
            result = await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "500", 0, USER_WITHDRAW_SIG)

        # The reading would be short by the bridge; the contract's figure holds.
        assert result["status"] == "completed"
        service.sync_total_assets.assert_not_awaited()

    async def test_deposit_skips_the_sync_while_a_bridge_is_running(self, test_db):
        service, _, _ = self._withdraw_service(idle=0)
        service._held[POOL_ID_HEX] = 100
        service.sync_total_assets = AsyncMock(return_value=None)

        result = await service.deposit(POOL_ID_HEX, USER_ADDRESS, "1000", 5, "0x" + "aa" * 65)

        assert result["status"] == "completed"
        service.sync_total_assets.assert_not_awaited()

    async def test_manual_pools_always_pay_out(self, test_db):
        service, _, _, _ = _make_service()

        assert await service.withdrawal_payable(POOL_ID_HEX, 10**30)


class TestEffectiveTotalAssets:
    async def test_manual_strategy_returns_on_chain_value(self):
        service, _, _, _ = _make_service()

        assert await service.effective_total_assets(POOL_ID_HEX, 1050) == 1050

    async def test_active_strategy_overrides_with_atoken_balance(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.total_assets = AsyncMock(return_value=1100)
        strategy.idle_assets = AsyncMock(return_value=0)
        registry.register(POOL_ID_HEX, strategy)

        service, _, _, _ = _make_service(registry=registry)

        assert await service.effective_total_assets(POOL_ID_HEX, 1000) == 1100

    async def test_idle_funds_count_toward_live_aum(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.total_assets = AsyncMock(return_value=1100)
        strategy.idle_assets = AsyncMock(return_value=200)
        registry.register(POOL_ID_HEX, strategy)

        service, _, _, _ = _make_service(registry=registry)

        assert await service.effective_total_assets(POOL_ID_HEX, 1000) == 1300

    async def test_strategy_failure_falls_back_to_on_chain(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.total_assets = AsyncMock(side_effect=RuntimeError("rpc down"))
        registry.register(POOL_ID_HEX, strategy)

        service, _, _, _ = _make_service(registry=registry)

        assert await service.effective_total_assets(POOL_ID_HEX, 1234) == 1234

    async def test_strategy_zero_balance_falls_back_to_on_chain(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.total_assets = AsyncMock(return_value=0)
        strategy.idle_assets = AsyncMock(return_value=0)
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
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.get_apy_bps = AsyncMock(return_value=487)
        registry.register(POOL_ID_HEX, strategy)

        service, _, _, _ = _make_service(registry=registry)

        assert await service.strategy_apy_bps_safe(POOL_ID_HEX) == 487

    async def test_strategy_failure_degrades_to_zero(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.get_apy_bps = AsyncMock(side_effect=RuntimeError("rpc down"))
        registry.register(POOL_ID_HEX, strategy)

        service, _, _, _ = _make_service(registry=registry)

        # Failure must not crash the listing endpoint; surface 0 instead.
        assert await service.strategy_apy_bps_safe(POOL_ID_HEX) == 0


class TestSeededLiquidity:
    @staticmethod
    def _registry_with(total_assets: int, idle: int):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.total_assets = AsyncMock(return_value=total_assets)
        strategy.idle_assets = AsyncMock(return_value=idle)
        registry.register(POOL_ID_HEX, strategy)
        return registry

    def test_seed_comes_off_the_top_leaving_only_user_assets(self):
        service, _, _, _ = _make_service()
        # 100k seed, 10k of user principal, 400 of yield on the whole balance.
        assert service._net_of_seed(110_400, 100_000, total_shares=10_000) == 10_400

    def test_users_absorb_a_shortfall_before_the_seed_does(self):
        service, _, _, _ = _make_service()
        # Backing falls 10 below the combined position: the seed stays whole.
        assert service._net_of_seed(90, 50, total_shares=10) == 40

    def test_user_assets_clamp_at_zero_rather_than_going_negative(self):
        service, _, _, _ = _make_service()
        assert service._net_of_seed(30, 50, total_shares=10) == 0

    def test_nothing_is_user_backed_before_the_first_deposit(self):
        service, _, _, _ = _make_service()
        # Yield earned while no shares exist stays with the seed.
        assert service._net_of_seed(100_400, 100_000, total_shares=0) == 0

    def test_an_unseeded_pool_is_left_exactly_as_it_was(self):
        service, _, _, _ = _make_service()
        # No seed, no shares, idle balance present: must stay the plain
        # backing figure, never be reported as zero and never be recorded as
        # protocol principal.
        assert service._net_of_seed(1_500, 0, total_shares=0) == 1_500
        assert service._net_of_seed(1_500, 0, total_shares=10) == 1_500

    async def test_sync_never_invents_seed_on_a_pool_that_has_none(self):
        registry = self._registry_with(total_assets=1_400, idle=100)
        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            0, 0, True,
        )
        contract.functions.getSeededAssets.return_value.call.return_value = 0

        await service.sync_total_assets(POOL_ID_HEX)

        written = [
            c.kwargs["function_name"] for c in sapphire.execute_contract_call.call_args_list
        ]
        assert "setSeededAssets" not in written

    async def test_sync_writes_backing_net_of_seed(self):
        registry = self._registry_with(total_assets=110_000, idle=400)
        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            10_000, 10_000, True,
        )
        contract.functions.getSeededAssets.return_value.call.return_value = 100_000

        result = await service.sync_total_assets(POOL_ID_HEX)

        assert result == 10_400
        assert sapphire.execute_contract_call.call_args.kwargs["args"][1] == 10_400

    async def test_sync_rolls_pre_deposit_yield_into_the_seed_baseline(self):
        registry = self._registry_with(total_assets=100_400, idle=0)
        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            0, 0, True,
        )
        contract.functions.getSeededAssets.return_value.call.return_value = 100_000

        result = await service.sync_total_assets(POOL_ID_HEX)

        # The 400 of yield becomes seed, not assets the first depositor finds
        # already on the books.
        assert result == 0
        call = sapphire.execute_contract_call.call_args
        assert call.kwargs["function_name"] == "setSeededAssets"
        assert call.kwargs["args"][1] == 100_400

    async def test_live_aum_reports_user_assets_not_the_whole_balance(self):
        registry = self._registry_with(total_assets=110_000, idle=400)
        service, contract, _, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            10_000, 10_000, True,
        )
        contract.functions.getSeededAssets.return_value.call.return_value = 100_000

        assert await service.effective_total_assets(POOL_ID_HEX, 10_000) == 10_400


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
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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

    async def test_refuses_a_large_unexplained_drop(self):
        """A partially credited reclaim reads as a big dip in backing. Writing
        it would hand the next deposit a false low denominator, so the sync
        must refuse rather than pass the transient through on-chain."""
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.total_assets = AsyncMock(return_value=800)
        strategy.idle_assets = AsyncMock(return_value=0)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1000, True,
        )

        result = await service.sync_total_assets(POOL_ID_HEX)

        assert result is None
        sapphire.execute_contract_call.assert_not_called()

    async def test_small_drop_within_tolerance_still_syncs(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.total_assets = AsyncMock(return_value=995)
        strategy.idle_assets = AsyncMock(return_value=0)
        registry.register(POOL_ID_HEX, strategy)

        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1000, True,
        )

        result = await service.sync_total_assets(POOL_ID_HEX)

        assert result == 995
        call_kwargs = sapphire.execute_contract_call.call_args.kwargs
        assert call_kwargs["args"][1] == 995

    async def test_strategy_read_failure_returns_none(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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
        # Pool totals scaled so the contract's virtual offsets give exact
        # rates: 1.0 at the anchor, 1.05 now.
        self._seed_pool_contract(contract, total_assets=1_049_999_999,
                                 total_shares=999_000_000)
        store_point(
            POOL_ID_HEX,
            PoolRatePoint(
                int(time_module.time()) - 86400 - 21600,
                "999999999", "999000000",
            ),
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


class TestReceiptUnknown:
    """An unread receipt leaves the row pending for recovery."""

    async def test_deposit_is_left_pending_with_its_hash(self, test_db):
        service, contract, saph, _ = _make_service()
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]), POOL_ADDRESS, 1000, 1050, True,
        )
        saph.wait_for_receipt.side_effect = TimeoutError("receipt wait timed out")

        result = await service.deposit(POOL_ID_HEX, USER_ADDRESS, "105", 5, "0x" + "aa" * 65)

        assert result["status"] == "pending"
        row = test_db.execute("SELECT * FROM earn_transactions").fetchone()
        assert row["status"] == "pending"
        assert row["tx_hash"] == "0x" + "ff" * 32
        assert row["error"] is None

    async def test_withdraw_is_left_pending_without_a_rollback(self, test_db):
        service, contract, saph, _ = _make_service()
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]), POOL_ADDRESS, 1000, 1050, True,
        )
        saph.wait_for_receipt.side_effect = TimeoutError("receipt wait timed out")
        service._rollback_reclaim = AsyncMock()

        with patch(
            "src.services.earn.vault_service.sign_transfer",
            return_value="0x" + "bb" * 65,
        ):
            result = await service.withdraw(
                POOL_ID_HEX, USER_ADDRESS, "420", 0, USER_WITHDRAW_SIG
            )

        assert result["status"] == "pending"
        service._rollback_reclaim.assert_not_awaited()
        row = test_db.execute(
            "SELECT * FROM earn_transactions WHERE operation = 'withdraw'"
        ).fetchone()
        assert row["status"] == "pending"
        assert row["tx_hash"] == "0x" + "ff" * 32


class TestGetAllBalancesEarned:
    def _seed(self, contract):
        contract.functions.getPoolCount.return_value.call.return_value = 1
        contract.functions.poolIds.return_value.call.return_value = bytes.fromhex(
            POOL_ID_HEX[2:]
        )
        # Pool totalShares must equal everything the ledger accounts for, or
        # the completeness check correctly refuses to report a figure.
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]), POOL_ADDRESS, 100, 1050, True,
        )
        contract.functions.getUserShares.return_value.call.return_value = 100
        # convertToAssets is authoritative for position value (virtual offsets).
        contract.functions.convertToAssets.return_value.call.return_value = 105

    def _deposit_row(self, user):
        from src.core.db import db_write, get_db
        db_write(
            get_db(),
            """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, status, created_at, updated_at,
                shares_delta)
               VALUES ('tx-1', 'deposit', ?, ?, '0xtok', '100', '0xsig', 0,
                       '0xsig', 'completed', 100, 100, '100')""",
            (POOL_ID_HEX, user.lower()),
        )

    @pytest.mark.asyncio
    async def test_earned_populated_from_ledger(self, test_db):
        service, contract, _, _ = _make_service()
        self._seed(contract)
        user = "0x" + "d" * 40
        self._deposit_row(user)

        balances = await service.get_all_balances(SIWE_TOKEN, user_address=user)
        assert balances[0]["earned_active"] == "5"
        assert balances[0]["earned_active_status"] == "ok"
        assert balances[0]["cost_basis"] == "100"
        assert balances[0]["deposit_count"] == 1
        assert balances[0]["first_deposit_at"] == 100

    @pytest.mark.asyncio
    async def test_earned_unsupported_without_identity(self, test_db):
        service, contract, _, _ = _make_service()
        self._seed(contract)
        self._deposit_row("0x" + "d" * 40)

        balances = await service.get_all_balances(SIWE_TOKEN)
        assert balances[0]["earned_active"] is None
        assert balances[0]["earned_active_status"] == "unsupported"

    @pytest.mark.asyncio
    async def test_earned_incomplete_without_ledger_rows(self, test_db):
        service, contract, _, _ = _make_service()
        self._seed(contract)

        balances = await service.get_all_balances(
            SIWE_TOKEN, user_address="0x" + "d" * 40
        )
        assert balances[0]["earned_active"] is None
        assert balances[0]["earned_active_status"] == "ledger_incomplete"


class TestSeedAwareQuotesAndExits:
    @staticmethod
    def _service(*, external, idle, seeded, shares, on_chain_assets, sync_fails=False):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "midas-mtbill"
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.total_assets = AsyncMock(return_value=external)
        strategy.idle_assets = AsyncMock(return_value=idle)
        strategy.withdraw_from_earn = AsyncMock()
        strategy.deposit_to_earn = AsyncMock()
        registry.register(POOL_ID_HEX, strategy)

        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            shares, on_chain_assets, True,
        )
        contract.functions.getSeededAssets.return_value.call.return_value = seeded
        contract.functions.convertToShares.return_value.call.return_value = 952
        if sync_fails:
            sapphire.execute_contract_call.side_effect = RuntimeError("sync tx failed")
        return service, strategy, sapphire

    async def test_quote_prices_against_user_assets_not_the_seed(self):
        # 100k seed, 10k of user principal, 400 of yield on the whole balance.
        service, _, _ = self._service(
            external=110_400, idle=0, seeded=100_000, shares=10_000, on_chain_assets=10_400,
        )

        quote = await service.get_deposit_quote(POOL_ID_HEX, "1000", USER_ADDRESS)

        # Gross would read 11.04 per share; only 10,400 backs the shares.
        assert quote["exchange_rate"] == "1.04"

    async def test_quote_on_an_unseeded_pool_is_unchanged(self):
        service, _, _ = self._service(
            external=1_050, idle=0, seeded=0, shares=1_000, on_chain_assets=1_050,
        )

        quote = await service.get_deposit_quote(POOL_ID_HEX, "1000", USER_ADDRESS)

        assert quote["exchange_rate"] == "1.05"

    async def test_a_seeded_pool_refuses_to_exit_on_an_unconfirmed_valuation(self, test_db):
        # Backing fell far enough that the drop guard refuses to write it, so
        # the recorded denominator no longer holds.
        service, strategy, _ = self._service(
            external=105, idle=0, seeded=100, shares=10, on_chain_assets=110,
        )

        with pytest.raises(ValueError, match="could not be confirmed"):
            await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "10", 0, USER_WITHDRAW_SIG)

        # Nothing was reclaimed, so no user was paid out of seed principal.
        strategy.withdraw_from_earn.assert_not_awaited()

    async def test_an_unseeded_pool_still_exits_on_an_unconfirmed_valuation(self, test_db):
        service, strategy, _ = self._service(
            external=95, idle=10, seeded=0, shares=10, on_chain_assets=110,
        )

        with patch("src.services.earn.vault_service.sign_transfer", return_value="0x" + "bb" * 65):
            result = await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "10", 0, USER_WITHDRAW_SIG)

        # Users must always be able to leave a pool with no senior claim on it.
        assert result["status"] == "completed"
        strategy.withdraw_from_earn.assert_not_awaited()


class TestDeployIdle:
    @staticmethod
    def _service(*, idle, minimum=0, healthy=True, name="midas-mtbill", buffer_min=0, buffer_bps=0):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = name
        strategy.idle_assets = AsyncMock(return_value=idle)
        strategy.stranded_assets = AsyncMock(return_value=0)
        strategy.in_flight_assets = AsyncMock(return_value=0)
        strategy.min_deploy_amount = AsyncMock(return_value=minimum)
        strategy.is_healthy = AsyncMock(return_value=healthy)
        strategy.total_assets = AsyncMock(return_value=1000)
        strategy.deposit_to_earn = AsyncMock()
        registry.register(POOL_ID_HEX, strategy)

        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1000, True,
        )
        contract.functions.getSeededAssets.return_value.call.return_value = 0
        service.settings = replace(
            service.settings, earn_buffer_min=buffer_min, earn_buffer_bps=buffer_bps,
        )
        return service, strategy

    async def test_routes_the_whole_idle_balance(self, test_db):
        service, strategy = self._service(idle=100_000)

        assert await service.deploy_reclaim(POOL_ID_HEX) == 100_000

        strategy.deposit_to_earn.assert_awaited_once_with(100_000)

    @pytest.mark.parametrize("status,moved", [
        ("executing", 99_900), ("pending", 0), ("failed", 100_000),
    ])
    async def test_waits_for_unresolved_rows_in_this_pool(self, test_db, status, moved):
        from src.core.db import db_write
        service, strategy = self._service(idle=100_000)
        db_write(
            test_db,
            """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, status, created_at, updated_at)
               VALUES ('w1', 'withdraw', ?, 'user', 'token', '100', 'signer', 0, '', ?, 1, 2)""",
            ("0x" + POOL_ID_HEX[2:].upper(), status),
        )
        # A receipt nobody has read may still move the balance; a withdrawal
        # still in the queue only keeps its own amount back.
        assert await service.deploy_reclaim(POOL_ID_HEX) == moved
        assert strategy.deposit_to_earn.await_count == (1 if moved else 0)

    async def test_a_reverted_withdraw_found_by_recovery_is_redeployed(self, test_db):
        """Timeout, then recovery reads the revert and fails the row; the
        reclaim it left idle goes back to the strategy on the next deploy,
        under the usual conditions, and the pool is resynced."""
        from src.core.db import db_write, get_db
        from src.services.earn.worker import EarnWorker
        service, strategy = self._service(idle=100_000)
        service.sync_total_assets = AsyncMock(return_value=1000)
        db_write(
            get_db(),
            """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, status, created_at, updated_at, tx_hash)
               VALUES ('w1', 'withdraw', ?, '0xuser', ?, '100000', ?, 1, '0xsig',
                       'pending', 0, 0, '0xw1')""",
            (POOL_ID_HEX, USDC_TOKEN_ID, POOL_ADDRESS),
        )
        service.sapphire.w3.eth.get_transaction_receipt = MagicMock(
            side_effect=[
                TimeoutError("timed out"),
                {"status": 0, "blockNumber": 7, "to": service.contract_address},
            ]
        )

        with patch("src.services.earn.worker.get_vault_service", return_value=service):
            await EarnWorker()._recover()
            assert self._status(test_db, "w1") == "pending"
            await EarnWorker()._recover()
        assert self._status(test_db, "w1") == "failed"
        strategy.deposit_to_earn.assert_not_awaited()

        assert await service.deploy_reclaim(POOL_ID_HEX) == 100_000
        strategy.deposit_to_earn.assert_awaited_once_with(100_000)
        service.sync_total_assets.assert_awaited()

    @staticmethod
    def _status(test_db, tx_id):
        return test_db.execute(
            "SELECT status FROM earn_transactions WHERE id = ?", (tx_id,)
        ).fetchone()["status"]

    async def test_completes_undeployed_deposits_once_their_funds_are_working(self, test_db):
        from src.core.db import db_write, get_db
        service, strategy = self._service(idle=100_000)
        strategy.idle_assets = AsyncMock(side_effect=[100_000] + [0] * 4)
        db_write(
            get_db(),
            """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, input_nonce, input_signature,
                status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("stuck", "deposit", POOL_ID_HEX.upper().replace("0X", "0x"), "0xuser", USDC_TOKEN_ID, "100000",
             "0xuser", 1, "0xsig", 1, "0xsig", "undeployed", 0, 0),
        )
        db_write(
            get_db(),
            """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, input_nonce, input_signature,
                status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("elsewhere", "deposit", "0xotherpool", "0xuser", USDC_TOKEN_ID, "100000",
             "0xuser", 2, "0xsig", 2, "0xsig", "undeployed", 0, 0),
        )

        await service.deploy_reclaim(POOL_ID_HEX)

        rows = get_db().execute(
            "SELECT id, status, error FROM earn_transactions ORDER BY id"
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            ("elsewhere", "undeployed", None),
            ("stuck", "completed", None),
        ]

    async def test_sweeps_funds_stranded_on_the_earn_account(self, test_db):
        service, strategy = self._service(idle=0)
        strategy.stranded_assets = AsyncMock(side_effect=[5_000_000, 0])

        assert await service.deploy_reclaim(POOL_ID_HEX) == 5_000_000
        strategy.deposit_to_earn.assert_awaited_once_with(5_000_000)

    async def test_deploys_idle_and_stranded_funds_together_against_the_minimum(self, test_db):
        service, strategy = self._service(idle=600_000, minimum=1_000_000)
        strategy.stranded_assets = AsyncMock(side_effect=[600_000, 0])

        assert await service.deploy_reclaim(POOL_ID_HEX) == 1_200_000
        strategy.deposit_to_earn.assert_awaited_once_with(1_200_000)

    async def test_leaves_undeployed_rows_open_while_a_bridge_is_in_flight(self, test_db):
        from src.core.db import db_write, get_db
        service, strategy = self._service(idle=0)
        strategy.in_flight_assets = AsyncMock(return_value=1_000_000)
        db_write(
            get_db(),
            """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, input_nonce, input_signature,
                status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("stuck", "deposit", POOL_ID_HEX, "0xuser", USDC_TOKEN_ID, "100000",
             "0xuser", 1, "0xsig", 1, "0xsig", "undeployed", 0, 0),
        )

        await service.deploy_reclaim(POOL_ID_HEX)

        row = get_db().execute("SELECT status FROM earn_transactions WHERE id = 'stuck'").fetchone()
        assert row[0] == "undeployed"

    async def test_stands_down_while_a_bridge_is_in_flight(self, test_db):
        service, strategy = self._service(idle=600_000)
        strategy.in_flight_assets = AsyncMock(return_value=400_000)

        assert await service.deploy_reclaim(POOL_ID_HEX) == 0
        strategy.deposit_to_earn.assert_not_awaited()

    async def test_reconciles_undeployed_rows_when_nothing_is_waiting(self, test_db):
        from src.core.db import db_write, get_db
        service, _ = self._service(idle=0)
        db_write(
            get_db(),
            """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, input_nonce, input_signature,
                status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("stuck", "deposit", POOL_ID_HEX, "0xuser", USDC_TOKEN_ID, "100000",
             "0xuser", 1, "0xsig", 1, "0xsig", "undeployed", 0, 0),
        )

        assert await service.deploy_reclaim(POOL_ID_HEX) == 0

        row = get_db().execute("SELECT status FROM earn_transactions WHERE id = 'stuck'").fetchone()
        assert row[0] == "completed"

    async def test_a_deploy_keeps_the_pool_open_with_its_amount_held(self, test_db):
        service, strategy = self._service(idle=100_000, buffer_min=20_000)
        during = []
        strategy.deposit_to_earn = AsyncMock(
            side_effect=lambda _a: during.append(
                (service._pool_lock(POOL_ID_HEX).locked(), dict(service._held))
            )
        )

        assert await service.deploy_reclaim(POOL_ID_HEX) == 80_000

        # Deposits and payouts carry on during the bridge, but cannot touch
        # the 80_000 it was promised.
        assert during == [(False, {POOL_ID_HEX: 80_000})]
        assert service._held == {}
        assert not service._pool_lock(POOL_ID_HEX).locked()

    async def test_a_failed_deploy_holds_its_amount_until_the_bridge_settles(self, test_db):
        service, strategy = self._service(idle=100_000)
        strategy.deposit_to_earn = AsyncMock(side_effect=RuntimeError("bridge timed out"))
        service.sync_total_assets = AsyncMock(return_value=1000)

        with pytest.raises(RuntimeError):
            await service.deploy_reclaim(POOL_ID_HEX)

        # Accounting may have debited a bridge that has not landed: a sync now
        # would write that dip into the share price.
        assert service._held == {POOL_ID_HEX: 100_000}
        assert service.sync_total_assets.await_count == 1

        strategy.in_flight_assets = AsyncMock(return_value=100_000)
        assert await service.deploy_reclaim(POOL_ID_HEX) == 0
        assert service._held == {POOL_ID_HEX: 100_000}

        strategy.in_flight_assets = AsyncMock(return_value=0)
        strategy.idle_assets = AsyncMock(return_value=0)
        await service.deploy_reclaim(POOL_ID_HEX)
        assert service._held == {}
        assert service.sync_total_assets.await_count == 2

    async def test_syncs_before_the_bridge_so_mid_bridge_deposits_price_right(self, test_db):
        service, strategy = self._service(idle=100_000)
        order = []
        service.sync_total_assets = AsyncMock(side_effect=lambda _p: order.append("sync") or 1000)
        strategy.deposit_to_earn = AsyncMock(side_effect=lambda _a: order.append("bridge"))

        await service.deploy_reclaim(POOL_ID_HEX)

        assert order == ["sync", "bridge", "sync"]

    async def test_an_unconfirmed_valuation_holds_the_deploy(self, test_db):
        service, strategy = self._service(idle=100_000)
        service.sync_total_assets = AsyncMock(return_value=None)

        assert await service.deploy_reclaim(POOL_ID_HEX) == 0
        strategy.deposit_to_earn.assert_not_awaited()
        assert service._held == {}

    async def test_skips_the_closing_sync_while_a_receipt_is_unread(self, test_db):
        from src.core.db import db_write, get_db
        service, strategy = self._service(idle=100_000)
        service.sync_total_assets = AsyncMock(return_value=1000)

        async def bridge(_amount):
            db_write(
                get_db(),
                """INSERT INTO earn_transactions
                   (id, operation, pool_id, user_address, token_id, amount,
                    signer_address, nonce, signature, status, created_at, updated_at)
                   VALUES ('d1', 'deposit', ?, '0xuser', ?, '5', '0xuser', 0, '0x', 'pending', 0, 0)""",
                (POOL_ID_HEX, USDC_TOKEN_ID),
            )

        strategy.deposit_to_earn = AsyncMock(side_effect=bridge)

        await service.deploy_reclaim(POOL_ID_HEX)

        # Syncing now would land after that deposit and erase it from totalAssets.
        assert service.sync_total_assets.await_count == 1

    async def test_keeps_the_buffer_floor_on_the_pool_account(self, test_db):
        service, strategy = self._service(idle=80_000_000, buffer_min=30_000_000)

        assert await service.deploy_reclaim(POOL_ID_HEX) == 50_000_000
        strategy.deposit_to_earn.assert_awaited_once_with(50_000_000)

    async def test_buffer_grows_with_the_pool(self, test_db):
        service, strategy = self._service(idle=100_000, buffer_min=1, buffer_bps=5_000)

        # Half of the 1000 deployed plus 100_000 idle stays behind.
        assert await service.deploy_reclaim(POOL_ID_HEX) == 49_500
        strategy.deposit_to_earn.assert_awaited_once_with(49_500)

    async def test_a_half_full_buffer_is_left_alone(self, test_db):
        from src.core.db import db_write, get_db
        service, strategy = self._service(idle=30_000_000, buffer_min=50_000_000)
        strategy.withdraw_from_earn = AsyncMock()
        db_write(
            get_db(),
            """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, input_nonce, input_signature,
                status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("stuck", "deposit", POOL_ID_HEX, "0xuser", USDC_TOKEN_ID, "100000",
             "0xuser", 1, "0xsig", 1, "0xsig", "undeployed", 0, 0),
        )

        assert await service.deploy_reclaim(POOL_ID_HEX) == 0
        strategy.deposit_to_earn.assert_not_awaited()
        strategy.withdraw_from_earn.assert_not_awaited()
        # Money held as the buffer is where it is meant to be.
        row = get_db().execute("SELECT status FROM earn_transactions WHERE id = 'stuck'").fetchone()
        assert row[0] == "completed"

    @staticmethod
    def _waiting(amount, tx_id, pool=POOL_ID_HEX):
        from src.core.db import db_write, get_db
        db_write(
            get_db(),
            """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, status, created_at, updated_at)
               VALUES (?, 'withdraw', ?, '0xuser', ?, ?, ?, 0, '0xsig', 'awaiting_liquidity', 0, 0)""",
            (tx_id, pool, USDC_TOKEN_ID, str(amount), POOL_ADDRESS),
        )

    async def test_one_reclaim_covers_every_waiting_withdrawal(self, test_db):
        service, strategy = self._service(idle=50, buffer_min=100)
        strategy.total_assets = AsyncMock(return_value=10_000)
        service.sync_total_assets = AsyncMock(return_value=10_050)
        self._waiting(300, "w1")
        self._waiting(200, "w2")
        self._waiting(999, "elsewhere", pool="0x" + "cd" * 32)
        during = []

        async def reclaim(amount):
            during.append((service._pool_lock(POOL_ID_HEX).locked(), service.reclaiming_pools()))

        strategy.withdraw_from_earn = AsyncMock(side_effect=reclaim)

        assert await service.deploy_reclaim(POOL_ID_HEX) == -450

        # Exactly what the two withdrawals are short of, nothing for the buffer.
        strategy.withdraw_from_earn.assert_awaited_once_with(450)
        strategy.withdraw_ready.assert_awaited_once_with(450)
        # The credit is spotted by the pool balance rising, so nothing else
        # may touch that balance until it lands.
        assert during == [(True, frozenset({POOL_ID_HEX}))]
        assert service.reclaiming_pools() == frozenset()
        service.sync_total_assets.assert_awaited_once()
        strategy.deposit_to_earn.assert_not_awaited()

    async def test_an_empty_buffer_is_not_refilled_from_the_strategy(self, test_db):
        # Mainnet, 30 Sep: a 50 USDC floor on a Midas pool holding ~71 USDC,
        # nothing waiting. The round redeemed 50 USDC to fill the buffer and
        # every holder paid Midas's 7 bps redeem fee through the next sync.
        service, strategy = self._service(idle=0, buffer_min=50_000_000)
        strategy.total_assets = AsyncMock(return_value=70_896_496)
        strategy.withdraw_from_earn = AsyncMock()

        assert await service.deploy_reclaim(POOL_ID_HEX) == 0
        strategy.withdraw_from_earn.assert_not_awaited()

    async def test_a_buffer_covering_the_waiting_withdrawals_needs_no_reclaim(self, test_db):
        service, strategy = self._service(idle=500, buffer_min=1_000)
        strategy.withdraw_from_earn = AsyncMock()
        self._waiting(400, "w1")

        assert await service.deploy_reclaim(POOL_ID_HEX) == 0
        strategy.withdraw_from_earn.assert_not_awaited()

    async def test_a_reclaim_never_asks_for_more_than_is_deployed(self, test_db):
        service, strategy = self._service(idle=0, buffer_min=100)
        strategy.total_assets = AsyncMock(return_value=250)
        strategy.withdraw_from_earn = AsyncMock()
        self._waiting(1_000, "w1")

        assert await service.deploy_reclaim(POOL_ID_HEX) == -250
        strategy.withdraw_from_earn.assert_awaited_once_with(250)

    async def test_waits_while_the_strategy_cannot_pay_out(self, test_db):
        service, strategy = self._service(idle=0)
        strategy.withdraw_ready = AsyncMock(return_value=False)
        strategy.withdraw_from_earn = AsyncMock()
        self._waiting(300, "w1")

        assert await service.deploy_reclaim(POOL_ID_HEX) == 0
        strategy.withdraw_from_earn.assert_not_awaited()

    @pytest.mark.parametrize("error", ["liquidity", "crash"])
    async def test_a_failed_reclaim_clears_the_pool_for_the_next_round(self, test_db, error):
        from src.services.earn.strategies.base import LiquidityUnavailable
        service, strategy = self._service(idle=0)
        strategy.withdraw_from_earn = AsyncMock(
            side_effect=LiquidityUnavailable("capacity") if error == "liquidity" else RuntimeError("rpc")
        )
        self._waiting(300, "w1")

        assert await service.deploy_reclaim(POOL_ID_HEX) == 0

        assert service.reclaiming_pools() == frozenset()
        assert not service._pool_lock(POOL_ID_HEX).locked()
        assert self._status(test_db, "w1") == "awaiting_liquidity"

    async def test_waiting_withdrawals_are_kept_back_from_a_deploy(self, test_db):
        service, strategy = self._service(idle=1_000, buffer_min=100)
        self._waiting(400, "w1")

        assert await service.deploy_reclaim(POOL_ID_HEX) == 500
        strategy.deposit_to_earn.assert_awaited_once_with(500)

    async def test_queued_withdrawals_are_kept_back_from_a_deploy_too(self, test_db):
        from src.core.db import db_write, get_db
        service, strategy = self._service(idle=1_000, buffer_min=100)
        self._waiting(100, "w1")
        for tx_id, status in (("w2", "scheduled"), ("w3", "executing"), ("done", "completed")):
            self._waiting(200, tx_id)
            db_write(get_db(), "UPDATE earn_transactions SET status = ? WHERE id = ?", (status, tx_id))

        # A withdrawal released back to the queue still needs its money.
        assert await service.deploy_reclaim(POOL_ID_HEX) == 400
        strategy.deposit_to_earn.assert_awaited_once_with(400)

    async def test_a_paused_pool_still_reclaims_for_its_exits(self, test_db):
        service, strategy = self._service(idle=0)
        strategy.withdraw_from_earn = AsyncMock()
        self._waiting(300, "w1")

        assert await service.deploy_reclaim(POOL_ID_HEX, allow_deploy=False) == -300

    async def test_a_paused_pool_takes_nothing_new(self, test_db):
        service, strategy = self._service(idle=100_000)

        assert await service.deploy_reclaim(POOL_ID_HEX, allow_deploy=False) == 0
        strategy.deposit_to_earn.assert_not_awaited()

    async def test_a_deposit_landing_mid_bridge_stays_undeployed(self, test_db):
        from src.core.db import db_write, get_db
        service, strategy = self._service(idle=100_000)
        insert = """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, input_nonce, input_signature,
                status, created_at, updated_at)
               VALUES (?, 'deposit', ?, '0xuser', ?, '1', '0xuser', 1, '0xsig', 1, '0xsig',
                       'undeployed', 0, 0)"""
        db_write(get_db(), insert, ("before", POOL_ID_HEX, USDC_TOKEN_ID))

        async def bridge(_amount):
            db_write(get_db(), insert, ("during", POOL_ID_HEX, USDC_TOKEN_ID))

        strategy.deposit_to_earn = AsyncMock(side_effect=bridge)

        await service.deploy_reclaim(POOL_ID_HEX)

        assert self._status(test_db, "before") == "completed"
        assert self._status(test_db, "during") == "undeployed"

    async def test_does_nothing_when_there_is_nothing_idle(self, test_db):
        service, strategy = self._service(idle=0)

        assert await service.deploy_reclaim(POOL_ID_HEX) == 0

        strategy.deposit_to_earn.assert_not_awaited()

    async def test_leaves_amounts_below_the_protocol_minimum(self, test_db):
        service, strategy = self._service(idle=500_000, minimum=1_000_000)

        assert await service.deploy_reclaim(POOL_ID_HEX) == 0

        strategy.deposit_to_earn.assert_not_awaited()

    async def test_leaves_the_funds_when_the_strategy_is_unhealthy(self, test_db):
        service, strategy = self._service(idle=100_000, healthy=False)

        assert await service.deploy_reclaim(POOL_ID_HEX) == 0

        strategy.deposit_to_earn.assert_not_awaited()

    async def test_manual_pools_have_nothing_to_deploy_into(self, test_db):
        service, strategy = self._service(idle=100_000, name="manual")

        assert await service.deploy_reclaim(POOL_ID_HEX) == 0

        strategy.deposit_to_earn.assert_not_awaited()


class TestSeededAssetsRead:
    def test_an_upgraded_contract_returns_the_figure(self):
        service, contract, _, _ = _make_service()
        contract.functions.getSeededAssets.return_value.call.return_value = 100_000

        assert service.get_seeded_assets(b"\x11" * 32) == 100_000

    def test_a_contract_without_the_function_reads_as_unseeded(self):
        from web3.exceptions import ContractLogicError

        service, contract, _, _ = _make_service()
        contract.functions.getSeededAssets.return_value.call.side_effect = ContractLogicError(
            "execution reverted"
        )

        # The proxy predates the upgrade, so there is no seed to net out and
        # every quote would otherwise fail until it lands.
        assert service.get_seeded_assets(b"\x11" * 32) == 0

    def test_a_network_failure_is_not_swallowed(self):
        service, contract, _, _ = _make_service()
        contract.functions.getSeededAssets.return_value.call.side_effect = ConnectionError(
            "rpc down"
        )

        with pytest.raises(ConnectionError):
            service.get_seeded_assets(b"\x11" * 32)


class TestStaleLeashRetry:
    @staticmethod
    def _rpc_error():
        from web3.exceptions import Web3RPCError

        return Web3RPCError(
            "{'code': -32000, 'message': 'invalid signed simulate call query: "
            "base block not found'}"
        )

    def test_a_stale_leash_is_retried(self, test_db):
        service, contract, _, _ = _make_service()
        contract.functions.getSeededAssets.return_value.call.side_effect = [
            self._rpc_error(), 100_000,
        ]

        with patch("src.services.earn.vault_service.time.sleep"):
            assert service.get_seeded_assets(b"\x11" * 32) == 100_000

    def test_a_stale_query_nonce_is_retried(self, test_db):
        from web3.exceptions import Web3RPCError

        service, contract, _, _ = _make_service()
        contract.functions.getUserShares.return_value.call.side_effect = [
            Web3RPCError(
                "{'code': -32000, 'message': 'invalid signed simulate call query: stale nonce'}"
            ),
            7,
        ]

        with patch("src.services.earn.vault_service.time.sleep"):
            assert service.get_user_shares_via_token(b"\x11" * 32, "0xab") == 7

    def test_it_gives_up_rather_than_retrying_forever(self, test_db):
        from web3.exceptions import Web3RPCError

        from src.services.earn.vault_service import READ_RETRY_ATTEMPTS

        service, contract, _, _ = _make_service()
        contract.functions.getSeededAssets.return_value.call.side_effect = [
            self._rpc_error() for _ in range(READ_RETRY_ATTEMPTS)
        ]

        with patch("src.services.earn.vault_service.time.sleep"):
            with pytest.raises(Web3RPCError):
                service.get_seeded_assets(b"\x11" * 32)

    def test_the_pool_listing_reads_are_retried(self, test_db):
        service, contract, _, _ = _make_service()
        contract.functions.getPoolCount.return_value.call.side_effect = [
            self._rpc_error(), 0,
        ]

        with patch("src.services.earn.vault_service.time.sleep"):
            assert service.list_pools() == []

    def test_the_user_share_read_is_retried(self, test_db):
        service, contract, _, _ = _make_service()
        contract.functions.getUserShares.return_value.call.side_effect = [
            self._rpc_error(), 7,
        ]

        with patch("src.services.earn.vault_service.time.sleep"):
            assert service.get_user_shares_via_token(b"\x11" * 32, "0xab") == 7

    def test_the_share_conversion_reads_are_retried(self, test_db):
        service, contract, _, _ = _make_service()
        contract.functions.convertToAssets.return_value.call.side_effect = [
            self._rpc_error(), 42,
        ]

        with patch("src.services.earn.vault_service.time.sleep"):
            assert service.convert_to_assets(b"\x11" * 32, 1) == 42

    def test_other_rpc_errors_are_not_retried(self, test_db):
        from web3.exceptions import Web3RPCError

        service, contract, _, _ = _make_service()
        call = contract.functions.getSeededAssets.return_value.call
        call.side_effect = Web3RPCError("{'code': -32000, 'message': 'out of gas'}")

        with pytest.raises(Web3RPCError):
            service.get_seeded_assets(b"\x11" * 32)
        assert call.call_count == 1


class TestSeedCache:
    def test_repeated_reads_hit_the_contract_once(self, test_db):
        service, contract, _, _ = _make_service()
        call = contract.functions.getSeededAssets.return_value.call
        call.return_value = 100_000

        assert service.get_seeded_assets(b"\x11" * 32) == 100_000
        assert service.get_seeded_assets(b"\x11" * 32) == 100_000

        # Every extra signed query is another chance at a stale leash.
        assert call.call_count == 1

    def test_each_pool_is_cached_separately(self, test_db):
        service, contract, _, _ = _make_service()
        call = contract.functions.getSeededAssets.return_value.call
        call.side_effect = [100_000, 250_000]

        assert service.get_seeded_assets(b"\x11" * 32) == 100_000
        assert service.get_seeded_assets(b"\x22" * 32) == 250_000

    def test_the_money_paths_read_through(self, test_db):
        service, contract, _, _ = _make_service()
        call = contract.functions.getSeededAssets.return_value.call
        call.side_effect = [100_000, 0]

        service.get_seeded_assets(b"\x11" * 32)
        # An operator can drop the seed at any moment, so anything that moves
        # funds asks the chain rather than trusting a cached figure.
        assert service.get_seeded_assets(b"\x11" * 32, fresh=True) == 0


class TestPoolCustody:
    @staticmethod
    def _service_with_foreign_pool():
        service, contract, sapphire, _ = _make_service()
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            "0x" + "99" * 20,   # created against some other account
            1000, 1050, True,
        )
        return service, sapphire

    async def test_deposit_refuses_a_pool_this_service_cannot_pay_out(self, test_db):
        service, sapphire = self._service_with_foreign_pool()

        with pytest.raises(ValueError, match="not served by this deployment"):
            await service.deposit(POOL_ID_HEX, USER_ADDRESS, "1000", 0, "0x" + "cc" * 65)

        # The whole point is that no shares exist against funds the service
        # could never redeem.
        sapphire.execute_contract_call.assert_not_called()

    async def test_withdraw_refuses_before_reclaiming_anything(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.withdraw_ready = AsyncMock(return_value=True)
        strategy.name = "aave-v3"
        strategy.withdraw_from_earn = AsyncMock()
        registry.register(POOL_ID_HEX, strategy)
        service, contract, sapphire, _ = _make_service(registry=registry)
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]), "0x" + "99" * 20, 1000, 1050, True,
        )

        with pytest.raises(ValueError, match="not served by this deployment"):
            await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "500", 0, USER_WITHDRAW_SIG)

        # Failing after the reclaim would leave funds mid-flight for a payout
        # that was always going to revert.
        strategy.withdraw_from_earn.assert_not_awaited()
        sapphire.execute_contract_call.assert_not_called()

    async def test_a_matching_pool_is_allowed_through(self, test_db):
        service, _, _, _ = _make_service()
        service._assert_pool_custody({"pool_address": POOL_ADDRESS})

    async def test_an_unset_earn_account_is_refused(self, test_db):
        from dataclasses import replace

        service, _, _, _ = _make_service()
        service.settings = replace(service.settings, earn_pool_address="")

        with pytest.raises(ValueError, match="not served by this deployment"):
            service._assert_pool_custody({"pool_address": POOL_ADDRESS})

def _transfer_sig(key, amount, nonce):
    from src.core.eip712 import sign_transfer

    return sign_transfer(
        private_key=key, chain_id=23295,
        verifying_contract="0xad3C76e4E621C0cfF7540479Ee9B0A945723A642",
        to_address=POOL_ADDRESS, token_id=USDC_TOKEN_ID, amount=amount, nonce=nonce,
    )


def _schedulable(contract):
    contract.functions.pools.return_value.call.return_value = (
        bytes.fromhex(USDC_TOKEN_ID[2:]), POOL_ADDRESS, 1000, 1050, True,
    )


class TestScheduling:
    """The request path records and returns. Everything that needs a chain
    read happens in the worker, because a deposit that waits for those inline
    is a deposit the gateway hangs up on."""

    def test_a_scheduled_deposit_is_queued_not_executed(self, test_db):
        service, contract, _, _ = _make_service()
        _schedulable(contract)

        from eth_account import Account

        key = "0x" + "33" * 32
        user = Account.from_key(key).address
        result = service.schedule_deposit(
            pool_id_hex=POOL_ID_HEX, user_address=user,
            amount="1000", nonce=3, signature=_transfer_sig(key, 1000, 3),
        )

        assert result["status"] == "scheduled"
        row = test_db.execute(
            "SELECT * FROM earn_transactions WHERE id = ?", (result["id"],)
        ).fetchone()
        assert row["operation"] == "deposit"
        assert row["status"] == "scheduled"
        assert row["amount"] == "1000"
        assert row["nonce"] == 3
        # The pool is read once, to bind the signature and to answer the checks
        # that settle a request outright. What is deferred is the strategy leg.
        assert contract.functions.pools.call_count == 1

    @staticmethod
    def _consent(key: str, amount: int, nonce: int) -> str:
        from src.core.eip712 import sign_withdraw_consent

        return sign_withdraw_consent(
            private_key=key, chain_id=23295,
            earn_manager_address="0x1111111111111111111111111111111111111111",
            pool_id=POOL_ID_HEX, amount=amount, nonce=nonce,
        )

    def test_a_scheduled_withdraw_is_recorded_as_a_withdraw(self, test_db):
        from eth_account import Account

        service, contract, _, _ = _make_service()
        _schedulable(contract)
        key = "0x" + "11" * 32
        user = Account.from_key(key).address

        result = service.schedule_withdraw(
            pool_id_hex=POOL_ID_HEX, user_address=user,
            amount="500", nonce=1, signature=self._consent(key, 500, 1),
        )

        row = test_db.execute(
            "SELECT operation, status FROM earn_transactions WHERE id = ?", (result["id"],)
        ).fetchone()
        assert row["operation"] == "withdraw"
        assert row["status"] == "scheduled"

    def test_a_withdraw_signed_by_someone_else_is_refused_before_it_queues(self, test_db):
        """A withdraw the pool account cannot cover makes the idle deployer
        reclaim for it before the contract checks consent, so an unverified one
        is a way to make the pool redeem on demand. The consent recovers
        without a chain read, so it is bound here rather than at execution."""
        from eth_account import Account

        service, contract, _, _ = _make_service()
        _schedulable(contract)
        attacker_key = "0x" + "22" * 32
        victim = Account.from_key("0x" + "11" * 32).address

        with pytest.raises(ValueError, match="not signed by user_address"):
            service.schedule_withdraw(
                pool_id_hex=POOL_ID_HEX, user_address=victim,
                amount="500", nonce=1, signature=self._consent(attacker_key, 500, 1),
            )

        assert test_db.execute("SELECT COUNT(*) c FROM earn_transactions").fetchone()["c"] == 0

    def test_a_scheduled_row_keeps_the_callers_own_nonce_and_signature(self, test_db):
        """Execution overwrites nonce/signature with what it settled on — a
        withdraw signs the payout with the pool's key — so the caller's own
        consent is kept in its own columns for reconstruction after a crash."""
        from eth_account import Account

        service, contract, _, _ = _make_service()
        _schedulable(contract)
        key = "0x" + "44" * 32
        user = Account.from_key(key).address
        sig = _transfer_sig(key, 1000, 7)

        result = service.schedule_deposit(
            pool_id_hex=POOL_ID_HEX, user_address=user,
            amount="1000", nonce=7, signature=sig,
        )

        row = test_db.execute(
            "SELECT input_nonce, input_signature FROM earn_transactions WHERE id = ?",
            (result["id"],),
        ).fetchone()
        assert row["input_nonce"] == 7
        assert row["input_signature"] == sig

    def test_a_pool_id_without_its_prefix_is_stored_in_one_spelling(self, test_db):
        from eth_account import Account

        service, contract, _, _ = _make_service()
        _schedulable(contract)
        key = "0x" + "55" * 32
        user = Account.from_key(key).address

        result = service.schedule_deposit(
            pool_id_hex=POOL_ID_HEX.removeprefix("0x").upper(), user_address=user,
            amount="1000", nonce=3, signature=_transfer_sig(key, 1000, 3),
        )

        row = test_db.execute(
            "SELECT pool_id FROM earn_transactions WHERE id = ?", (result["id"],)
        ).fetchone()
        assert row["pool_id"] == POOL_ID_HEX
        assert service._pool_lock(POOL_ID_HEX.removeprefix("0x")) is service._pool_lock(POOL_ID_HEX)

    @pytest.mark.parametrize(
        "field,value",
        [("user_address", "nope"), ("amount", "-1"), ("signature", "0xzz")],
    )
    def test_a_malformed_request_is_refused_without_queueing(self, test_db, field, value):
        service, contract, _, _ = _make_service()
        _schedulable(contract)
        kwargs = dict(
            pool_id_hex=POOL_ID_HEX, user_address=USER_ADDRESS,
            amount="1000", nonce=0, signature="0x" + "aa" * 65,
        )
        kwargs[field] = value

        with pytest.raises(ValueError):
            service.schedule_deposit(**kwargs)

        assert test_db.execute("SELECT COUNT(*) c FROM earn_transactions").fetchone()["c"] == 0

    def test_a_second_deposit_on_a_held_nonce_is_refused_before_it_queues(self, test_db):
        from eth_account import Account

        from src.services.user_queue import OperationPendingError

        service, contract, _, _ = _make_service()
        _schedulable(contract)
        key = "0x" + "66" * 32
        user = Account.from_key(key).address
        first = service.schedule_deposit(
            pool_id_hex=POOL_ID_HEX, user_address=user,
            amount="1000", nonce=3, signature=_transfer_sig(key, 1000, 3),
        )

        with pytest.raises(OperationPendingError) as exc:
            service.schedule_deposit(
                pool_id_hex=POOL_ID_HEX, user_address=user,
                amount="2000", nonce=3, signature=_transfer_sig(key, 2000, 3),
            )

        assert exc.value.operation_type == "earn_deposit"
        assert exc.value.operation_id == first["id"]
        assert test_db.execute("SELECT COUNT(*) c FROM earn_transactions").fetchone()["c"] == 1

    def test_a_user_at_the_pending_limit_is_refused_before_it_queues(self, test_db):
        from eth_account import Account

        from src.core.db import db_write
        from src.services.earn.vault_service import PendingLimitReached

        service, contract, _, _ = _make_service()
        _schedulable(contract)
        service.settings = replace(service.settings, earn_max_pending_per_user=2)
        key = "0x" + "88" * 32
        user = Account.from_key(key).address
        for i, status in enumerate(("awaiting_liquidity", "completed")):
            db_write(
                test_db,
                """INSERT INTO earn_transactions
                   (id, operation, pool_id, user_address, token_id, amount,
                    signer_address, nonce, signature, status, created_at, updated_at)
                   VALUES (?, 'withdraw', ?, ?, '', '1', ?, 0, '0x', ?, 0, 0)""",
                (f"old{i}", POOL_ID_HEX, user.lower(), user.lower(), status),
            )
        service.schedule_deposit(
            pool_id_hex=POOL_ID_HEX, user_address=user,
            amount="1000", nonce=1, signature=_transfer_sig(key, 1000, 1),
        )

        with pytest.raises(PendingLimitReached):
            service.schedule_deposit(
                pool_id_hex=POOL_ID_HEX, user_address=user,
                amount="1000", nonce=2, signature=_transfer_sig(key, 1000, 2),
            )

        assert test_db.execute("SELECT COUNT(*) c FROM earn_transactions").fetchone()["c"] == 3

    def test_a_retry_at_the_limit_still_returns_its_operation(self, test_db):
        from eth_account import Account

        service, contract, _, _ = _make_service()
        _schedulable(contract)
        service.settings = replace(service.settings, earn_max_pending_per_user=1)
        key = "0x" + "99" * 32
        user = Account.from_key(key).address
        kwargs = dict(
            pool_id_hex=POOL_ID_HEX, user_address=user,
            amount="1000", nonce=1, signature=_transfer_sig(key, 1000, 1),
        )
        first = service.schedule_deposit(**kwargs)

        # A lost response retried is the same request, not a new one.
        assert service.schedule_deposit(**kwargs)["id"] == first["id"]

    def test_other_users_are_not_held_to_someone_elses_limit(self, test_db):
        from eth_account import Account

        service, contract, _, _ = _make_service()
        _schedulable(contract)
        service.settings = replace(service.settings, earn_max_pending_per_user=1)
        busy, free = "0x" + "aa" * 32, "0x" + "bb" * 32
        service.schedule_deposit(
            pool_id_hex=POOL_ID_HEX, user_address=Account.from_key(busy).address,
            amount="1000", nonce=1, signature=_transfer_sig(busy, 1000, 1),
        )

        result = service.schedule_deposit(
            pool_id_hex=POOL_ID_HEX, user_address=Account.from_key(free).address,
            amount="1000", nonce=1, signature=_transfer_sig(free, 1000, 1),
        )

        assert result["status"] == "scheduled"

    def test_a_deposit_on_a_spent_nonce_is_refused_before_it_queues(self, test_db):
        from eth_account import Account

        from src.services.user_queue import StaleNonceError

        service, contract, _, _ = _make_service()
        _schedulable(contract)
        service.accounting.transfer_nonce = MagicMock(return_value=4)
        key = "0x" + "12" * 32
        user = Account.from_key(key).address

        with pytest.raises(StaleNonceError) as exc:
            service.schedule_deposit(
                pool_id_hex=POOL_ID_HEX, user_address=user,
                amount="1000", nonce=3, signature=_transfer_sig(key, 1000, 3),
            )

        # It would only revert with InvalidNonce after showing up as pending.
        assert exc.value.payload()["code"] == "stale_nonce"
        assert exc.value.payload()["current_nonce"] == 4
        assert test_db.execute("SELECT COUNT(*) c FROM earn_transactions").fetchone()["c"] == 0

    def test_the_current_or_a_later_nonce_still_queues(self, test_db):
        from eth_account import Account

        service, contract, _, _ = _make_service()
        _schedulable(contract)
        service.accounting.transfer_nonce = MagicMock(return_value=4)
        key = "0x" + "13" * 32
        user = Account.from_key(key).address

        # The next one may be signed while the current one is still queued.
        for nonce in (4, 5):
            result = service.schedule_deposit(
                pool_id_hex=POOL_ID_HEX, user_address=user,
                amount="1000", nonce=nonce, signature=_transfer_sig(key, 1000, nonce),
            )
            assert result["status"] == "scheduled"

    def test_a_retry_of_a_queued_deposit_returns_it_once_its_nonce_is_spent(self, test_db):
        from eth_account import Account

        service, contract, _, _ = _make_service()
        _schedulable(contract)
        key = "0x" + "14" * 32
        kwargs = dict(
            pool_id_hex=POOL_ID_HEX, user_address=Account.from_key(key).address,
            amount="1000", nonce=0, signature=_transfer_sig(key, 1000, 0),
        )
        first = service.schedule_deposit(**kwargs)
        service.accounting.transfer_nonce = MagicMock(return_value=1)

        assert service.schedule_deposit(**kwargs)["id"] == first["id"]

    def test_a_withdraw_does_not_read_the_transfer_nonce(self, test_db):
        from eth_account import Account

        service, contract, _, _ = _make_service()
        _schedulable(contract)
        service.accounting.transfer_nonce = MagicMock(return_value=99)
        key = "0x" + "15" * 32

        result = service.schedule_withdraw(
            pool_id_hex=POOL_ID_HEX, user_address=Account.from_key(key).address,
            amount="500", nonce=1, signature=self._consent(key, 500, 1),
        )

        # A withdraw is signed against EarnManager's own nonce, not this one.
        assert result["status"] == "scheduled"
        service.accounting.transfer_nonce.assert_not_called()

    def test_a_withdraw_consent_nonce_never_blocks_a_deposit(self, test_db):
        from eth_account import Account

        service, contract, _, _ = _make_service()
        _schedulable(contract)
        key = "0x" + "77" * 32
        user = Account.from_key(key).address
        service.schedule_withdraw(
            pool_id_hex=POOL_ID_HEX, user_address=user,
            amount="500", nonce=3, signature=self._consent(key, 500, 3),
        )

        result = service.schedule_deposit(
            pool_id_hex=POOL_ID_HEX, user_address=user,
            amount="1000", nonce=3, signature=_transfer_sig(key, 1000, 3),
        )

        assert result["status"] == "scheduled"
        assert test_db.execute("SELECT COUNT(*) c FROM earn_transactions").fetchone()["c"] == 2

    def test_a_queued_swap_blocks_a_deposit_on_the_same_nonce(self, test_db):
        import time

        from eth_account import Account

        from src.services.user_queue import OperationPendingError

        service, contract, _, _ = _make_service()
        _schedulable(contract)
        key = "0x" + "88" * 32
        user = Account.from_key(key).address
        now = int(time.time())
        test_db.execute(
            """INSERT INTO swaps
               (id, quote_id, user_address, from_token_id, to_token_id, from_amount,
                to_amount_estimate, status, venue, created_at, updated_at,
                input_nonce, input_signature)
               VALUES ('swap-1', 'q', ?, ?, ?, '1', '1', 'scheduled', 'internal', ?, ?, '3', '0xsig')""",
            (user.lower(), USDC_TOKEN_ID, USDC_TOKEN_ID, now, now),
        )
        test_db.commit()

        with pytest.raises(OperationPendingError) as exc:
            service.schedule_deposit(
                pool_id_hex=POOL_ID_HEX, user_address=user,
                amount="1000", nonce=3, signature=_transfer_sig(key, 1000, 3),
            )

        assert (exc.value.operation_type, exc.value.operation_id) == ("swap", "swap-1")

    def test_executing_a_scheduled_row_updates_it_rather_than_adding_another(self, test_db):
        from eth_account import Account

        service, contract, _, _ = _make_service()
        _schedulable(contract)
        key = "0x" + "55" * 32
        user = Account.from_key(key).address
        scheduled = service.schedule_deposit(
            pool_id_hex=POOL_ID_HEX, user_address=user,
            amount="1000", nonce=0, signature=_transfer_sig(key, 1000, 0),
        )

        adopted = service._record_transaction(
            existing_id=scheduled["id"], operation="deposit", pool_id_hex=POOL_ID_HEX,
            user_address=user, token_id=USDC_TOKEN_ID, amount="1000",
            signer_address=user, nonce=0, signature=_transfer_sig(key, 1000, 0),
        )

        assert adopted == scheduled["id"]
        assert test_db.execute("SELECT COUNT(*) c FROM earn_transactions").fetchone()["c"] == 1
        row = test_db.execute(
            "SELECT status, token_id FROM earn_transactions WHERE id = ?", (adopted,)
        ).fetchone()
        assert row["status"] == "pending"
        # Scheduling cannot know the token without reading the pool, so
        # execution has to fill it or every consumer keyed on it sees a blank.
        assert row["token_id"] == USDC_TOKEN_ID
