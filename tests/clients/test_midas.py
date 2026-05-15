from unittest.mock import MagicMock, patch

import pytest

from src.models.settings import Settings


TEST_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TEST_ISSUANCE_VAULT = "0x8978e327FE7C72Fa4eaF4649C23147E279ae1470"
TEST_REDEMPTION_VAULT = "0x2a8c22E3b10036f3AEF5875d04f8441d4188b656"
TEST_MTBILL = "0xDD629E5241CbC5919847783e6C96B2De4754e438"
TEST_ORACLE = "0x70E58b7A1c884fFFE7dbce5249337603a28b8422"
TEST_LP_SK = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_LP_ADDRESS = "0xd8991364507FAfC256EafF950d28618735753476"


def _make_client(with_signer: bool = False):
    settings_kwargs = {
        "base_mainnet_rpc_url": "http://localhost:8545",
        "midas_issuance_vault_address": TEST_ISSUANCE_VAULT,
        "midas_redemption_vault_address": TEST_REDEMPTION_VAULT,
        "midas_mtbill_token_address": TEST_MTBILL,
        "midas_oracle_address": TEST_ORACLE,
    }
    if with_signer:
        settings_kwargs["liquidity_provider_secret_key"] = TEST_LP_SK
    settings = Settings(**settings_kwargs)

    with patch("src.clients.midas.load_settings") as mock_settings, \
         patch("src.clients.midas.Web3") as mock_web3_cls:
        mock_settings.return_value = settings

        w3 = MagicMock()
        mock_web3_cls.return_value = w3
        mock_web3_cls.HTTPProvider = MagicMock()
        mock_web3_cls.to_checksum_address = lambda a: a

        issuance = MagicMock(name="issuance_vault")
        redemption = MagicMock(name="redemption_vault")
        mtbill = MagicMock(name="mtbill")
        oracle = MagicMock(name="oracle")
        w3.eth.contract.side_effect = [issuance, redemption, mtbill, oracle]

        from src.clients.midas import MidasClient
        client = MidasClient()
        w3.eth.contract.side_effect = None
        client.issuance_vault = issuance
        client.redemption_vault = redemption
        client.mtbill = mtbill
        client.oracle = oracle
        return client, {
            "issuance": issuance,
            "redemption": redemption,
            "mtbill": mtbill,
            "oracle": oracle,
            "w3": w3,
        }


def _attach_signer(client, address: str = TEST_LP_ADDRESS):
    """Substitute the eth_account-backed signer with a MagicMock so write tests
    don't rely on real signing math. Mirrors the test_aave.py pattern.
    """
    signed = MagicMock()
    signed.raw_transaction = b"\x01\x02"
    client._account = MagicMock()
    client._account.address = address
    client._account.sign_transaction.return_value = signed
    return signed


def _wire_write_chain(w3, tx_hash_byte: int = 0xAB, status: int = 1) -> None:
    w3.eth.get_transaction_count.return_value = 7
    w3.eth.gas_price = 10**9
    w3.eth.chain_id = 8453
    w3.eth.send_raw_transaction.return_value = bytes([tx_hash_byte] * 32)
    w3.eth.wait_for_transaction_receipt.return_value = {"status": status}


def test_get_oracle_answer_returns_latestAnswer():
    client, c = _make_client()
    c["oracle"].functions.latestAnswer.return_value.call.return_value = 1_002_345_678_901

    assert client.get_oracle_answer() == 1_002_345_678_901
    c["oracle"].functions.latestAnswer.assert_called_once_with()


def test_get_oracle_decimals_returns_uint8():
    client, c = _make_client()
    c["oracle"].functions.decimals.return_value.call.return_value = 18

    assert client.get_oracle_decimals() == 18


def test_get_oracle_round_returns_answer_and_updated_at():
    client, c = _make_client()
    c["oracle"].functions.latestRoundData.return_value.call.return_value = (
        12345,
        1_002_345_678_901,
        1_700_000_000,
        1_700_086_400,
        12345,
    )

    answer, updated_at = client.get_oracle_round()
    assert answer == 1_002_345_678_901
    assert updated_at == 1_700_086_400


def test_get_mtbill_balance_calls_balanceOf_with_holder():
    client, c = _make_client()
    c["mtbill"].functions.balanceOf.return_value.call.return_value = 5_765_982_791_670_100_899

    assert client.get_mtbill_balance(TEST_LP_ADDRESS) == 5_765_982_791_670_100_899
    c["mtbill"].functions.balanceOf.assert_called_once_with(TEST_LP_ADDRESS)


@pytest.mark.parametrize("paused_value", [True, False])
def test_is_issuance_paused_reflects_contract(paused_value):
    client, c = _make_client()
    c["issuance"].functions.paused.return_value.call.return_value = paused_value

    assert client.is_issuance_paused() is paused_value


@pytest.mark.parametrize("paused_value", [True, False])
def test_is_redemption_paused_reflects_contract(paused_value):
    client, c = _make_client()
    c["redemption"].functions.paused.return_value.call.return_value = paused_value

    assert client.is_redemption_paused() is paused_value


def test_get_redemption_instant_fee_bps_reads_redemption_vault():
    client, c = _make_client()
    c["redemption"].functions.instantFee.return_value.call.return_value = 25

    assert client.get_redemption_instant_fee_bps() == 25


def test_get_issuance_min_amount_reads_issuance_vault():
    client, c = _make_client()
    c["issuance"].functions.minAmount.return_value.call.return_value = 1_000_000

    assert client.get_issuance_min_amount() == 1_000_000


