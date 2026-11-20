from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from flaggems_rsi.schemas import (
    AblationKind,
    AgentConfig,
    ChangeKind,
    EvaluationConfig,
    IterationPlan,
    RunConfig,
    SemanticChange,
    Workload,
)


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    repository = tmp_path / "fixture"
    repository.mkdir()
    (repository / "kernel.py").write_text(
        "WORK_FACTOR = 4\n\n\ndef run_kernel(values):\n"
        "    result = []\n"
        "    for _ in range(WORK_FACTOR):\n"
        "        result = [value * 2 for value in values]\n"
        "    return result\n", encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "kernel.py"], check=True)
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@localhost", "commit", "-qm", "baseline"], check=True)
    return repository


@pytest.fixture
def config(fixture_repo: Path) -> RunConfig:
    return RunConfig(
        run_id="test-sim", mode="simulated", operator="sim_copy", repository=fixture_repo,
        allowed_files=["kernel.py"],
        workloads=[Workload(workload_id="small", shapes=[[32]]),
                   Workload(workload_id="guard", shapes=[[128]], role="guard")],
        agent=AgentConfig(auto_promote=True),
        evaluation=EvaluationConfig(repeats=3, min_speedup=1.05),
    )


@pytest.fixture
def plan() -> IterationPlan:
    return make_plan()


def make_plan(parent: str = "sim_copy_0000", *, value: int = 1) -> IterationPlan:
    return IterationPlan(
        target_operator="sim_copy", parent_node_id=parent,
        hypothesis=f"Set the work factor to {value}", expected_bottleneck="repeated work",
        implementation_steps=["Modify WORK_FACTOR"], target_workloads=["small", "guard"],
        semantic_changes=[SemanticChange(
            change_id="work", kind=ChangeKind.ALGORITHM, description="Work factor",
            files=["kernel.py"], ablation=AblationKind.INDEPENDENT,
            operation="set_work_factor", value=value,
        )],
    )
