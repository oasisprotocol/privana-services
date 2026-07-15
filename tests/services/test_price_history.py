from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.clients.coingecko import PricePoint
from src.services.price_history import (
    BACKFILL_DELAY_SEC,
    PriceSampler,
    parse_coingecko_token_ids,
    read_points,
    store_points,
)

USDC_SWAP = "0x330ba47d00c7ce3018deee017b319fd7cc6473a2ddc9e6eba6ebb4207be15279"
USDC_EARN = "0xc719650e9f4b0f27d956638c54518932ef9d15e720a1a2b2850250bcd0816514"
WETH = "0x335b5cccd1e63b2fe79863a0db73fce430e4e66902e2b78424f8662621e29fb7"

MAPPING = f'{{"{USDC_SWAP}":"usd-coin","{USDC_EARN}":"usd-coin","{WETH}":"ethereum"}}'

JUN15 = 1781481600  # 2026-06-15 00:00:00 UTC
JUN16 = JUN15 + 86400


def _settings(raw: str):
    settings = MagicMock()
    settings.coingecko_token_ids = raw
    return settings


class TestParseMapping:
    def test_parses_token_to_coin_id(self):
        assert parse_coingecko_token_ids(MAPPING)[WETH] == "ethereum"

    def test_empty_config_disables_history(self):
        assert parse_coingecko_token_ids("") == {}

    def test_bad_json_disables_history_without_raising(self):
        # The chart is decoration; balances are correct regardless.
        assert parse_coingecko_token_ids("{not json") == {}

    def test_non_object_disables_history(self):
        assert parse_coingecko_token_ids('["usd-coin"]') == {}


class TestStoreAndRead:
    def test_roundtrips_oldest_first(self):
        store_points("ethereum", [PricePoint(JUN16, 200), PricePoint(JUN15, 100)])

        assert [(p.timestamp, p.price_e8) for p in read_points("ethereum")] == [
            (JUN15, 100),
            (JUN16, 200),
        ]

    def test_rewriting_a_day_is_a_no_op(self):
        # A sampler restart or an overlapping backfill must not double-write, and
        # must not clobber a price already recorded for that day.
        store_points("ethereum", [PricePoint(JUN15, 100)])
        store_points("ethereum", [PricePoint(JUN15, 999)])

        assert [p.price_e8 for p in read_points("ethereum")] == [100]

    def test_coins_are_stored_separately(self):
        store_points("ethereum", [PricePoint(JUN15, 100)])
        store_points("usd-coin", [PricePoint(JUN15, 99973600)])

        assert [p.price_e8 for p in read_points("usd-coin")] == [99973600]

    def test_intraday_points_collapse_onto_the_day(self):
        # Whatever the caller hands us, one day is one row: the write path is the
        # only thing standing between a stray timestamp and a duplicated day.
        store_points("ethereum", [PricePoint(JUN15, 100), PricePoint(JUN15 + 3661, 200)])

        assert [(p.timestamp, p.price_e8) for p in read_points("ethereum")] == [(JUN15, 100)]


