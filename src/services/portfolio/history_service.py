"""Assembles the chart series the history endpoints serve (S6).

Everything below is composition: the accounting history replays into
available/locked buckets (S3), those are priced against stored price history
(S5), and the earn leg comes from the earn value series (S4). The only new
decisions here are which window to cover and how densely to sample it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from src.clients.accounting import JwtIdentity, get_accounting_client
from src.services.earn.value_history import read_user_earn_cashflows
from src.services.portfolio.reconstruction import replay_history
from src.services.portfolio.valuation import (
    EarnSeriesValues,
    PortfolioPoint,
    compose_portfolio,
    sample_grid,
    value_buckets,
)
from src.services.price_history import (
    DAY_SEC,
    SAMPLE_INTERVAL_SEC,
    price_series_for_tokens,
)

logger = logging.getLogger(__name__)

# The furthest back an "All" range reaches. Nothing older is worth plotting:
# the price backfill is a year deep and the service is younger than that, so a
# longer window would only add flat extrapolated points — and an entry with a
# bogus timestamp can't blow the grid up into millions of samples.
MAX_HISTORY_DAYS = 1825

# Ranges up to this length keep the sampler's own ~1/4-day resolution; longer
# ones step daily so a multi-year "All" stays a few thousand points rather
# than tens of thousands. DAY_SEC is a multiple of the sampler interval, so
# the coarser points still land on sampled slots.
FINE_GRAIN_DAYS = 90


@dataclass(frozen=True)
class EarnValueSample:
    timestamp: int
    value_e8: int


def _window(earliest_ts: int, days: Optional[int], now: int) -> list[int]:
    """The sampling grid for a request, oldest first.

    A fixed range is anchored to now so the chart shows the window the client
    asked for, even when the user's first event predates it — balances carry
    forward across the window boundary. An "All" range starts at the first
    event instead, clamped to MAX_HISTORY_DAYS.
    """
    floor_ts = now - MAX_HISTORY_DAYS * DAY_SEC
    start = now - days * DAY_SEC if days is not None else earliest_ts
    start = max(start, floor_ts)
    if start > now:
        return []
    step = DAY_SEC if now - start > FINE_GRAIN_DAYS * DAY_SEC else SAMPLE_INTERVAL_SEC
    return sample_grid(start, now, step)


async def _token_decimals(token_ids: list[str]) -> dict[str, int]:
    """Decimals per token; ones the accounting service will not describe are
    left out, so valuation skips them loudly instead of scaling them wrong."""
    client = get_accounting_client()
    infos = await asyncio.gather(
        *(client.get_token_info(token_id) for token_id in token_ids),
        return_exceptions=True,
    )
    decimals = {}
    for token_id, info in zip(token_ids, infos):
        if isinstance(info, BaseException):
            logger.warning("Token info read failed for %s: %s", token_id, info)
            continue
        if info.decimals is None:
            logger.warning("Token %s has no decimals on record", token_id)
            continue
        decimals[token_id] = info.decimals
    return decimals


async def portfolio_history(
    identity: JwtIdentity, days: Optional[int] = None
) -> list[PortfolioPoint]:
    """Total portfolio value over time: available + locked + earn, in fiat.

    Empty when the user has no accounting history and no earn positions —
    there is no chart to draw, which is an answer rather than an error.
    """
    now = int(time.time())
    entries = await get_accounting_client().get_user_history(identity.siwe_token)
    buckets = replay_history(entries, identity.address)
    earn_flows = read_user_earn_cashflows(identity.address)

    event_times = [points[0].timestamp for points in buckets.values() if points]
    event_times += [flow.timestamp for flow in earn_flows]
    if not event_times:
        return []

    grid = _window(min(event_times), days, now)
    if not grid:
        return []

    token_ids = sorted(buckets)
    bucket_values = value_buckets(
        buckets,
        price_series_for_tokens(token_ids),
        await _token_decimals(token_ids),
        grid,
    )
    earn = await EarnSeriesValues.load(identity.address, grid)
    return compose_portfolio(bucket_values, earn)


async def earn_history(
    user_address: str, days: Optional[int] = None
) -> list[EarnValueSample]:
    """Earn position value over time, in fiat.

    Reads the same aggregate the portfolio total uses, so the earn chart and
    the earn slice of the portfolio chart can never disagree.
    """
    now = int(time.time())
    flows = read_user_earn_cashflows(user_address)
    if not flows:
        return []

    grid = _window(flows[0].timestamp, days, now)
    if not grid:
        return []

    earn = await EarnSeriesValues.load(user_address, grid)
    return [
        EarnValueSample(timestamp=ts, value_e8=earn.earn_value_e8(ts)) for ts in grid
    ]
