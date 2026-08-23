from unittest.mock import AsyncMock, MagicMock, patch

from src.services.pool_rate_history import (
    PoolRatePoint,
    PoolRateSampler,
    read_points,
    store_point,
)

POOL_A = "0xaaaa000000000000000000000000000000000000000000000000000000000001"
POOL_B = "0xbbbb000000000000000000000000000000000000000000000000000000000002"

JUN15 = 1781481600  # 2026-06-15 00:00:00 UTC
JUN16 = JUN15 + 86400
SAMPLE_INTERVAL = 86400 // 4


def _pool(pool_id, total_assets=100, total_shares=50, active=True):
    return {
        "pool_id": pool_id,
        "total_assets": total_assets,
        "total_shares": total_shares,
        "active": active,
    }


class TestStoreAndRead:
    def test_roundtrips_oldest_first(self):
        store_point(POOL_A, PoolRatePoint(JUN16, "200", "20"))
        store_point(POOL_A, PoolRatePoint(JUN15, "100", "10"))

        assert [(p.timestamp, p.total_assets, p.total_shares) for p in read_points(POOL_A)] == [
            (JUN15, "100", "10"),
            (JUN16, "200", "20"),
        ]

    def test_rewriting_a_bucket_is_a_no_op(self):
        # A sampler restart or an overlapping tick must not clobber a row already
        # recorded for that interval.
        store_point(POOL_A, PoolRatePoint(JUN15, "100", "10"))
        store_point(POOL_A, PoolRatePoint(JUN15, "999", "99"))

        assert [(p.total_assets, p.total_shares) for p in read_points(POOL_A)] == [("100", "10")]

    def test_pools_are_stored_separately(self):
        store_point(POOL_A, PoolRatePoint(JUN15, "100", "10"))
        store_point(POOL_B, PoolRatePoint(JUN15, "200", "20"))

        assert [p.total_assets for p in read_points(POOL_B)] == ["200"]

    def test_points_within_one_interval_collapse(self):
        store_point(POOL_A, PoolRatePoint(JUN15, "100", "10"))
        store_point(POOL_A, PoolRatePoint(JUN15 + 3661, "200", "20"))

        assert [(p.timestamp, p.total_assets) for p in read_points(POOL_A)] == [(JUN15, "100")]

    def test_samples_in_different_intervals_are_kept(self):
        for index, assets in enumerate(["100", "200", "300", "400"]):
            store_point(POOL_A, PoolRatePoint(JUN15 + index * SAMPLE_INTERVAL, assets, "10"))

        assert [p.total_assets for p in read_points(POOL_A)] == ["100", "200", "300", "400"]

    def test_uint256_scale_values_survive_as_text(self):
        big = str(2**200)
        store_point(POOL_A, PoolRatePoint(JUN15, big, big))

        assert read_points(POOL_A)[0].total_assets == big


