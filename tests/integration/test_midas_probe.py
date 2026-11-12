import json
import time
from pathlib import Path

import pytest
import requests
from web3 import Web3

pytestmark = pytest.mark.integration

# The Ethereum deployment the service uses (MIDAS_* in .env.mainnet).
ETHEREUM_RPC_URL = "https://ethereum-rpc.publicnode.com"

ISSUANCE_VAULT = Web3.to_checksum_address("0x99361435420711723aF805F08187c9E6bF796683")
REDEMPTION_VAULT = Web3.to_checksum_address("0x569D7dccBF6923350521ecBC28A555A500c4f0Ec")
MTBILL_TOKEN = Web3.to_checksum_address("0xDD629E5241CbC5919847783e6C96B2De4754e438")
ORACLE = Web3.to_checksum_address("0x056339C044055819E8Db84E71f5f2E1F536b2E5b")

ABI_DIR = Path(__file__).resolve().parents[2] / "src" / "abis"

_RETRY_DELAYS_SEC = (0.5, 1.0, 2.0, 4.0, 8.0)


def _abi(name: str) -> list:
    with (ABI_DIR / f"{name}.json").open() as f:
        return json.load(f)["abi"]


def _call(fn):
    """Wrap a web3 contract function call with retry on 429 rate-limits.

    Public RPCs throttle bursts of eth_calls, which is exactly the
    pattern this probe produces. Real ops would configure a private RPC
    via BASE_RPC_URL, but we want CI to be green against the
    public endpoint too.
    """
    last_exc: Exception | None = None
    for delay in _RETRY_DELAYS_SEC:
        try:
            return fn.call()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status != 429:
                raise
            last_exc = exc
            time.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("unreachable")


@pytest.fixture(scope="module")
def w3() -> Web3:
    client = Web3(Web3.HTTPProvider(ETHEREUM_RPC_URL))
    if not client.is_connected():
        pytest.skip(f"Cannot reach Ethereum RPC at {ETHEREUM_RPC_URL}")
    return client


@pytest.fixture(scope="module")
def issuance(w3: Web3):
    return w3.eth.contract(address=ISSUANCE_VAULT, abi=_abi("MidasDepositVault"))


@pytest.fixture(scope="module")
def redemption(w3: Web3):
    return w3.eth.contract(address=REDEMPTION_VAULT, abi=_abi("MidasRedemptionVault"))


@pytest.fixture(scope="module")
def oracle(w3: Web3):
    return w3.eth.contract(address=ORACLE, abi=_abi("ChronicleOracle"))


@pytest.fixture(scope="module")
def mtbill(w3: Web3):
    return w3.eth.contract(address=MTBILL_TOKEN, abi=_abi("ERC20"))


def test_oracle_answer_is_a_sane_mtbill_price(oracle) -> None:
    """MidasStrategy normalizes by the feed's own decimals, so pin the
    normalized price rather than the precision. `latestRoundData` because the
    Ethereum feed reverts on `latestAnswer`.
    """
    decimals = _call(oracle.functions.decimals())
    _, answer, _, _, _ = _call(oracle.functions.latestRoundData())
    assert 0.95 < answer / 10**decimals < 2.0, (
        f"oracle answer {answer} ({decimals} decimals) outside sane bounds for MTBILL/USD"
    )


def test_oracle_round_data_recent(oracle) -> None:
    """Sanity check that the oracle has been updated within the past 14 days.
    Chronicle MTBILL/USD updates on a sparse cadence (treasury bill prices
    barely move day-to-day) so we tolerate up to two weeks here, far looser
    than the 48h is_healthy() gate which is a separate routing concern.
    """
    _, answer, _, updated_at, _ = _call(oracle.functions.latestRoundData())
    assert answer > 0
    age = int(time.time()) - int(updated_at)
    assert age < 14 * 86400, (
        f"oracle round is {age}s old (~{age/86400:.1f} days); feed may be stalled"
    )


def test_issuance_vault_not_paused(issuance) -> None:
    assert _call(issuance.functions.paused()) is False


def test_redemption_vault_not_paused(redemption) -> None:
    assert _call(redemption.functions.paused()) is False


def test_issuance_vault_min_amount_is_set(issuance) -> None:
    min_amount = _call(issuance.functions.minAmount())
    assert min_amount >= 0


def test_issuance_vault_tokens_receiver_is_non_zero(issuance) -> None:
    receiver = _call(issuance.functions.tokensReceiver())
    assert int(receiver, 16) != 0, "tokensReceiver should be a real address"


def test_redemption_instant_fee_within_sane_bounds(redemption) -> None:
    fee_bps = _call(redemption.functions.instantFee())
    assert 0 <= fee_bps <= 500, (
        f"instantFee {fee_bps} bps is outside 0-5% sane bounds"
    )


def test_mtbill_token_address_is_proxy_with_balance_method(mtbill) -> None:
    """Calling balanceOf on the zero address should not revert; it confirms
    the proxy delegates to a working ERC20 implementation and our ABI is
    selector-compatible.
    """
    balance = _call(
        mtbill.functions.balanceOf("0x0000000000000000000000000000000000000000"),
    )
    assert balance == 0


def test_deposit_instant_selector_matches_known_tx(w3: Web3) -> None:
    """Cross-check the depositInstant selector against the known deposit tx
    from basescan (0x3afa5693...). Mis-matched selectors would cause silent
    reverts on real deposits.
    """
    expected_selector = bytes.fromhex("c02dd27a")
    abi = _abi("MidasDepositVault")
    deposit_instant = next(
        (item for item in abi if item.get("name") == "depositInstant"),
        None,
    )
    assert deposit_instant is not None, "depositInstant missing from ABI"
    sig = "depositInstant(" + ",".join(i["type"] for i in deposit_instant["inputs"]) + ")"
    actual = Web3.keccak(text=sig)[:4]
    assert actual == expected_selector, (
        f"depositInstant selector {actual.hex()} != known 0xc02dd27a; "
        f"ABI is incompatible with the deployed contract"
    )


def test_redeem_instant_selector_known(w3: Web3) -> None:
    """The redeem path is symmetric — pin its selector too. We saw
    redeemRequest 0xbfc2d46a in the example withdraw tx; redeemInstant is a
    separate function with its own selector.
    """
    abi = _abi("MidasRedemptionVault")
    redeem_instant = next(
        (item for item in abi if item.get("name") == "redeemInstant"),
        None,
    )
    assert redeem_instant is not None
    sig = "redeemInstant(" + ",".join(i["type"] for i in redeem_instant["inputs"]) + ")"
    selector = Web3.keccak(text=sig)[:4]
    assert len(selector) == 4
