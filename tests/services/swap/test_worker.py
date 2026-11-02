import asyncio
from dataclasses import replace

import src.services.swap.worker as worker_module
from src.services.swap.worker import SwapWorker

# The hyperevm RPC is configured but not reachable yet.
CHAINS = {"base_rpc_url": 8453, "ethereum_rpc_url": 1, "hyperevm_rpc_url": None}


async def test_every_stage_runs_rounds_until_stopped(monkeypatch, settings):
    rounds = {}

    def stage(name):
        async def run_round(*chain):
            key = (name, *chain)
            rounds[key] = rounds.get(key, 0) + 1
            if key == ("execute", 8453):
                raise RuntimeError("rpc down")
        return run_round

    monkeypatch.setattr(worker_module, "STAGES", tuple(
        (name, stage(name), 0.01) for name, _, _ in worker_module.STAGES))
    monkeypatch.setattr(worker_module, "CHAIN_STAGES", tuple(
        (name, stage(name), 0.01) for name, _, _ in worker_module.CHAIN_STAGES))
    monkeypatch.setattr(worker_module, "rpc_chain_id", CHAINS.get)
    monkeypatch.setattr(worker_module, "load_settings", lambda: replace(
        settings, base_rpc_url="http://base", ethereum_rpc_url="http://eth", hyperevm_rpc_url="http://hyperevm"))
    expected = {(name,) for name, _, _ in worker_module.STAGES} | {
        (name, chain) for name, _, _ in worker_module.CHAIN_STAGES for chain in (8453, 1)}
    worker = SwapWorker()

    await worker.start()
    # Polled, not slept: a loaded box can give a stage one round in 50 ms.
    for _ in range(500):
        if set(rounds) == expected and rounds[("execute", 8453)] > 1 and rounds[("execute", 1)] > 1:
            break
        await asyncio.sleep(0.01)
    await worker.stop()
    stopped = dict(rounds)
    await asyncio.sleep(0.03)

    # A failing round does not stop its stage or another chain's, and a chain
    # whose RPC does not answer gets no rounds.
    assert set(rounds) == expected
    assert rounds[("execute", 8453)] > 1 and rounds[("execute", 1)] > 1
    assert rounds == stopped


async def test_two_rpcs_for_one_chain_run_its_stages_once(monkeypatch, settings):
    runners = {}

    def stage(name):
        async def run_round(chain_id):
            runners.setdefault((name, chain_id), set()).add(asyncio.current_task().get_name())
        return run_round

    monkeypatch.setattr(worker_module, "STAGES", ())
    monkeypatch.setattr(worker_module, "CHAIN_STAGES", tuple(
        (name, stage(name), 0.01) for name, _, _ in worker_module.CHAIN_STAGES))
    # Both RPCs answer for Base.
    monkeypatch.setattr(worker_module, "rpc_chain_id", lambda setting: 8453)
    monkeypatch.setattr(worker_module, "load_settings", lambda: replace(
        settings, base_rpc_url="http://base", ethereum_rpc_url="http://base-too", hyperevm_rpc_url=""))
    worker = SwapWorker()

    await worker.start()
    for _ in range(500):
        if set(runners) == {(name, 8453) for name, _, _ in worker_module.CHAIN_STAGES}:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    await worker.stop()

    assert set(runners) == {(name, 8453) for name, _, _ in worker_module.CHAIN_STAGES}
    assert all(len(tasks) == 1 for tasks in runners.values())
