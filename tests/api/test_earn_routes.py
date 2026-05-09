from unittest.mock import AsyncMock, MagicMock, patch


USDC_TOKEN_ID = "0x330ba47d00c7ce3018deee017b319fd7cc6473a2ddc9e6eba6ebb4207be15279"
POOL_ID = "0x" + "ab" * 32
POOL_ADDRESS = "0x152E6a7125665764a4F1F1df80E8f5D49Bf0239c"
USER_ADDRESS = "0x" + "d" * 40


def _mock_pool():
    return {
        "pool_id": POOL_ID,
        "token_id": USDC_TOKEN_ID,
        "pool_address": POOL_ADDRESS,
        "total_shares": 1000,
        "total_assets": 1050,
        "active": True,
    }


def _mock_service(**overrides) -> MagicMock:
    svc = MagicMock()
    svc.effective_total_assets = AsyncMock(side_effect=lambda _pool_id, on_chain: on_chain)
    svc.strategy_apy_bps_safe = AsyncMock(return_value=0)
    for k, v in overrides.items():
        setattr(svc, k, v)
    return svc


class TestListPoolsRoute:
    async def test_returns_200_with_pools(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = _mock_service()
            svc.list_pools.return_value = [_mock_pool()]
            mock_svc.return_value = svc

            r = await api_client.get("/v1/earn/pools")

            assert r.status_code == 200
            data = r.json()
            assert len(data["pools"]) == 1
            assert data["pools"][0]["pool_id"] == POOL_ID
            assert data["pools"][0]["status"] == "active"

    async def test_returns_empty_list(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = _mock_service()
            svc.list_pools.return_value = []
            mock_svc.return_value = svc

            r = await api_client.get("/v1/earn/pools")
            assert r.status_code == 200
            assert r.json()["pools"] == []

    async def test_paused_pool_shows_status(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            pool = _mock_pool()
            pool["active"] = False
            svc = _mock_service()
            svc.list_pools.return_value = [pool]
            mock_svc.return_value = svc

            r = await api_client.get("/v1/earn/pools")
            assert r.json()["pools"][0]["status"] == "paused"

    async def test_returns_500_on_error(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = _mock_service()
            svc.list_pools.side_effect = RuntimeError("rpc down")
            mock_svc.return_value = svc

            r = await api_client.get("/v1/earn/pools")
            assert r.status_code == 500

    async def test_total_assets_reflects_strategy_live_aum(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.list_pools.return_value = [_mock_pool()]
            svc.effective_total_assets = AsyncMock(return_value=1100)
            svc.strategy_apy_bps_safe = AsyncMock(return_value=0)
            mock_svc.return_value = svc

            r = await api_client.get("/v1/earn/pools")
            assert r.json()["pools"][0]["total_assets"] == "1100"

    async def test_apy_bps_reflects_strategy_value(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = _mock_service()
            svc.list_pools.return_value = [_mock_pool()]
            svc.strategy_apy_bps_safe = AsyncMock(return_value=487)
            mock_svc.return_value = svc

            r = await api_client.get("/v1/earn/pools")
            assert r.json()["pools"][0]["apy_bps"] == 487


class TestGetPoolRoute:
    async def test_returns_200_for_existing_pool(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = _mock_service()
            svc.get_pool.return_value = _mock_pool()
            mock_svc.return_value = svc

            r = await api_client.get(f"/v1/earn/pools/{POOL_ID}")
            assert r.status_code == 200
            data = r.json()
            assert data["pool_id"] == POOL_ID
            assert data["total_shares"] == "1000"
            assert data["total_assets"] == "1050"

    async def test_returns_404_for_missing_pool(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = _mock_service()
            pool = _mock_pool()
            pool["pool_address"] = "0x0000000000000000000000000000000000000000"
            svc.get_pool.return_value = pool
            mock_svc.return_value = svc

            r = await api_client.get(f"/v1/earn/pools/{POOL_ID}")
            assert r.status_code == 404


class TestDepositQuoteRoute:
    async def test_returns_200_with_quote(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.get_deposit_quote = AsyncMock(return_value={
                "quote_id": "11111111-1111-1111-1111-111111111111",
                "pool_id": POOL_ID,
                "token_id": USDC_TOKEN_ID,
                "amount": "1000",
                "shares_estimate": "952",
                "exchange_rate": "1.05",
                "pool_address": POOL_ADDRESS,
                "transfer_nonce": 5,
                "expires_at": 9999999999,
            })
            mock_svc.return_value = svc

            r = await api_client.get("/v1/earn/quote", params={
                "pool_id": POOL_ID,
                "amount": "1000",
                "user_address": USER_ADDRESS,
            })
            assert r.status_code == 200
            assert r.json()["shares_estimate"] == "952"

    async def test_returns_400_on_invalid_input(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.get_deposit_quote = AsyncMock(side_effect=ValueError("Pool not found"))
            mock_svc.return_value = svc

            r = await api_client.get("/v1/earn/quote", params={
                "pool_id": POOL_ID,
                "amount": "1000",
                "user_address": USER_ADDRESS,
            })
            assert r.status_code == 400


class TestDepositRoute:
    async def test_returns_200_on_success(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.deposit = AsyncMock(return_value={
                "pool_id": POOL_ID,
                "amount": "1000",
                "shares_minted": "952",
                "exchange_rate": "1.05",
                "tx_hash": "0x" + "ff" * 32,
                "status": "completed",
            })
            mock_svc.return_value = svc

            r = await api_client.post("/v1/earn/deposit", json={
                "pool_id": POOL_ID,
                "user_address": USER_ADDRESS,
                "amount": "1000",
                "nonce": 0,
                "signature": "0x" + "aa" * 65,
            })
            assert r.status_code == 200
            assert r.json()["shares_minted"] == "952"

    async def test_returns_400_on_value_error(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.deposit = AsyncMock(side_effect=ValueError("Pool is not active"))
            mock_svc.return_value = svc

            r = await api_client.post("/v1/earn/deposit", json={
                "pool_id": POOL_ID,
                "user_address": USER_ADDRESS,
                "amount": "1000",
                "nonce": 0,
                "signature": "0x" + "aa" * 65,
            })
            assert r.status_code == 400


class TestWithdrawRoute:
    async def test_returns_200_on_success(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.withdraw = AsyncMock(return_value={
                "pool_id": POOL_ID,
                "amount": "500",
                "shares_burned": "476",
                "exchange_rate": "1.05",
                "tx_hash": "0x" + "ff" * 32,
                "status": "completed",
            })
            mock_svc.return_value = svc

            r = await api_client.post("/v1/earn/withdraw", json={
                "pool_id": POOL_ID,
                "user_address": USER_ADDRESS,
                "amount": "500",
                "nonce": 0,
                "signature": "0x" + "cc" * 65,
            })
            assert r.status_code == 200
            assert r.json()["shares_burned"] == "476"

    async def test_returns_400_on_insufficient_shares(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.withdraw = AsyncMock(side_effect=ValueError("Insufficient shares"))
            mock_svc.return_value = svc

            r = await api_client.post("/v1/earn/withdraw", json={
                "pool_id": POOL_ID,
                "user_address": USER_ADDRESS,
                "amount": "999999999",
                "nonce": 0,
                "signature": "0x" + "cc" * 65,
            })
            assert r.status_code == 400

    async def test_returns_422_when_signature_missing(self, api_client):
        # Pydantic-level guard: missing the new fields should be a structural
        # rejection, not silently accepted as the old shape.
        r = await api_client.post("/v1/earn/withdraw", json={
            "pool_id": POOL_ID,
            "user_address": USER_ADDRESS,
            "amount": "500",
        })
        assert r.status_code == 422


class TestBalanceRoute:
    async def test_returns_200_with_positions(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.get_all_balances = AsyncMock(return_value=[{
                "pool_id": POOL_ID,
                "token_id": USDC_TOKEN_ID,
                "shares": "500",
                "underlying_amount": "525",
                "exchange_rate": "1.05",
            }])
            mock_svc.return_value = svc

            r = await api_client.get("/v1/earn/balance", params={"token": "0x" + "ee" * 32})
            assert r.status_code == 200
            data = r.json()
            assert len(data["positions"]) == 1
            assert data["positions"][0]["shares"] == "500"

    async def test_returns_empty_list(self, api_client):
        with patch("src.api.earn.get_vault_service") as mock_svc:
            svc = MagicMock()
            svc.get_all_balances = AsyncMock(return_value=[])
            mock_svc.return_value = svc

            r = await api_client.get("/v1/earn/balance", params={"token": "0x" + "ee" * 32})
            assert r.status_code == 200
            assert r.json()["positions"] == []
