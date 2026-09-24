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


def _pool_shares(contract, by_block, latest=None):
    """pools() returns ``by_block[n]`` when pinned to block n, else ``latest``."""
    def pools(block_identifier=None):
        shares = latest if block_identifier is None else by_block[block_identifier]
        return (bytes.fromhex(USDC_TOKEN_ID[2:]), POOL_ADDRESS, shares, shares, True)
    contract.functions.pools.return_value.call.side_effect = pools


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
        saph.reader = w3
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
    async def test_deposit_routes_to_strategy(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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
        strategy.is_healthy = AsyncMock(return_value=True)
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

    async def test_undeployed_deposit_records_why_the_bridge_refused(self, test_db):
        """Regression for the testnet outage: the accounting SDK folds its
        .detail into str() (privana-sdk fix), and the row must keep that
        explanation rather than truncating it back to "400 Bad Request"."""
        from src.services.earn.registry import StrategyRegistry

        class ApiError(Exception):
            def __init__(self, message, status_code, detail=None):
                super().__init__(message)
                self.status_code = status_code
                self.detail = detail

            def __str__(self):
                base = super().__str__()
                if self.detail and self.detail not in base:
                    return f"{base}: {self.detail}"
                return base

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.deposit_to_earn = AsyncMock(
            side_effect=ApiError(
                "API request failed: 400 Bad Request",
                400,
                "Insufficient native balance on Base Sepolia. EVM address "
                "0xE5A94d196DE8EeC7ABEc59aca32C322F3Dccc74A has 0 wei, "
                "needs at least 10000000000000 wei.",
            )
        )
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

        assert result["status"] == "undeployed"
        row = test_db.execute(
            "SELECT error FROM earn_transactions WHERE id = ?",
            (result["deposit_id"],),
        ).fetchone()
        assert "Insufficient native balance on Base Sepolia" in row["error"]
        assert "0xE5A94d196DE8EeC7ABEc59aca32C322F3Dccc74A" in row["error"]

    async def test_deposit_is_undeployed_until_strategy_routing_succeeds(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.total_assets = AsyncMock(return_value=1050)
        strategy.idle_assets = AsyncMock(return_value=0)

        status_during_routing = {}

        async def capture_status(amount):
            row = test_db.execute(
                "SELECT status FROM earn_transactions WHERE operation = 'deposit'"
            ).fetchone()
            status_during_routing["value"] = row["status"]

        strategy.deposit_to_earn = AsyncMock(side_effect=capture_status)
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

        # A crash between the mint and the routing must leave the row visibly
        # undeployed, never "completed" with idle funds behind it.
        assert status_during_routing["value"] == "undeployed"
        assert result["status"] == "completed"
        row = test_db.execute(
            "SELECT status FROM earn_transactions WHERE id = ?",
            (result["deposit_id"],),
        ).fetchone()
        assert row["status"] == "completed"

    async def test_rate_snapshot_pairs_assets_with_the_shares_of_one_instant(self):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
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
            held.append(service._pools_tx_lock.locked())
            return 1050

        service.sync_total_assets = spy
        return held

    async def test_deposit_syncs_under_the_lock(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
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

    async def test_failed_withdraw_resyncs_under_the_lock(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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

    async def test_deposit_refuses_when_strategy_unhealthy(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
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

    async def test_withdraw_reclaims_from_strategy(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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
        strategy.is_healthy = AsyncMock(return_value=True)
        strategy.withdraw_from_earn = AsyncMock(side_effect=RuntimeError("aave rpc down"))
        strategy.deposit_to_earn = AsyncMock()
        strategy.total_assets = AsyncMock(return_value=1050)
        strategy.idle_assets = AsyncMock(return_value=0)
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
            with pytest.raises(ValueError, match="Withdraw failed"):
                await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "500", 0, USER_WITHDRAW_SIG)

        strategy.withdraw_from_earn.assert_awaited_once()
        strategy.deposit_to_earn.assert_awaited_once_with(500)
        sapphire.execute_contract_call.assert_not_called()

    async def test_withdraw_onchain_revert_resupplies_reclaimed_funds(self, test_db):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
        strategy.name = "aave-v3"
        strategy.is_healthy = AsyncMock(return_value=True)
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


class TestShareDeltaCapture:
    """A cashflow's share movement is totalShares across its block."""

    async def test_deposit_records_positive_delta_and_rate(self, test_db):
        service, contract, _, _ = _make_service()
        _pool_shares(contract, {BLOCK - 1: 1000, BLOCK: 1100}, latest=1000)

        await service.deposit(POOL_ID_HEX, USER_ADDRESS, "105", 5, "0x" + "aa" * 65)

        row = test_db.execute("SELECT * FROM earn_transactions").fetchone()
        assert row["shares_delta"] == "100"
        assert row["exchange_rate"] == "1.05"
        assert row["settled_at"] == 1_000_000 + BLOCK

    async def test_withdraw_records_negative_delta(self, test_db):
        service, contract, _, _ = _make_service()
        _pool_shares(contract, {BLOCK - 1: 1000, BLOCK: 600}, latest=1000)

        with patch(
            "src.services.earn.vault_service.sign_transfer",
            return_value="0x" + "bb" * 65,
        ):
            await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "420", 0, USER_WITHDRAW_SIG)

        row = test_db.execute("SELECT * FROM earn_transactions").fetchone()
        assert row["shares_delta"] == "-400"
        assert row["exchange_rate"] == "1.05"

    async def test_a_later_mint_is_not_folded_in(self, test_db):
        # A later read no longer folds the next cashflow in.
        service, contract, _, _ = _make_service()
        _pool_shares(contract, {BLOCK - 1: 1000, BLOCK: 1100}, latest=1300)

        await service.deposit(POOL_ID_HEX, USER_ADDRESS, "105", 5, "0x" + "aa" * 65)

        row = test_db.execute("SELECT * FROM earn_transactions").fetchone()
        assert row["shares_delta"] == "100"

    async def test_failed_deposit_records_no_delta(self, test_db):
        service, contract, saph, _ = _make_service()
        contract.functions.pools.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]), POOL_ADDRESS, 1000, 1050, True,
        )
        saph.execute_contract_call.side_effect = RuntimeError("reverted")

        await service.deposit(POOL_ID_HEX, USER_ADDRESS, "105", 5, "0x" + "aa" * 65)

        row = test_db.execute("SELECT * FROM earn_transactions").fetchone()
        assert row["status"] == "failed"
        assert row["shares_delta"] is None

    async def test_delta_is_null_when_the_block_read_fails(self, test_db):
        service, contract, _, _ = _make_service()
        _pool_shares(contract, {BLOCK: 1100}, latest=1000)  # BLOCK - 1 raises

        result = await service.deposit(
            POOL_ID_HEX, USER_ADDRESS, "105", 5, "0x" + "aa" * 65
        )

        # The deposit itself still settles; only the earned figure is lost.
        assert result["status"] == "completed"
        row = test_db.execute("SELECT * FROM earn_transactions").fetchone()
        assert row["shares_delta"] is None


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


class TestRepairShareLedger:
    """Rows in a mismatched pool are re-derived from their block."""

    # totalShares: +100 at block 10, +50 at 20, -30 at 30, +50 at 40.
    HISTORY = {9: 0, 10: 100, 19: 100, 20: 150, 29: 150, 30: 120, 39: 120, 40: 170}

    def _row(self, tx_id, *, block, operation="deposit", status="completed",
             shares_delta=None, updated_at=111, amount="100"):
        from src.core.db import db_write, get_db
        db_write(
            get_db(),
            """INSERT INTO earn_transactions
               (id, operation, pool_id, user_address, token_id, amount,
                signer_address, nonce, signature, status, created_at, updated_at,
                tx_hash, shares_delta, error)
               VALUES (?, ?, ?, ?, '0xtok', ?, '0xsig', 0, '0xsig', ?, 100, ?, ?, ?, ?)""",
            (tx_id, operation, POOL_ID_HEX, USER_ADDRESS.lower(), amount, status,
             updated_at, f"0x{tx_id}", shares_delta,
             "timed out" if status == "failed" else None),
        )
        return f"0x{tx_id}", block

    def _service(self, rows, *, chain_total=170, history=None):
        from web3.exceptions import TransactionNotFound
        service, contract, saph, _ = _make_service()
        _pool_shares(contract, history or self.HISTORY)
        receipts = {tx_hash: block for tx_hash, block in rows}

        def receipt(tx_hash):
            block = receipts[tx_hash]
            if block is None:
                raise TransactionNotFound(tx_hash)
            if block < 0:
                return {"status": 0, "blockNumber": -block}
            return {"status": 1, "blockNumber": block}

        saph.w3.eth.get_transaction_receipt.side_effect = receipt
        service.list_pools = MagicMock(
            return_value=[{"pool_id": POOL_ID_HEX, "total_shares": chain_total}]
        )
        return service, saph

    def _get(self, test_db, tx_id):
        return test_db.execute(
            "SELECT * FROM earn_transactions WHERE id = ?", (tx_id,)
        ).fetchone()

    async def test_wrong_missing_and_failed_but_landed_rows_are_rederived(self, test_db):
        from src.services.earn.vault_service import _settled_shares
        rows = [
            self._row("d1", block=10, shares_delta="300"),
            self._row("d2", block=20, shares_delta=None),
            self._row("w1", block=30, operation="withdraw", status="failed", amount="30"),
            self._row("d3", block=40, status="failed", amount="50"),
            self._row("reverted", block=-50, status="failed"),
            self._row("dropped", block=None, status="failed"),
        ]
        service, _ = self._service(rows)

        await service.repair_share_ledger()

        d1 = self._get(test_db, "d1")
        assert (d1["shares_delta"], d1["updated_at"]) == ("100", 111)
        assert self._get(test_db, "d2")["shares_delta"] == "50"
        w1 = self._get(test_db, "w1")
        assert (w1["status"], w1["error"], w1["shares_delta"]) == ("completed", None, "-30")
        # Charted when it landed, not when the repair ran.
        assert w1["updated_at"] == w1["settled_at"] == 1_000_000 + 30
        assert self._get(test_db, "d3")["status"] == "undeployed"
        assert self._get(test_db, "reverted")["status"] == "failed"
        assert self._get(test_db, "dropped")["status"] == "failed"
        assert _settled_shares(POOL_ID_HEX) == 170

    async def test_rows_in_flight_are_left_for_recovery(self, test_db):
        rows = [self._row("d1", block=10, status="pending")]
        service, saph = self._service(rows, chain_total=100)

        await service.repair_share_ledger()

        saph.w3.eth.get_transaction_receipt.assert_not_called()
        assert self._get(test_db, "d1")["status"] == "pending"

    async def test_a_second_run_changes_nothing(self, test_db):
        rows = [self._row("d1", block=10, shares_delta="300"),
                self._row("d2", block=20, shares_delta="50"),
                self._row("w1", block=30, operation="withdraw", shares_delta="-30"),
                self._row("d3", block=40, shares_delta="50")]
        service, saph = self._service(rows)
        await service.repair_share_ledger()
        saph.w3.eth.get_transaction_receipt.reset_mock()

        await service.repair_share_ledger()

        saph.w3.eth.get_transaction_receipt.assert_not_called()

    async def test_a_pool_that_reconciles_is_left_alone(self, test_db):
        rows = [self._row("d1", block=10, shares_delta="170")]
        service, saph = self._service(rows)

        await service.repair_share_ledger()

        saph.w3.eth.get_transaction_receipt.assert_not_called()

    @pytest.mark.parametrize("status,shares_delta,unavailable", [
        ("completed", "300", ConnectionError("rpc down")),
        ("completed", "300", "not-found"),
        # A wrongly failed row the node has not caught up on yet.
        ("failed", None, "not-found"),
    ])
    async def test_an_unread_receipt_is_retried_on_the_next_pass(
        self, test_db, status, shares_delta, unavailable
    ):
        from web3.exceptions import TransactionNotFound
        rows = [self._row("d1", block=10, status=status, shares_delta=shares_delta)]
        service, saph = self._service(rows, chain_total=100)
        receipt = saph.w3.eth.get_transaction_receipt.side_effect
        first = TransactionNotFound("0xd1") if unavailable == "not-found" else unavailable
        saph.w3.eth.get_transaction_receipt.side_effect = [first, receipt("0xd1")]

        await service.repair_share_ledger()
        assert self._get(test_db, "d1")["shares_delta"] == shares_delta

        await service.repair_share_ledger()
        assert self._get(test_db, "d1")["shares_delta"] == "100"
        assert self._get(test_db, "d1")["status"] != "failed"

    async def test_a_mismatch_no_receipt_explains_is_not_rescanned(self, test_db):
        rows = [self._row("d1", block=10, shares_delta="100")]
        # The chain holds 50 shares no row accounts for.
        service, saph = self._service(rows, chain_total=150)
        await service.repair_share_ledger()
        saph.w3.eth.get_transaction_receipt.reset_mock()

        await service.repair_share_ledger()

        saph.w3.eth.get_transaction_receipt.assert_not_called()

    async def test_rows_sharing_a_block_are_skipped(self, test_db):
        rows = [self._row("d1", block=10, shares_delta="1"),
                self._row("d2", block=10, shares_delta="2")]
        service, _ = self._service(rows, chain_total=100)

        await service.repair_share_ledger()

        assert self._get(test_db, "d1")["shares_delta"] == "1"
        assert self._get(test_db, "d2")["shares_delta"] == "2"


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
            external=105, idle=0, seeded=0, shares=10, on_chain_assets=110,
        )

        with patch("src.services.earn.vault_service.sign_transfer", return_value="0x" + "bb" * 65):
            result = await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "10", 0, USER_WITHDRAW_SIG)

        # Users must always be able to leave a pool with no senior claim on it.
        assert result["status"] == "completed"
        strategy.withdraw_from_earn.assert_awaited_once_with(10)


