"""Claude Agent SDK integration with least-privilege tool scopes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    query,
)

from .map import IterationMap
from .schemas import IterationPlan, RunConfig
from .storage import append_jsonl, read_jsonl
from .tools import create_research_server


def prompt_path(name: str) -> Path:
    candidates = [
        Path(__file__).resolve().parents[3] / "prompts" / f"{name}.md",
        Path.cwd() / "prompts" / f"{name}.md",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(f"Prompt not found: {name}")


def load_prompt(name: str) -> str:
    return prompt_path(name).read_text(encoding="utf-8")


def sdk_common(config: RunConfig, workspace: Path, run_root: Path) -> dict[str, Any]:
    async def audit(tool_input: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        append_jsonl(run_root / "agent-tools.jsonl", [{
            "tool_use_id": tool_use_id, "input": tool_input,
        }])
        return {}

    return {
        "model": config.agent.model,
        "cwd": workspace,
        "setting_sources": [],
        "strict_mcp_config": True,
        "permission_mode": "dontAsk",
        "max_turns": config.agent.max_turns,
        "max_budget_usd": config.agent.max_budget_usd,
        "env": {
            "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",
            "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "3",
        },
        "hooks": {"PreToolUse": [HookMatcher(hooks=[audit], on_failure="continue")]},
    }


def researcher_definitions(config: RunConfig, iteration_map: IterationMap, runner: Any) -> dict[str, AgentDefinition]:
    tools = ["Read", "Grep", "Glob"]
    return {
        "code-researcher": AgentDefinition(
            description="Read the specified operator implementation and public tests; propose a falsifiable hypothesis.",
            prompt=load_prompt("researcher"), tools=tools, model="inherit",
            maxTurns=5, background=False,
        ),
        "profiler": AgentDefinition(
            description="Use only the controlled simulated profiling tool and read-only inspection; never edit code.",
            prompt=load_prompt("profiler"),
            tools=[*tools, "mcp__kernel_research__get_simulated_profile"], model="inherit",
            maxTurns=5, background=False,
        ),
    }


async def receive_result(client: ClaudeSDKClient) -> ResultMessage:
    result = None
    async for message in client.receive_response():
        if isinstance(message, ResultMessage):
            result = message
    if result is None:
        raise RuntimeError("Agent session produced no ResultMessage")
    if result.is_error or result.subtype != "success":
        raise RuntimeError(f"Agent failed: {result.subtype}; {result.errors or result.result}")
    return result


class ClaudeResearchAgent:
    """One main session for planning; a fresh scoped session for implementation."""

    def __init__(self, config: RunConfig, run_root: Path, iteration_map: IterationMap, runner: Any):
        self.config = config
        self.run_root = run_root.resolve()
        self.iteration_map = iteration_map
        self.runner = runner
        self._planner: ClaudeSDKClient | None = None
        self._resume: str | None = next(
            (item.get("session_id") for item in reversed(read_jsonl(self.run_root / "sessions.jsonl"))
             if item.get("role") == "planner" and item.get("session_id")), None,
        )

    def _planner_options(self) -> ClaudeAgentOptions:
        workspace = self.config.repository.resolve()
        server = create_research_server(self.config, self.iteration_map, self.runner)
        return ClaudeAgentOptions(
            **sdk_common(self.config, workspace, self.run_root),
            tools=["Read", "Grep", "Glob", "Agent"],
            allowed_tools=[
                "Read", "Grep", "Glob", "Agent",
                "mcp__kernel_research__get_iteration_map",
                "mcp__kernel_research__get_simulated_profile",
            ],
            disallowed_tools=["Edit", "Write", "Bash", "NotebookEdit"],
            system_prompt=load_prompt("planner"),
            agents=researcher_definitions(self.config, self.iteration_map, self.runner),
            mcp_servers={"kernel_research": server},
            output_format={"type": "json_schema", "schema": IterationPlan.model_json_schema()},
            resume=self._resume,
        )

    async def plan(self, context: dict[str, object]) -> IterationPlan:
        if self._planner is None:
            self._planner = ClaudeSDKClient(options=self._planner_options())
            await self._planner.connect()
        prompt = (
            "基于以下实验地图提出下一轮单候选计划。先调用 code-researcher 和 profiler，并等待它们的报告。"
            "只使用公开信息。目标算子和允许文件如下：\n"
            + json.dumps({**context, "allowed_files": self.config.allowed_files,
                          "public_workloads": [item.model_dump(mode="json") for item in self.config.workloads]},
                         ensure_ascii=False)
        )
        await self._planner.query(prompt)
        result = await receive_result(self._planner)
        self._resume = result.session_id
        if result.structured_output is None:
            raise ValueError("Planner returned no structured plan")
        plan = IterationPlan.model_validate(result.structured_output)
        append_jsonl(self.run_root / "sessions.jsonl", [{
            "role": "planner", "session_id": result.session_id,
            "total_cost_usd": result.total_cost_usd, "num_turns": result.num_turns,
        }])
        return plan

    async def implement(self, plan: IterationPlan, workspace: Path) -> None:
        prompt = (
            "请实现以下已审批计划。只修改该 worktree 内的白名单源码，不修改任何测试、配置或 Driver。\n"
            f"候选 worktree: {workspace}\n"
            f"允许文件: {json.dumps(self.config.allowed_files)}\n"
            f"计划: {plan.model_dump_json()}\n"
        )
        options = ClaudeAgentOptions(
            **sdk_common(self.config, workspace, self.run_root),
            tools=["Read", "Edit", "Write", "Grep", "Glob"],
            allowed_tools=["Read", "Edit", "Write", "Grep", "Glob"],
            disallowed_tools=["Bash", "Agent", "NotebookEdit"],
            system_prompt=load_prompt("developer"),
        )
        result = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                result = message
        if result is None or result.is_error or result.subtype != "success":
            raise RuntimeError(f"Developer failed: {result.errors if result else 'no result'}")
        append_jsonl(self.run_root / "sessions.jsonl", [{
            "role": "developer", "session_id": result.session_id,
            "total_cost_usd": result.total_cost_usd, "num_turns": result.num_turns,
        }])

    async def close(self) -> None:
        if self._planner is not None:
            await self._planner.disconnect()
            self._planner = None
