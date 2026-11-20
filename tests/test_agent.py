from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from flaggems_rsi.agent import ClaudeResearchAgent, load_prompt, sdk_common
from flaggems_rsi.map import IterationMap
from flaggems_rsi.runners.simulated import SimulatedRunner
from flaggems_rsi.storage import RunStore, read_jsonl


def test_agent_options_have_least_privilege(config, tmp_path):
    options = sdk_common(config, tmp_path, tmp_path)
    assert options["permission_mode"] == "dontAsk"
    assert options["setting_sources"] == []
    assert options["strict_mcp_config"] is True
    assert options["max_budget_usd"] > 0
    assert options["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"
    assert "Kernel" in load_prompt("developer")


def test_planner_structured_output_and_session_capture(config, tmp_path, monkeypatch, plan):
    run = tmp_path / "run"
    run.mkdir()
    store = RunStore(run)
    lineage = IterationMap(store)
    lineage.initialize(config)
    runner = SimulatedRunner(config, run)
    result = SimpleNamespace(
        is_error=False, subtype="success", structured_output=plan.model_dump(mode="json"),
        session_id="planner-session", total_cost_usd=0.01, num_turns=2, errors=[], result=None,
    )

    class FakeClient:
        def __init__(self, options):
            self.options = options
        async def connect(self):
            pass
        async def query(self, prompt):
            assert "private_holdout" not in prompt
        async def receive_response(self):
            yield result
        async def disconnect(self):
            pass

    async def fake_receive(client):
        return result

    monkeypatch.setattr("flaggems_rsi.agent.ClaudeSDKClient", FakeClient)
    monkeypatch.setattr("flaggems_rsi.agent.receive_result", fake_receive)
    agent = ClaudeResearchAgent(config, run, lineage, runner)

    async def exercise():
        returned = await agent.plan(lineage.summary(config.operator))
        assert returned.hypothesis == plan.hypothesis
        assert agent._resume == "planner-session"
        await agent.close()

    import asyncio
    asyncio.run(exercise())
    sessions = read_jsonl(run / "sessions.jsonl")
    assert sessions[0]["session_id"] == "planner-session"
    assert agent._planner_options().resume == "planner-session"
    assert agent._planner_options().output_format["type"] == "json_schema"