class TestDeployIdle:
    @staticmethod
    def _service(*, idle, minimum=0, healthy=True, name="midas-mtbill"):
        from src.services.earn.registry import StrategyRegistry

        registry = StrategyRegistry()
        strategy = MagicMock()
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
        return service, strategy

    async def test_routes_the_whole_idle_balance(self, test_db):
        service, strategy = self._service(idle=100_000)

        assert await service.deploy_idle(POOL_ID_HEX) == 100_000

        strategy.deposit_to_earn.assert_awaited_once_with(100_000)

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

        await service.deploy_idle(POOL_ID_HEX)

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

        assert await service.deploy_idle(POOL_ID_HEX) == 5_000_000
        strategy.deposit_to_earn.assert_awaited_once_with(5_000_000)

    async def test_deploys_idle_and_stranded_funds_together_against_the_minimum(self, test_db):
        service, strategy = self._service(idle=600_000, minimum=1_000_000)
        strategy.stranded_assets = AsyncMock(side_effect=[600_000, 0])

        assert await service.deploy_idle(POOL_ID_HEX) == 1_200_000
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

        await service.deploy_idle(POOL_ID_HEX)

        row = get_db().execute("SELECT status FROM earn_transactions WHERE id = 'stuck'").fetchone()
        assert row[0] == "undeployed"

    async def test_stands_down_while_a_bridge_is_in_flight(self, test_db):
        service, strategy = self._service(idle=600_000)
        strategy.in_flight_assets = AsyncMock(return_value=400_000)

        assert await service.deploy_idle(POOL_ID_HEX) == 0
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

        assert await service.deploy_idle(POOL_ID_HEX) == 0

        row = get_db().execute("SELECT status FROM earn_transactions WHERE id = 'stuck'").fetchone()
        assert row[0] == "completed"

    async def test_holds_the_pool_lock_across_the_bridge(self, test_db):
        service, strategy = self._service(idle=100_000)
        held = []
        strategy.deposit_to_earn = AsyncMock(
            side_effect=lambda _a: held.append(service._pools_tx_lock.locked())
        )

        await service.deploy_idle(POOL_ID_HEX)

        # A withdrawal's reclaim must not be deployed out from under it.
        assert held == [True]
        assert not service._pools_tx_lock.locked()

    async def test_does_nothing_when_there_is_nothing_idle(self, test_db):
        service, strategy = self._service(idle=0)

        assert await service.deploy_idle(POOL_ID_HEX) == 0

        strategy.deposit_to_earn.assert_not_awaited()

    async def test_leaves_amounts_below_the_protocol_minimum(self, test_db):
        service, strategy = self._service(idle=500_000, minimum=1_000_000)

        assert await service.deploy_idle(POOL_ID_HEX) == 0

        strategy.deposit_to_earn.assert_not_awaited()

    async def test_leaves_the_funds_when_the_strategy_is_unhealthy(self, test_db):
        service, strategy = self._service(idle=100_000, healthy=False)

        assert await service.deploy_idle(POOL_ID_HEX) == 0

        strategy.deposit_to_earn.assert_not_awaited()

    async def test_manual_pools_have_nothing_to_deploy_into(self, test_db):
        service, strategy = self._service(idle=100_000, name="manual")

        assert await service.deploy_idle(POOL_ID_HEX) == 0

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
        """A withdraw reclaims from the strategy before the contract checks
        consent, so an unverified one is a way to make the pool redeem and roll
        back on demand. The consent recovers without a chain read, so it is
        bound here rather than at execution."""
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