def test_get_redemption_min_amount_reads_redemption_vault():
    client, c = _make_client()
    c["redemption"].functions.minAmount.return_value.call.return_value = 5_000_000

    assert client.get_redemption_min_amount() == 5_000_000


def test_get_allowance_defaults_owner_to_signer():
    client, c = _make_client(with_signer=True)
    asset_contract = MagicMock()
    asset_contract.functions.allowance.return_value.call.return_value = 42
    c["w3"].eth.contract.return_value = asset_contract
    client._account = MagicMock()
    client._account.address = TEST_LP_ADDRESS

    assert client.get_allowance(TEST_USDC, TEST_ISSUANCE_VAULT) == 42
    asset_contract.functions.allowance.assert_called_once_with(TEST_LP_ADDRESS, TEST_ISSUANCE_VAULT)


def test_get_allowance_accepts_explicit_owner():
    client, c = _make_client(with_signer=True)
    asset_contract = MagicMock()
    asset_contract.functions.allowance.return_value.call.return_value = 7
    c["w3"].eth.contract.return_value = asset_contract
    other_owner = "0x00000000000000000000000000000000000000bb"

    assert client.get_allowance(TEST_USDC, TEST_ISSUANCE_VAULT, owner=other_owner) == 7
    asset_contract.functions.allowance.assert_called_once_with(other_owner, TEST_ISSUANCE_VAULT)


def test_account_address_without_signer_raises():
    client, _ = _make_client(with_signer=False)

    with pytest.raises(RuntimeError, match="no signer configured"):
        _ = client.account_address


def test_deposit_instant_without_signer_raises():
    client, _ = _make_client(with_signer=False)

    with pytest.raises(RuntimeError, match="no signer configured"):
        client.deposit_instant(TEST_USDC, 1_000_000, 950_000)


def test_redeem_instant_without_signer_raises():
    client, _ = _make_client(with_signer=False)

    with pytest.raises(RuntimeError, match="no signer configured"):
        client.redeem_instant(TEST_USDC, 10**18, 950_000)


def test_deposit_instant_rejects_bad_referrer_id_length():
    client, _ = _make_client(with_signer=True)

    with pytest.raises(ValueError, match="32 bytes"):
        client.deposit_instant(TEST_USDC, 1_000_000, 950_000, referrer_id=b"\x00\x01")


def test_deposit_instant_builds_and_sends_signed_tx():
    client, c = _make_client(with_signer=True)
    _wire_write_chain(c["w3"])
    c["issuance"].functions.depositInstant.return_value.build_transaction.return_value = {}
    signed = _attach_signer(client)

    tx_hash = client.deposit_instant(TEST_USDC, 6_106_564, 5_700_000_000_000_000_000)

    c["issuance"].functions.depositInstant.assert_called_once_with(
        TEST_USDC, 6_106_564, 5_700_000_000_000_000_000, b"\x00" * 32,
    )
    client._account.sign_transaction.assert_called_once()
    c["w3"].eth.send_raw_transaction.assert_called_once_with(signed.raw_transaction)
    assert tx_hash.startswith("0x")


def test_deposit_instant_threads_custom_referrer_id():
    client, c = _make_client(with_signer=True)
    _wire_write_chain(c["w3"])
    c["issuance"].functions.depositInstant.return_value.build_transaction.return_value = {}
    _attach_signer(client)

    custom_ref = bytes.fromhex("aa" * 32)
    client.deposit_instant(TEST_USDC, 1_000_000, 950_000_000_000_000_000, referrer_id=custom_ref)

    c["issuance"].functions.depositInstant.assert_called_once_with(
        TEST_USDC, 1_000_000, 950_000_000_000_000_000, custom_ref,
    )


def test_redeem_instant_builds_and_sends_signed_tx():
    client, c = _make_client(with_signer=True)
    _wire_write_chain(c["w3"])
    c["redemption"].functions.redeemInstant.return_value.build_transaction.return_value = {}
    _attach_signer(client)

    client.redeem_instant(TEST_USDC, 10_069_393_621_017_422_785, 9_900_000)

    c["redemption"].functions.redeemInstant.assert_called_once_with(
        TEST_USDC, 10_069_393_621_017_422_785, 9_900_000,
    )


def test_approve_calls_erc20_with_spender_and_amount():
    client, c = _make_client(with_signer=True)
    _wire_write_chain(c["w3"])
    asset_contract = MagicMock()
    asset_contract.functions.approve.return_value.build_transaction.return_value = {}
    c["w3"].eth.contract.return_value = asset_contract
    _attach_signer(client)

    client.approve(TEST_USDC, TEST_ISSUANCE_VAULT, 999)

    asset_contract.functions.approve.assert_called_once_with(TEST_ISSUANCE_VAULT, 999)


def test_transfer_erc20_calls_erc20_transfer():
    client, c = _make_client(with_signer=True)
    _wire_write_chain(c["w3"])
    asset_contract = MagicMock()
    asset_contract.functions.transfer.return_value.build_transaction.return_value = {}
    c["w3"].eth.contract.return_value = asset_contract
    _attach_signer(client)

    recipient = "0x00000000000000000000000000000000000000bb"
    client.transfer_erc20(TEST_USDC, recipient, 1_500_000)

    asset_contract.functions.transfer.assert_called_once_with(recipient, 1_500_000)


def test_write_tx_raises_on_reverted_receipt():
    client, c = _make_client(with_signer=True)
    _wire_write_chain(c["w3"], status=0)
    c["issuance"].functions.depositInstant.return_value.build_transaction.return_value = {}
    _attach_signer(client)

    with pytest.raises(RuntimeError, match="depositInstant tx reverted"):
        client.deposit_instant(TEST_USDC, 1_000_000, 950_000_000_000_000_000)
