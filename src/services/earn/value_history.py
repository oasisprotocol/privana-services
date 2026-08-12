"""Per-user earn value history (S4).

Reconstructs the value of a user's earn positions over time from their
``earn_transactions`` cashflows and the pool growth factors served by the
EarnRateProvider seam (S2). Implements the valuation identity

    value_pool(t) = sum over events e with t_e <= t of
                    signed_amount_e * growth_factor(pool, t_e, t)

with +amount for deposits and -amount for withdrawals, all in token base
units. No share bookkeeping is needed: a withdrawal of A assets at t_w burns
A / rate(t_w) shares, so its forward-grown negative cashflow removes exactly
the value those shares would have carried.

This series powers the /earn chart directly and is handed to the portfolio
valuation (S5) for the total-balance curve, so both charts value a position
exactly one way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional, Sequence

from src.clients.coingecko import PricePoint
from src.core.config import load_settings
from src.core.db import get_db
from src.services.earn.rate_provider import EarnRateResolver, get_earn_rate_resolver
from src.services.price_history import parse_coingecko_token_ids, read_points

logger = logging.getLogger(__name__)

PRICE_E8 = Decimal(10**8)

ZERO = Decimal(0)

# Rows that represent funds actually moved. A deposit counts once
# EarnManager.deposit landed and shares were minted — including 'undeployed',
# where strategy routing failed afterwards but the position exists (funds sit
# in pool balance pending an operator redeploy). A withdraw counts only when
# the on-chain withdraw completed. pending/failed rows never moved funds.
_DEPOSIT_STATUSES = ("completed", "undeployed")
_WITHDRAW_STATUSES = ("completed",)


@dataclass(frozen=True)
class EarnCashflow:
    """One settled earn cashflow: +amount deposit, -amount withdraw (base units)."""

    timestamp: int
    pool_id: str
    token_id: str
    signed_amount: Decimal


@dataclass(frozen=True)
class EarnValuePoint:
    """Earn value held in ``token_id`` at ``timestamp``, across all pools.

    ``earn_value_base`` is in token base units; ``earn_value_fiat`` is None
    when the token has no configured CoinGecko mapping, no stored price
    series, or its decimals could not be resolved.
    """

    timestamp: int
    token_id: str
    earn_value_base: int
    earn_value_fiat: Optional[Decimal]


def read_user_earn_cashflows(user_address: str) -> list[EarnCashflow]:
    """The user's settled earn cashflows, oldest first.

    The event time is ``updated_at``: the moment the row reached its settled
    status, i.e. when funds actually moved (``created_at`` is the signing
    time). For 'undeployed' deposits ``updated_at`` is the deploy-failure
    stamp, seconds after the mint — close enough for a chart, and the only
    stamp the table has.
    """
    deposit_marks = ", ".join("?" * len(_DEPOSIT_STATUSES))
    withdraw_marks = ", ".join("?" * len(_WITHDRAW_STATUSES))
    rows = get_db().execute(
        f"""
        SELECT operation, pool_id, token_id, amount, updated_at
        FROM earn_transactions
        WHERE user_address = ?
          AND (
            (operation = 'deposit' AND status IN ({deposit_marks}))
            OR (operation = 'withdraw' AND status IN ({withdraw_marks}))
          )
        ORDER BY updated_at ASC
        """,
        (user_address.lower(), *_DEPOSIT_STATUSES, *_WITHDRAW_STATUSES),
    ).fetchall()

    flows = []
    for row in rows:
        sign = 1 if row["operation"] == "deposit" else -1
        flows.append(
            EarnCashflow(
                timestamp=row["updated_at"],
                pool_id=row["pool_id"],
                token_id=row["token_id"].lower(),
                signed_amount=sign * Decimal(row["amount"]),
            )
        )
    return flows


async def earn_value_series(
    user_address: str,
    timestamps: Sequence[int],
    resolver: Optional[EarnRateResolver] = None,
) -> list[EarnValuePoint]:
    """Earn value at each requested timestamp, aggregated per token (the S4 seam).

    The caller picks the grid (daily buckets, event times, whatever the chart
    needs); timestamps are deduplicated and sorted. A user with no settled
    earn cashflows yields an empty list. Values are evaluated stepwise along
    the grid — growth factors compose multiplicatively within a provider, so
    this is the same identity at E + P rate reads per pool instead of E × P.
    """
    if not timestamps:
        return []
    resolver = resolver or get_earn_rate_resolver()
    grid = sorted({int(t) for t in timestamps})
    flows = read_user_earn_cashflows(user_address)
    if not flows:
        return []

    # A pool holds exactly one token, but the table doesn't enforce it, so key
    # on both — a stray mixed row degrades to a second position instead of
    # silently crediting the wrong token.
    by_position: dict[tuple[str, str], list[EarnCashflow]] = {}
    for flow in flows:
        by_position.setdefault((flow.pool_id, flow.token_id), []).append(flow)

    token_ids = sorted({flow.token_id for flow in flows})
    totals: dict[int, dict[str, Decimal]] = {t: dict.fromkeys(token_ids, ZERO) for t in grid}

    for (pool_id, token_id), pool_flows in by_position.items():
        value = ZERO
        index = 0
        prev_t: Optional[int] = None
        for t in grid:
            if prev_t is not None and value != ZERO:
                value *= await resolver.growth_factor(pool_id, prev_t, t)
            while index < len(pool_flows) and pool_flows[index].timestamp <= t:
                flow = pool_flows[index]
                growth = await resolver.growth_factor(pool_id, flow.timestamp, t)
                value += flow.signed_amount * growth
                index += 1
            # A full exit can leave a small negative residual when the modeled
            # rate (tier-b APY) differs from the rate the contract actually
            # paid. Report it as zero; keep the raw value for stepping so the
            # residual doesn't compound into later windows.
            totals[t][token_id] += max(value, ZERO)
            prev_t = t

    fiat = await _FiatContext.load(token_ids)
    return [
        EarnValuePoint(
            timestamp=t,
            token_id=token_id,
            earn_value_base=int(base.quantize(Decimal(1), rounding=ROUND_HALF_UP)),
            earn_value_fiat=fiat.convert(token_id, t, base),
        )
        for t in grid
        for token_id, base in totals[t].items()
    ]


class _FiatContext:
    """Per-series fiat conversion state: one price series and one decimals
    lookup per distinct token, resolved up front."""

    def __init__(
        self,
        prices: dict[str, list[PricePoint]],
        decimals: dict[str, Optional[int]],
    ) -> None:
        self._prices = prices
        self._decimals = decimals

    @classmethod
    async def load(cls, token_ids: Sequence[str]) -> "_FiatContext":
        mapping = parse_coingecko_token_ids(load_settings().coingecko_token_ids)
        prices: dict[str, list[PricePoint]] = {}
        decimals: dict[str, Optional[int]] = {}
        for token_id in token_ids:
            coin_id = mapping.get(token_id)
            prices[token_id] = read_points(coin_id) if coin_id else []
            decimals[token_id] = await _token_decimals(token_id)
        return cls(prices, decimals)

    def convert(self, token_id: str, ts: int, base_value: Decimal) -> Optional[Decimal]:
        price_e8 = _price_e8_at(self._prices[token_id], ts)
        token_decimals = self._decimals[token_id]
        if price_e8 is None or token_decimals is None:
            return None
        return base_value * price_e8 / PRICE_E8 / Decimal(10**token_decimals)


async def _token_decimals(token_id: str) -> Optional[int]:
    from src.clients.accounting import get_accounting_client

    try:
        info = await get_accounting_client().get_token_info(token_id)
    except Exception:
        logger.exception("earn value history: token info read failed for %s", token_id)
        return None
    return info.decimals


def _price_e8_at(points: list[PricePoint], ts: int) -> Optional[Decimal]:
    """Price in force at ``ts``: the last sample at or before it, extrapolated
    flat from the first point when ``ts`` predates the series (matching how
    the tier-b rate provider treats time before its history). None when the
    token has no stored series at all.

    ``points`` is oldest-first (read_points guarantees ORDER BY timestamp ASC).
    """
    if not points:
        return None
    chosen = points[0]
    for point in points:
        if point.timestamp > ts:
            break
        chosen = point
    return Decimal(chosen.price_e8)
