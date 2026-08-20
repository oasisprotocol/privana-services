import json
import time

import pytest

import src.core.fee_policy as fee_policy_module
from src.core.config import load_settings
from src.core.fee_policy import FeeDecision, parse_fee_policies, resolve_internal_fee

WALLET = "0x" + "ab" * 20
OTHER_WALLET = "0x" + "cd" * 20
NOW = int(time.time())


def _campaign(**overrides):
    entry = {
        "id": "founding-members-2026",
        "fee_bps": 0,
        "valid_from": NOW - 3600,
        "valid_until": NOW + 86400,
        "wallets": [WALLET],
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def policies(monkeypatch):
    def _set(entries):
        fee_policy_module._policies = parse_fee_policies(json.dumps(entries))

    yield _set
    fee_policy_module._policies = None


class TestParseFeePolicies:
    def test_empty_input_yields_no_policies(self):
        assert parse_fee_policies("") == ()
        assert parse_fee_policies("   ") == ()

    def test_parses_valid_campaign(self):
        policies = parse_fee_policies(json.dumps([_campaign()]))
        assert len(policies) == 1
        assert policies[0].fee_bps == 0
        assert WALLET.lower() in policies[0].wallets

    def test_wallets_are_lowercased(self):
        upper = "0x" + "AB" * 20
        policies = parse_fee_policies(json.dumps([_campaign(wallets=[upper])]))
        assert upper.lower() in policies[0].wallets

    @pytest.mark.parametrize(
        "bad",
        [
            "not json",
            json.dumps({"id": "x"}),
            json.dumps([_campaign(id="")]),
            json.dumps([_campaign(fee_bps=-1)]),
            json.dumps([_campaign(fee_bps=10001)]),
            json.dumps([_campaign(fee_bps="0")]),
            json.dumps([_campaign(valid_from="2026-01-01")]),
            json.dumps([_campaign(valid_from=NOW, valid_until=NOW)]),
            json.dumps([_campaign(venue_scope="lifi")]),
            json.dumps([_campaign(wallets=[])]),
            json.dumps([_campaign(wallets=["0x123"])]),
        ],
    )
    def test_rejects_malformed_config(self, bad):
        with pytest.raises(ValueError):
            parse_fee_policies(bad)

    def test_rejects_duplicate_policy_id(self):
        entries = [_campaign(), _campaign(wallets=[OTHER_WALLET])]
        with pytest.raises(ValueError, match="duplicate fee policy id"):
            parse_fee_policies(json.dumps(entries))

    def test_rejects_wallet_in_two_policies(self):
        entries = [
            _campaign(),
            _campaign(id="second", wallets=[WALLET.upper().replace("0X", "0x")]),
        ]
        with pytest.raises(ValueError, match="more than one fee policy"):
            parse_fee_policies(json.dumps(entries))


class TestResolveInternalFee:
    def test_exempt_wallet_within_window(self, policies):
        policies([_campaign()])
        decision = resolve_internal_fee(WALLET, NOW)
        assert decision == FeeDecision(
            fee_bps=0, policy_id="founding-members-2026", valid_until=NOW + 86400
        )

    def test_wallet_match_is_case_insensitive(self, policies):
        policies([_campaign()])
        decision = resolve_internal_fee("0x" + "AB" * 20, NOW)
        assert decision.fee_bps == 0

    def test_unlisted_wallet_pays_default(self, policies):
        policies([_campaign()])
        decision = resolve_internal_fee(OTHER_WALLET, NOW)
        assert decision.fee_bps == load_settings().fee_bps
        assert decision.policy_id is None

    def test_expired_policy_pays_default(self, policies):
        policies([_campaign(valid_from=NOW - 7200, valid_until=NOW - 3600)])
        decision = resolve_internal_fee(WALLET, NOW)
        assert decision.fee_bps == load_settings().fee_bps

    def test_window_is_half_open(self, policies):
        policies([_campaign(valid_from=NOW, valid_until=NOW + 100)])
        assert resolve_internal_fee(WALLET, NOW).fee_bps == 0
        assert resolve_internal_fee(WALLET, NOW + 100).fee_bps == load_settings().fee_bps

    def test_not_yet_valid_policy_pays_default(self, policies):
        policies([_campaign(valid_from=NOW + 3600, valid_until=NOW + 7200)])
        decision = resolve_internal_fee(WALLET, NOW)
        assert decision.fee_bps == load_settings().fee_bps