class TestSampler:
    @pytest.fixture
    def gecko(self):
        client = MagicMock()
        client.get_spot_prices = AsyncMock(return_value={"usd-coin": 99973600})
        client.get_price_history = AsyncMock(return_value=[PricePoint(JUN15, 100)])
        return client

    @pytest.fixture
    def no_sleep(self):
        # Backfill spaces its requests; without this every backfill test would
        # pay that delay for real.
        with patch("src.services.price_history.asyncio.sleep", AsyncMock()) as sleep:
            yield sleep

    async def test_sample_once_records_configured_coins(self, gecko):
        with patch("src.services.price_history.load_settings", return_value=_settings(MAPPING)):
            stored = await PriceSampler(client=gecko).sample_once()

        assert stored == 1
        # Three token ids collapse to two coins, so we ask CoinGecko for two.
        assert sorted(gecko.get_spot_prices.await_args.args[0]) == ["ethereum", "usd-coin"]

    async def test_sample_once_survives_coingecko_failure(self, gecko):
        gecko.get_spot_prices.side_effect = RuntimeError("coingecko down")

        with patch("src.services.price_history.load_settings", return_value=_settings(MAPPING)):
            assert await PriceSampler(client=gecko).sample_once() == 0

    async def test_sample_once_does_nothing_without_config(self, gecko):
        with patch("src.services.price_history.load_settings", return_value=_settings("")):
            assert await PriceSampler(client=gecko).sample_once() == 0
        gecko.get_spot_prices.assert_not_called()

    async def test_backfill_stores_each_coin(self, gecko, no_sleep):
        with patch("src.services.price_history.load_settings", return_value=_settings(MAPPING)):
            stored = await PriceSampler(client=gecko).backfill()

        assert stored == 2  # one point each for ethereum and usd-coin
        assert gecko.get_price_history.await_count == 2

    async def test_backfill_spaces_requests_between_coins(self, gecko, no_sleep):
        # The free tier refuses back-to-back bursts, and the coin list grows with
        # the token list. Two coins means one gap, not two: no delay before the
        # first request or after the last.
        with patch("src.services.price_history.load_settings", return_value=_settings(MAPPING)):
            await PriceSampler(client=gecko).backfill()

        assert no_sleep.await_args_list == [call(BACKFILL_DELAY_SEC)]

    async def test_backfill_continues_past_a_failing_coin(self, gecko, no_sleep):
        gecko.get_price_history.side_effect = [RuntimeError("nope"), [PricePoint(JUN15, 100)]]

        with patch("src.services.price_history.load_settings", return_value=_settings(MAPPING)):
            assert await PriceSampler(client=gecko).backfill() == 1

    async def test_restarting_does_not_accumulate_rows_for_today(self, gecko, no_sleep):
        # CoinGecko's daily series ends with a point at the current second, not at
        # midnight. That timestamp differs on every boot, so OR IGNORE alone let each
        # restart append another row for the day on top of the day's real point.
        with patch("src.services.price_history.load_settings", return_value=_settings(MAPPING)):
            for boot_second in (28800, 45000, 71100):
                gecko.get_price_history = AsyncMock(
                    return_value=[PricePoint(JUN15, 100), PricePoint(JUN15 + boot_second, 200)]
                )
                await PriceSampler(client=gecko).backfill()

        assert [(p.timestamp, p.price_e8) for p in read_points("ethereum")] == [(JUN15, 100)]

    async def test_start_is_a_no_op_without_config(self, gecko):
        sampler = PriceSampler(client=gecko)
        with patch("src.services.price_history.load_settings", return_value=_settings("")):
            await sampler.start()

        assert sampler._task is None

    async def test_samples_land_on_the_day(self, gecko):
        with patch("src.services.price_history.load_settings", return_value=_settings(MAPPING)):
            with patch("src.services.price_history.time.time", return_value=1781485337):
                await PriceSampler(client=gecko).sample_once()

        # 1781485337 is 2026-06-15 01:02:17 UTC; the point is recorded at 00:00:00.
        assert [p.timestamp for p in read_points("usd-coin")] == [1781481600]

    async def test_resampling_within_the_day_does_not_duplicate(self, gecko):
        # We poll several times a day but keep one point per day, so every tick after
        # the day's first is a no-op instead of another near-identical row.
        sampler = PriceSampler(client=gecko)
        with patch("src.services.price_history.load_settings", return_value=_settings(MAPPING)):
            with patch("src.services.price_history.time.time", return_value=1781485337):
                await sampler.sample_once()
            with patch("src.services.price_history.time.time", return_value=1781488937):
                stored = await sampler.sample_once()

        assert stored == 0
        assert len(read_points("usd-coin")) == 1

    async def test_loop_survives_a_failing_round(self, gecko):
        # A round that raises must not kill the task: the service would go on
        # serving traffic while silently recording nothing for the rest of its life.
        sampler = PriceSampler(client=gecko)
        calls = []

        async def failing_round():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("sqlite is having a moment")
            sampler._running = False
            return 0

        with patch.object(sampler, "backfill", AsyncMock(return_value=0)):
            with patch.object(sampler, "sample_once", side_effect=failing_round):
                with patch("src.services.price_history.asyncio.sleep", AsyncMock()):
                    sampler._running = True
                    await sampler._run()

        assert len(calls) == 2  # survived the first failure and ran again

    async def test_loop_survives_a_failing_backfill(self, gecko):
        sampler = PriceSampler(client=gecko)

        async def stop_after_one():
            sampler._running = False
            return 0

        with patch.object(sampler, "backfill", AsyncMock(side_effect=RuntimeError("no backfill"))):
            with patch.object(sampler, "sample_once", side_effect=stop_after_one) as sample:
                with patch("src.services.price_history.asyncio.sleep", AsyncMock()):
                    sampler._running = True
                    await sampler._run()

        sample.assert_awaited()  # a dead backfill must not stop us sampling forward
