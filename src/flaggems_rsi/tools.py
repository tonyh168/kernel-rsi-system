"""Read-only, domain-specific MCP tools exposed to the agent session."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool

from .map import IterationMap
from .runners.local import LocalRunner
from .schemas import JobSpec, JobStatus, RunConfig


def text_result(value: Any, *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}],
            "is_error": error}


def create_research_server(config: RunConfig, iteration_map: IterationMap, runner: LocalRunner):
    @tool(
        "get_iteration_map",
        "Read the compressed public lineage map for this run. It contains no holdout cases.",
        {},
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def get_iteration_map(args: dict[str, Any]) -> dict[str, Any]:
        return text_result(iteration_map.summary(config.operator))

    @tool(
        "get_simulated_profile",
        "Submit a CPU-fixture profile for a node workspace; this is not GPU profiling.",
        {"node_id": str},
        annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=False),
    )
    async def get_simulated_profile(args: dict[str, Any]) -> dict[str, Any]:
        if config.mode != "simulated":
            return text_result({"error": "Only the simulated fixture has a built-in profile tool"}, error=True)
        try:
            node = iteration_map.store.load_node(args["node_id"])
            workspace = runner.run_root / node.workspace if node.workspace else runner.run_root / "workspaces" / node.node_id
            if not workspace.exists():
                return text_result({"error": "Node workspace does not exist"}, error=True)
            job = JobSpec(
                job_id=f"profile-{node.node_id}", kind="profile", run_id=config.run_id,
                node_id=node.node_id, workspace=workspace, workload_ids=[], timeout_seconds=30,
            )
            await runner.submit(job)
            result = await runner.collect(job.job_id)
            if result.status != JobStatus.SUCCEEDED:
                return text_result({"error": result.error}, error=True)
            return text_result(result.summary)
        except (ValueError, KeyError) as error:
            return text_result({"error": str(error)}, error=True)

    return create_sdk_mcp_server(name="kernel_research", tools=[get_iteration_map, get_simulated_profile])
