from unittest.mock import MagicMock, patch

from src.models.settings import Settings


TEST_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
RAY = 10**27


def _make_client():
    settings = Settings(
        base_sepolia_rpc_url="http://localhost:8545",
        aave_pool_address="0x8bAB6d1b75f19e9eD9fCe8b9BD338844fF79aE27",
    )

    with patch("src.clients.aave.load_settings") as mock_settings, \
         patch("src.clients.aave.Web3") as mock_web3_cls:
        mock_settings.return_value = settings

        w3 = MagicMock()
        pool = MagicMock()
        w3.eth.contract.return_value = pool
        mock_web3_cls.return_value = w3
        mock_web3_cls.HTTPProvider = MagicMock()
        mock_web3_cls.to_checksum_address = lambda a: a

        from src.clients.aave import AaveClient
        client = AaveClient()
        client.pool = pool
        return client, pool


def _reserve_data(liquidity_rate_ray: int):
    return (
        (0,),
        0,
        liquidity_rate_ray,
        0, 0, 0, 0, 0,
        "0x0000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000000",
        0, 0, 0,
    )


def test_get_supply_apy_bps_5_percent():
    client, pool = _make_client()
    five_percent_ray = RAY * 5 // 100
    pool.functions.getReserveData.return_value.call.return_value = _reserve_data(five_percent_ray)

    assert client.get_supply_apy_bps(TEST_USDC) == 500


def test_get_supply_apy_bps_zero_rate():
    client, pool = _make_client()
    pool.functions.getReserveData.return_value.call.return_value = _reserve_data(0)

    assert client.get_supply_apy_bps(TEST_USDC) == 0


def test_get_supply_apy_bps_fractional_rate():
    client, pool = _make_client()
    rate = RAY * 375 // 10000
    pool.functions.getReserveData.return_value.call.return_value = _reserve_data(rate)

    assert client.get_supply_apy_bps(TEST_USDC) == 375


def test_get_supply_apy_bps_calls_pool_with_checksummed_asset():
    client, pool = _make_client()
    pool.functions.getReserveData.return_value.call.return_value = _reserve_data(0)

    client.get_supply_apy_bps(TEST_USDC)

    pool.functions.getReserveData.assert_called_once_with(TEST_USDC)
