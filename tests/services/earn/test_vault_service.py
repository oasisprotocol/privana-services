import pytest

from src.models.earn import PoolStatus
from src.services.earn.vault_service import VaultService


USDC_TOKEN_ID = "0x330ba47d00c7ce3018deee017b319fd7cc6473a2ddc9e6eba6ebb4207be15279"
WETH_TOKEN_ID = "0x335b5cccd1e63b2fe79863a0db73fce430e4e66902e2b78424f8662621e29fb7"
POOL_ADDRESS = "0x" + "ab" * 20


class TestCreatePool:
    def test_creates_pool_with_correct_fields(self, test_db):
        svc = VaultService()
        pool = svc.create_pool(USDC_TOKEN_ID, "aave-v3", POOL_ADDRESS)
        assert pool.token_id == USDC_TOKEN_ID
        assert pool.strategy == "aave-v3"
        assert pool.pool_address == POOL_ADDRESS.lower()
        assert pool.total_shares == "0"
        assert pool.total_assets == "0"
        assert pool.apy_bps == 0
        assert pool.status == PoolStatus.ACTIVE.value

    def test_pool_id_contains_token_and_strategy(self, test_db):
        svc = VaultService()
        pool = svc.create_pool(USDC_TOKEN_ID, "aave-v3", POOL_ADDRESS)
        assert "aave-v3" in pool.id
        assert USDC_TOKEN_ID[:10] in pool.id

    def test_duplicate_pool_raises(self, test_db):
        svc = VaultService()
        svc.create_pool(USDC_TOKEN_ID, "aave-v3", POOL_ADDRESS)
        with pytest.raises(Exception):
            svc.create_pool(USDC_TOKEN_ID, "aave-v3", POOL_ADDRESS)


class TestGetPool:
    def test_returns_existing_pool(self, test_db):
        svc = VaultService()
        created = svc.create_pool(USDC_TOKEN_ID, "aave-v3", POOL_ADDRESS)
        fetched = svc.get_pool(created.id)
        assert fetched.id == created.id
        assert fetched.token_id == USDC_TOKEN_ID

    def test_nonexistent_pool_raises(self, test_db):
        svc = VaultService()
        with pytest.raises(ValueError, match="not found"):
            svc.get_pool("nonexistent")


class TestListPools:
    def test_returns_all_pools(self, test_db):
        svc = VaultService()
        svc.create_pool(USDC_TOKEN_ID, "aave-v3", POOL_ADDRESS)
        svc.create_pool(WETH_TOKEN_ID, "aave-v3", POOL_ADDRESS)
        pools = svc.list_pools()
        assert len(pools) == 2

    def test_returns_empty_when_no_pools(self, test_db):
        svc = VaultService()
        pools = svc.list_pools()
        assert pools == []

    def test_filters_by_status(self, test_db):
        svc = VaultService()
        svc.create_pool(USDC_TOKEN_ID, "aave-v3", POOL_ADDRESS)
        pools = svc.list_pools(status="active")
        assert len(pools) == 1
        pools = svc.list_pools(status="paused")
        assert len(pools) == 0
