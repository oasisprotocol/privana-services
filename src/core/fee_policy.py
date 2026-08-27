"""Per-wallet fee policies for internal swaps.

Campaigns come from the ``FEE_POLICIES_JSON`` env var: a JSON array of

    {"id": "founding-members-2026", "fee_bps": 0,
     "valid_from": 1787184000, "valid_until": 1794960000,
     "wallets": ["0x..."]}

Policies only ever apply to internally filled swaps; LiFi routed swaps always
pay the global fee. Validation is strict and fails startup on bad config, a
malformed campaign list must never silently fall back to charging everyone
the default fee or, worse, nobody.
"""

import json
import logging
from dataclasses import dataclass
from typing import Optional

from src.core.config import load_settings
from src.core.validation import validate_address

logger = logging.getLogger(__name__)

_VENUE_SCOPE_INTERNAL = "internal"


@dataclass(frozen=True)
class FeePolicy:
    id: str
    fee_bps: int
    valid_from: int
    valid_until: int
    wallets: frozenset[str]


@dataclass(frozen=True)
class FeeDecision:
    fee_bps: int
    policy_id: Optional[str] = None
    valid_until: Optional[int] = None


def parse_fee_policies(raw: str) -> tuple[FeePolicy, ...]:
    if not raw or not raw.strip():
        return ()
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FEE_POLICIES_JSON is not valid JSON: {exc}") from exc
    if not isinstance(entries, list):
        raise ValueError("FEE_POLICIES_JSON must be a JSON array")

    policies = []
    seen_ids: set[str] = set()
    seen_wallets: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"FEE_POLICIES_JSON[{i}] must be an object")
        policy_id = entry.get("id")
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise ValueError(f"FEE_POLICIES_JSON[{i}].id must be a non-empty string")
        if policy_id in seen_ids:
            raise ValueError(f"duplicate fee policy id {policy_id!r}")
        seen_ids.add(policy_id)

        fee_bps = entry.get("fee_bps")
        if not isinstance(fee_bps, int) or isinstance(fee_bps, bool) or not 0 <= fee_bps <= 10_000:
            raise ValueError(f"fee policy {policy_id!r}: fee_bps must be an int in 0..10000")

        valid_from = entry.get("valid_from")
        valid_until = entry.get("valid_until")
        for name, value in (("valid_from", valid_from), ("valid_until", valid_until)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"fee policy {policy_id!r}: {name} must be a unix timestamp")
        if valid_from >= valid_until:
            raise ValueError(f"fee policy {policy_id!r}: valid_from must be before valid_until")

        venue_scope = entry.get("venue_scope", _VENUE_SCOPE_INTERNAL)
        if venue_scope != _VENUE_SCOPE_INTERNAL:
            raise ValueError(f"fee policy {policy_id!r}: only venue_scope 'internal' is supported")

        wallets_raw = entry.get("wallets")
        if not isinstance(wallets_raw, list) or not wallets_raw:
            raise ValueError(f"fee policy {policy_id!r}: wallets must be a non-empty array")
        wallets = set()
        for wallet in wallets_raw:
            if not isinstance(wallet, str):
                raise ValueError(f"fee policy {policy_id!r}: wallets must be address strings")
            validate_address(wallet, f"fee policy {policy_id!r} wallet")
            lowered = wallet.lower()
            if lowered in seen_wallets:
                raise ValueError(f"wallet {lowered} appears in more than one fee policy")
            seen_wallets.add(lowered)
            wallets.add(lowered)

        policies.append(
            FeePolicy(
                id=policy_id,
                fee_bps=fee_bps,
                valid_from=valid_from,
                valid_until=valid_until,
                wallets=frozenset(wallets),
            )
        )
    return tuple(policies)


_policies: Optional[tuple[FeePolicy, ...]] = None


def get_fee_policies(refresh: bool = False) -> tuple[FeePolicy, ...]:
    global _policies
    if _policies is None or refresh:
        _policies = parse_fee_policies(load_settings().fee_policies_json)
        if _policies:
            logger.info(
                "Loaded %d fee policies covering %d wallets",
                len(_policies),
                sum(len(p.wallets) for p in _policies),
            )
    return _policies


def resolve_internal_fee(user_address: str, now: int) -> FeeDecision:
    """Fee for an internally filled swap. Windows are half-open: [from, until)."""
    wallet = user_address.lower()
    for policy in get_fee_policies():
        if wallet in policy.wallets and policy.valid_from <= now < policy.valid_until:
            return FeeDecision(
                fee_bps=policy.fee_bps,
                policy_id=policy.id,
                valid_until=policy.valid_until,
            )
    return FeeDecision(fee_bps=load_settings().fee_bps)
