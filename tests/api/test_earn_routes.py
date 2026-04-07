from unittest.mock import MagicMock, patch

from src.models.earn import PoolRecord


MOCK_POOL = PoolRecord(
    id="0x330ba47d-aave-v3",
    token_id="0x330ba47d00c7ce3018deee017b319fd7cc6473a2ddc9e6eba6ebb4207be15279",
    strategy="aave-v3",
    total_shares="1000000",
    total_assets="1050000",
    pool_address="0x" + "ab" * 20,
    apy_bps=500,
    status="active",
    last_harvest_at=1000,
    created_at=900,
    updated_at=1000,
)


class TestListPools:
    async def test_returns_200_with_pools(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.list_pools.return_value = [MOCK_POOL]
            mock_svc.return_value = svc

            r = await api_client.get("/v1/earn/pools")

            assert r.status_code == 200
            data = r.json()
            assert len(data["pools"]) == 1
            assert data["pools"][0]["pool_id"] == MOCK_POOL.id
            assert data["pools"][0]["strategy"] == "aave-v3"
            assert data["pools"][0]["apy_bps"] == 500

    async def test_returns_empty_list(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.list_pools.return_value = []
            mock_svc.return_value = svc

            r = await api_client.get("/v1/earn/pools")

            assert r.status_code == 200
            assert r.json()["pools"] == []

    async def test_passes_status_filter(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.list_pools.return_value = []
            mock_svc.return_value = svc

            await api_client.get("/v1/earn/pools?status=paused")

            svc.list_pools.assert_called_once_with(status="paused")

    async def test_returns_500_on_unexpected_error(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.list_pools.side_effect = RuntimeError("db down")
            mock_svc.return_value = svc

            r = await api_client.get("/v1/earn/pools")

            assert r.status_code == 500


class TestGetPool:
    async def test_returns_200_for_existing_pool(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.get_pool.return_value = MOCK_POOL
            mock_svc.return_value = svc

            r = await api_client.get(f"/v1/earn/pools/{MOCK_POOL.id}")

            assert r.status_code == 200
            data = r.json()
            assert data["pool_id"] == MOCK_POOL.id
            assert data["total_shares"] == "1000000"
            assert data["total_assets"] == "1050000"
            assert data["pool_address"] == MOCK_POOL.pool_address

    async def test_returns_404_for_missing_pool(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.get_pool.side_effect = ValueError("Pool nonexistent not found")
            mock_svc.return_value = svc

            r = await api_client.get("/v1/earn/pools/nonexistent")

            assert r.status_code == 404

    async def test_returns_500_on_unexpected_error(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.get_pool.side_effect = RuntimeError("db down")
            mock_svc.return_value = svc

            r = await api_client.get(f"/v1/earn/pools/{MOCK_POOL.id}")

            assert r.status_code == 500
