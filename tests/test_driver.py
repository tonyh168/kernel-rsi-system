from __future__ import annotations

import re
from pathlib import Path

import pytest

from flaggems_rsi.driver import ExperimentDriver
from flaggems_rsi.schemas import NodeStatus


@pytest.mark.asyncio
async def test_full_offline_iteration_and_resume(config, tmp_path):
    config.evaluation.min_speedup = 1.0
    run = tmp_path / "run"
    driver = ExperimentDriver(config, run)
    driver.initialize()
    nodes = await driver.run(max_iterations=1)
    assert nodes[0].status == NodeStatus.ACCEPTED
    assert driver.map.require_champion(config.operator) == nodes[0].node_id
    assert (run / "iterations" / "000000" / "candidate.patch").exists()
    assert "WORK_FACTOR = 1" in (run / "workspaces" / nodes[0].node_id / "kernel.py").read_text()
    resumed = ExperimentDriver(config, run)
    later = await resumed.run(max_iterations=1)
    assert later[0].node_id != nodes[0].node_id
    assert (run / "iterations" / "000001").exists()


@pytest.mark.asyncio
async def test_wrong_fast_code_is_rejected(config, tmp_path, plan):
    class BrokenAgent:
        config = None
        async def plan(self, context):
            return plan
        async def implement(self, plan, workspace):
            target = workspace / "kernel.py"
            source = target.read_text().replace("WORK_FACTOR = 4", "WORK_FACTOR = 1")
            target.write_text(source.replace("value * 2", "value * 3"))
        async def close(self):
            pass

    run = tmp_path / "run"
    driver = ExperimentDriver(config, run, agent=BrokenAgent())
    driver.initialize()
    nodes = await driver.run(max_iterations=1)
    assert nodes[0].status == NodeStatus.REJECTED
    assert driver.map.require_champion(config.operator) == "sim_copy_0000"
    assert not (run / "iterations" / "000000" / "benchmark.json").exists()


@pytest.mark.asyncio
async def test_explicit_fixture_ablation(config, tmp_path):
    config.evaluation.min_speedup = 1.0
    run = tmp_path / "run"
    driver = ExperimentDriver(config, run)
    driver.initialize()
    candidate = (await driver.run(max_iterations=1))[0]
    ablations = await driver.run_ablation(candidate.node_id)
    assert len(ablations) == 1
    assert ablations[0].ablates_change_id == "reduce-work"
    assert ablations[0].status == NodeStatus.REJECTED
    assert any(edge.kind == "ABLATES" for edge in driver.store.edges())
    assert "WORK_FACTOR = 4" in (run / "workspaces" / ablations[0].node_id / "kernel.py").read_text()
