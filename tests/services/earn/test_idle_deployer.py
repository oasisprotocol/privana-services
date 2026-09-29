from unittest.mock import AsyncMock, MagicMock

import pytest

POOL_A = "0x" + "aa" * 32
POOL_B = "0x" + "bb" * 32


def _deployer(pools, deploy=None):
    from src.services.earn.idle_deployer import IdleDeployer

    service = MagicMock()
    service.list_pools = MagicMock(return_value=pools)
    service.deploy_reclaim = deploy or AsyncMock(return_value=0)
    return IdleDeployer(service=service), service


def _pool(pool_id, active=True):
    return {"pool_id": pool_id, "active": active}


@pytest.mark.asyncio
async def test_deploys_every_active_pool():
    deployer, service = _deployer(
        [_pool(POOL_A), _pool(POOL_B)], AsyncMock(side_effect=[100, 250]),
    )

    assert await deployer.deploy_once() == 350

    assert [c.args[0] for c in service.deploy_reclaim.await_args_list] == [POOL_A, POOL_B]


@pytest.mark.asyncio
async def test_paused_pools_only_reclaim():
    deployer, service = _deployer([_pool(POOL_A, active=False), _pool(POOL_B)])

    await deployer.deploy_once()

    # A paused pool gets nothing new, but its waiting exits are still paid for.
    assert [(c.args[0], c.kwargs["allow_deploy"]) for c in service.deploy_reclaim.await_args_list] == [
        (POOL_A, False), (POOL_B, True),
    ]


@pytest.mark.asyncio
async def test_one_pool_failing_does_not_stop_the_others():
    deployer, service = _deployer(
        [_pool(POOL_A), _pool(POOL_B)],
        AsyncMock(side_effect=[RuntimeError("bridge down"), 250]),
    )

    assert await deployer.deploy_once() == 250


@pytest.mark.asyncio
async def test_a_failed_pool_listing_is_not_fatal():
    deployer, service = _deployer([])
    service.list_pools = MagicMock(side_effect=RuntimeError("rpc down"))

    assert await deployer.deploy_once() == 0


@pytest.mark.asyncio
async def test_rounds_run_on_the_configured_batch_interval(monkeypatch):
    import src.services.earn.idle_deployer as module

    deployer, _ = _deployer([])
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        deployer._running = False

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        module, "load_settings", lambda: MagicMock(earn_batch_interval_sec=42),
    )
    deployer._running = True

    await deployer._run()

    assert slept == [42]