class TestSampler:
    async def test_sample_once_records_active_pools(self):
        service = MagicMock()
        service.list_pools = MagicMock(return_value=[_pool(POOL_A)])
        service.effective_total_assets = AsyncMock(return_value=150)

        stored = await PoolRateSampler(service=service).sample_once()

        assert stored == 1
        point = read_points(POOL_A)[0]
        assert (point.total_assets, point.total_shares) == ("150", "50")

    async def test_samples_strategy_aum_not_stale_on_chain_total(self):
        # The whole point: record the strategy's live AUM (150), not the on-chain
        # total_assets (100) which only moves on sync.
        service = MagicMock()
        service.list_pools = MagicMock(return_value=[_pool(POOL_A, total_assets=100)])
        service.effective_total_assets = AsyncMock(return_value=150)

        await PoolRateSampler(service=service).sample_once()

        service.effective_total_assets.assert_awaited_once_with(POOL_A, 100)
        assert read_points(POOL_A)[0].total_assets == "150"

    async def test_skips_inactive_pools(self):
        service = MagicMock()
        service.list_pools = MagicMock(return_value=[_pool(POOL_A, active=False), _pool(POOL_B)])
        service.effective_total_assets = AsyncMock(return_value=150)

        stored = await PoolRateSampler(service=service).sample_once()

        assert stored == 1
        assert read_points(POOL_A) == []
        assert len(read_points(POOL_B)) == 1
        service.effective_total_assets.assert_awaited_once_with(POOL_B, 100)

    async def test_one_unreadable_pool_does_not_abort_the_others(self):
        service = MagicMock()
        service.list_pools = MagicMock(return_value=[_pool(POOL_A), _pool(POOL_B)])
        service.effective_total_assets = AsyncMock(side_effect=[RuntimeError("rpc down"), 150])

        stored = await PoolRateSampler(service=service).sample_once()

        assert stored == 1
        assert read_points(POOL_A) == []
        assert len(read_points(POOL_B)) == 1

    async def test_sample_once_survives_a_list_pools_failure(self):
        service = MagicMock()
        service.list_pools = MagicMock(side_effect=RuntimeError("chain down"))
        service.effective_total_assets = AsyncMock()

        assert await PoolRateSampler(service=service).sample_once() == 0
        service.effective_total_assets.assert_not_awaited()

    async def test_samples_snap_to_the_interval_boundary(self):
        service = MagicMock()
        service.list_pools = MagicMock(return_value=[_pool(POOL_A)])
        service.effective_total_assets = AsyncMock(return_value=150)

        # 1781485337 is 2026-06-15 01:02:17 UTC, inside the 00:00 interval.
        with patch("src.services.pool_rate_history.time.time", return_value=1781485337):
            await PoolRateSampler(service=service).sample_once()

        assert [p.timestamp for p in read_points(POOL_A)] == [1781481600]

    async def test_resampling_within_one_interval_does_not_duplicate(self):
        service = MagicMock()
        service.list_pools = MagicMock(return_value=[_pool(POOL_A)])
        service.effective_total_assets = AsyncMock(return_value=150)
        sampler = PoolRateSampler(service=service)

        with patch("src.services.pool_rate_history.time.time", return_value=1781485337):
            await sampler.sample_once()
        with patch("src.services.pool_rate_history.time.time", return_value=1781488937):
            stored = await sampler.sample_once()

        assert stored == 0
        assert len(read_points(POOL_A)) == 1

    async def test_loop_survives_a_failing_round(self):
        # A round that raises must not kill the task, or the service would go on
        # serving traffic while silently recording nothing forever.
        sampler = PoolRateSampler(service=MagicMock())
        calls = []

        async def failing_round():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("sqlite is having a moment")
            sampler._running = False
            return 0

        with patch.object(sampler, "sample_once", side_effect=failing_round):
            with patch("src.services.pool_rate_history.asyncio.sleep", AsyncMock()):
                sampler._running = True
                await sampler._run()

        assert len(calls) == 2  # survived the first failure and ran again


class TestReadPointBefore:
    def test_returns_newest_at_or_before_bound(self):
        from src.services.pool_rate_history import read_point_before

        store_point(POOL_A, PoolRatePoint(JUN15, "100", "10"))
        store_point(POOL_A, PoolRatePoint(JUN15 + SAMPLE_INTERVAL, "110", "10"))
        store_point(POOL_A, PoolRatePoint(JUN16, "120", "10"))

        point = read_point_before(POOL_A, ts_max=JUN16 - 1)
        assert point is not None
        assert point.timestamp == JUN15 + SAMPLE_INTERVAL
        assert point.total_assets == "110"

    def test_exact_bound_is_included(self):
        from src.services.pool_rate_history import read_point_before

        store_point(POOL_A, PoolRatePoint(JUN15, "100", "10"))
        point = read_point_before(POOL_A, ts_max=JUN15)
        assert point is not None
        assert point.timestamp == JUN15

    def test_lower_bound_excludes_stale_samples(self):
        from src.services.pool_rate_history import read_point_before

        store_point(POOL_A, PoolRatePoint(JUN15, "100", "10"))
        assert read_point_before(POOL_A, ts_max=JUN16, ts_min=JUN15 + 1) is None

    def test_no_samples_returns_none(self):
        from src.services.pool_rate_history import read_point_before

        assert read_point_before(POOL_A, ts_max=JUN16) is None

    def test_other_pool_not_matched(self):
        from src.services.pool_rate_history import read_point_before

        store_point(POOL_B, PoolRatePoint(JUN15, "100", "10"))
        assert read_point_before(POOL_A, ts_max=JUN16) is None
