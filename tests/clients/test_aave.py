from unittest.mock import MagicMock, patch

import pytest

from src.models.settings import Settings


TEST_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TEST_ATOKEN = "0x0000000000000000000000000000000000000aaa"
TEST_LP_SK = "0x7b07a59f24f1900ec4e6ac3e521c1acd2cca3518f717abda1dc8bbcbbc344c4e"
TEST_LP_ADDRESS = "0xd8991364507FAfC256EafF950d28618735753476"
POOL_ADDRESS = "0x8bAB6d1b75f19e9eD9fCe8b9BD338844fF79aE27"
RAY = 10**27


def _make_client(with_signer: bool = False):
    settings_kwargs = {
        "base_sepolia_rpc_url": "http://localhost:8545",
        "aave_pool_address": POOL_ADDRESS,
    }
    if with_signer:
        settings_kwargs["liquidity_provider_secret_key"] = TEST_LP_SK
    settings = Settings(**settings_kwargs)

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
        return client, pool, w3


def _reserve_data(liquidity_rate_ray: int = 0, atoken_address: str = TEST_ATOKEN):
    return (
        (0,),
        0,
        liquidity_rate_ray,
        0, 0, 0, 0, 0,
        atoken_address,
        "0x0000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000000",
        "0x0000000000000000000000000000000000000000",
        0, 0, 0,
    )


def test_get_supply_apy_bps_5_percent():
    client, pool, _ = _make_client()
    five_percent_ray = RAY * 5 // 100
    pool.functions.getReserveData.return_value.call.return_value = _reserve_data(five_percent_ray)

    assert client.get_supply_apy_bps(TEST_USDC) == 500


def test_get_supply_apy_bps_zero_rate():
    client, pool, _ = _make_client()
    pool.functions.getReserveData.return_value.call.return_value = _reserve_data(0)

    assert client.get_supply_apy_bps(TEST_USDC) == 0


def test_get_supply_apy_bps_fractional_rate():
    client, pool, _ = _make_client()
    rate = RAY * 375 // 10000
    pool.functions.getReserveData.return_value.call.return_value = _reserve_data(rate)

    assert client.get_supply_apy_bps(TEST_USDC) == 375


def test_get_supply_apy_bps_calls_pool_with_checksummed_asset():
    client, pool, _ = _make_client()
    pool.functions.getReserveData.return_value.call.return_value = _reserve_data(0)

    client.get_supply_apy_bps(TEST_USDC)

    pool.functions.getReserveData.assert_called_once_with(TEST_USDC)


def test_get_aToken_address_returns_reserve_field():
    client, pool, _ = _make_client()
    pool.functions.getReserveData.return_value.call.return_value = _reserve_data(atoken_address=TEST_ATOKEN)

    assert client.get_aToken_address(TEST_USDC) == TEST_ATOKEN


def test_get_aToken_balance_reads_balanceOf():
    client, pool, w3 = _make_client()
    pool.functions.getReserveData.return_value.call.return_value = _reserve_data(atoken_address=TEST_ATOKEN)
    atoken_contract = MagicMock()
    atoken_contract.functions.balanceOf.return_value.call.return_value = 1_234_567
    w3.eth.contract.return_value = atoken_contract
    w3.eth.contract.side_effect = None

    assert client.get_aToken_balance(TEST_USDC, TEST_LP_ADDRESS) == 1_234_567
    atoken_contract.functions.balanceOf.assert_called_once_with(TEST_LP_ADDRESS)


def test_supply_without_signer_raises():
    client, _, _ = _make_client(with_signer=False)

    with pytest.raises(RuntimeError, match="no signer configured"):
        client.supply(TEST_USDC, 1_000_000)


def test_supply_builds_and_sends_signed_tx():
    client, pool, w3 = _make_client(with_signer=True)
    w3.eth.get_transaction_count.return_value = 7
    w3.eth.gas_price = 10**9
    w3.eth.chain_id = 84532
    w3.eth.send_raw_transaction.return_value = bytes.fromhex("ab" * 32)
    w3.eth.wait_for_transaction_receipt.return_value = {"status": 1}

    tx_built = {"from": TEST_LP_ADDRESS, "nonce": 7}
    pool.functions.supply.return_value.build_transaction.return_value = tx_built

    signed = MagicMock()
    signed.raw_transaction = b"\x01\x02"
    client._account = MagicMock()
    client._account.address = TEST_LP_ADDRESS
    client._account.sign_transaction.return_value = signed

    tx_hash = client.supply(TEST_USDC, 500_000)

    pool.functions.supply.assert_called_once_with(TEST_USDC, 500_000, TEST_LP_ADDRESS, 0)
    client._account.sign_transaction.assert_called_once_with(tx_built)
    w3.eth.send_raw_transaction.assert_called_once_with(b"\x01\x02")
    assert tx_hash.startswith("0x")


