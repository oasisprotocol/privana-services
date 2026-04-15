from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.settings import Settings


USDC_TOKEN_ID = "0x330ba47d00c7ce3018deee017b319fd7cc6473a2ddc9e6eba6ebb4207be15279"
POOL_ID_HEX = "0x" + "ab" * 32
POOL_ADDRESS = "0x152E6a7125665764a4F1F1df80E8f5D49Bf0239c"
USER_ADDRESS = "0xd8991364507FAfC256EafF950d28618735753476"


def _make_service():
    settings = Settings(
        earn_manager_contract_address="0x1111111111111111111111111111111111111111",
        liquidity_provider_private_key="0x4c0883a69102937d6231471b5dbb6204fe512961708279f69e0f0fcbf24b5830",
        liquidity_provider_address=POOL_ADDRESS,
        accounting_contract_address="0xFfB141bF8269E458b074A274bE6E8F971f08A401",
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

        from src.services.earn.vault_service import VaultService
        service = VaultService()
        service.contract = contract
        return service, contract, saph, acct


class TestGetPool:
    def test_returns_pool_data(self):
        service, contract, _, _ = _make_service()
        contract.functions.getPool.return_value.call.return_value = (
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
        contract.functions.getPool.return_value.call.return_value = (
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
        contract.functions.getPool.return_value.call.return_value = (
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

    async def test_rejects_missing_pool(self):
        service, contract, _, _ = _make_service()
        contract.functions.getPool.return_value.call.return_value = (
            b"\x00" * 32,
            "0x0000000000000000000000000000000000000000",
            0, 0, False,
        )
        with pytest.raises(ValueError, match="not found"):
            await service.get_deposit_quote(POOL_ID_HEX, "1000", USER_ADDRESS)

    async def test_rejects_inactive_pool(self):
        service, contract, _, _ = _make_service()
        contract.functions.getPool.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, False,
        )
        with pytest.raises(ValueError, match="not active"):
            await service.get_deposit_quote(POOL_ID_HEX, "1000", USER_ADDRESS)


class TestDeposit:
    async def test_successful_deposit(self, test_db):
        service, contract, saph, _ = _make_service()
        contract.functions.getPool.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        contract.functions.getUserShares.return_value.call.side_effect = [0, 952]

        result = await service.deposit(
            POOL_ID_HEX, USER_ADDRESS, "1000", 5, "0x" + "aa" * 65
        )
        assert result["shares_minted"] == "952"
        assert result["tx_hash"] == "0x" + "ff" * 32
        assert result["status"] == "completed"

        row = test_db.execute("SELECT * FROM earn_transactions").fetchone()
        assert row["operation"] == "deposit"
        assert row["signer_address"] == USER_ADDRESS.lower()
        assert row["nonce"] == 5
        assert row["signature"] == "0x" + "aa" * 65
        assert row["status"] == "completed"
        assert row["tx_hash"] == "0x" + "ff" * 32

    async def test_failed_deposit_marks_row(self, test_db):
        service, contract, saph, _ = _make_service()
        contract.functions.getPool.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        contract.functions.getUserShares.return_value.call.return_value = 0
        saph.execute_contract_call.side_effect = RuntimeError("onchain revert")

        with pytest.raises(RuntimeError, match="onchain revert"):
            await service.deposit(
                POOL_ID_HEX, USER_ADDRESS, "1000", 5, "0x" + "aa" * 65
            )

        row = test_db.execute("SELECT * FROM earn_transactions").fetchone()
        assert row["status"] == "failed"
        assert "onchain revert" in row["error"]


class TestWithdraw:
    async def test_insufficient_shares_raises(self):
        service, contract, _, _ = _make_service()
        contract.functions.getPool.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        contract.functions.getUserShares.return_value.call.return_value = 100
        contract.functions.convertToAssets.return_value.call.return_value = 105

        with pytest.raises(ValueError, match="Insufficient shares"):
            await service.withdraw(POOL_ID_HEX, USER_ADDRESS, "1000")


class TestGetAllBalances:
    @pytest.mark.asyncio
    async def test_returns_balances_for_user(self):
        service, contract, _, _ = _make_service()
        pool_id_bytes = bytes.fromhex(POOL_ID_HEX[2:])
        contract.functions.getPoolCount.return_value.call.return_value = 1
        contract.functions.poolIds.return_value.call.return_value = pool_id_bytes
        contract.functions.getPool.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        contract.functions.getUserShares.return_value.call.return_value = 500
        contract.functions.convertToAssets.return_value.call.return_value = 525

        balances = await service.get_all_balances(USER_ADDRESS)
        assert len(balances) == 1
        assert balances[0]["shares"] == "500"
        assert balances[0]["underlying_amount"] == "525"

    @pytest.mark.asyncio
    async def test_skips_zero_shares(self):
        service, contract, _, _ = _make_service()
        pool_id_bytes = bytes.fromhex(POOL_ID_HEX[2:])
        contract.functions.getPoolCount.return_value.call.return_value = 1
        contract.functions.poolIds.return_value.call.return_value = pool_id_bytes
        contract.functions.getPool.return_value.call.return_value = (
            bytes.fromhex(USDC_TOKEN_ID[2:]),
            POOL_ADDRESS,
            1000, 1050, True,
        )
        contract.functions.getUserShares.return_value.call.return_value = 0

        balances = await service.get_all_balances(USER_ADDRESS)
        assert balances == []