def test_withdraw_defaults_recipient_to_signer():
    client, pool, w3 = _make_client(with_signer=True)
    w3.eth.get_transaction_count.return_value = 1
    w3.eth.gas_price = 10**9
    w3.eth.chain_id = 84532
    w3.eth.send_raw_transaction.return_value = bytes.fromhex("cd" * 32)
    w3.eth.wait_for_transaction_receipt.return_value = {"status": 1}
    pool.functions.withdraw.return_value.build_transaction.return_value = {}

    signed = MagicMock()
    signed.raw_transaction = b"\x03"
    client._account = MagicMock()
    client._account.address = TEST_LP_ADDRESS
    client._account.sign_transaction.return_value = signed

    client.withdraw(TEST_USDC, 250_000)

    pool.functions.withdraw.assert_called_once_with(TEST_USDC, 250_000, TEST_LP_ADDRESS)


def test_withdraw_uses_explicit_recipient():
    client, pool, w3 = _make_client(with_signer=True)
    w3.eth.get_transaction_count.return_value = 1
    w3.eth.gas_price = 10**9
    w3.eth.chain_id = 84532
    w3.eth.send_raw_transaction.return_value = bytes.fromhex("ef" * 32)
    w3.eth.wait_for_transaction_receipt.return_value = {"status": 1}
    pool.functions.withdraw.return_value.build_transaction.return_value = {}

    signed = MagicMock()
    signed.raw_transaction = b"\x04"
    client._account = MagicMock()
    client._account.address = TEST_LP_ADDRESS
    client._account.sign_transaction.return_value = signed

    custom_to = "0x00000000000000000000000000000000000000bb"
    client.withdraw(TEST_USDC, 100, to=custom_to)

    pool.functions.withdraw.assert_called_once_with(TEST_USDC, 100, custom_to)


def test_supply_raises_on_reverted_receipt():
    client, pool, w3 = _make_client(with_signer=True)
    w3.eth.get_transaction_count.return_value = 1
    w3.eth.gas_price = 10**9
    w3.eth.chain_id = 84532
    w3.eth.send_raw_transaction.return_value = bytes.fromhex("aa" * 32)
    w3.eth.wait_for_transaction_receipt.return_value = {"status": 0}
    pool.functions.supply.return_value.build_transaction.return_value = {}

    signed = MagicMock()
    signed.raw_transaction = b"\x05"
    client._account = MagicMock()
    client._account.address = TEST_LP_ADDRESS
    client._account.sign_transaction.return_value = signed

    with pytest.raises(RuntimeError, match="supply tx reverted"):
        client.supply(TEST_USDC, 1)


def test_approve_pool_targets_asset_contract():
    client, pool, w3 = _make_client(with_signer=True)
    asset_contract = MagicMock()
    w3.eth.contract.return_value = asset_contract
    w3.eth.contract.side_effect = None
    asset_contract.functions.approve.return_value.build_transaction.return_value = {}
    w3.eth.get_transaction_count.return_value = 1
    w3.eth.gas_price = 10**9
    w3.eth.chain_id = 84532
    w3.eth.send_raw_transaction.return_value = bytes.fromhex("bc" * 32)
    w3.eth.wait_for_transaction_receipt.return_value = {"status": 1}

    signed = MagicMock()
    signed.raw_transaction = b"\x06"
    client._account = MagicMock()
    client._account.address = TEST_LP_ADDRESS
    client._account.sign_transaction.return_value = signed

    client.approve_pool(TEST_USDC, 999)

    asset_contract.functions.approve.assert_called_once_with(POOL_ADDRESS, 999)


def test_get_allowance_reads_from_asset_contract():
    client, pool, w3 = _make_client(with_signer=True)
    asset_contract = MagicMock()
    asset_contract.functions.allowance.return_value.call.return_value = 42
    w3.eth.contract.return_value = asset_contract
    w3.eth.contract.side_effect = None
    client._account = MagicMock()
    client._account.address = TEST_LP_ADDRESS

    assert client.get_allowance(TEST_USDC) == 42
    asset_contract.functions.allowance.assert_called_once_with(TEST_LP_ADDRESS, POOL_ADDRESS)
