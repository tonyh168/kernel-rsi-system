"""Single-candidate experiment driver with a persistent lineage graph."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from .evaluator import environment_fingerprint, evaluate
from .knowledge import planning_context
from .map import IterationMap
from .runners.simulated import SimulatedRunner
from .schemas import (
    Evaluation,
    FailureKind,
    IterationNode,
    IterationPlan,
    JobSpec,
    JobStatus,
    Measurement,
    NodeStatus,
    RunConfig,
)
from .storage import RunStore, atomic_write, json_text, read_model
from .workspace import WorkspaceManager, resolve_revision


class Agent(Protocol):
    async def plan(self, context: dict[str, object]) -> IterationPlan: ...
    async def implement(self, plan: IterationPlan, workspace: Path) -> None: ...
    async def close(self) -> None: ...


class SimulationAgent:
    """Deterministic offline agent for exercising the full driver without API calls."""

    def __init__(self, config: RunConfig, store: RunStore | None = None):
        self.config = config
        self._store = store

    async def plan(self, context: dict[str, object]) -> IterationPlan:
        from .schemas import AblationKind, ChangeKind, SemanticChange
        champion = self._store.load_node(str(context["champion_node"])) if self._store else None
        current_factor = 4
        if champion and champion.patch_path:
            patch = (self._store.artifacts / champion.patch_path).read_text(encoding="utf-8")
            match = re.search(r"^\+WORK_FACTOR = (\d+)$", patch, re.MULTILINE)
            if match:
                current_factor = int(match.group(1))
        target_factor = 4 if current_factor == 1 else 1
        hypothesis = (
            "Restore redundant work to exercise a historical branch"
            if target_factor == 4 else "Reduce the fixture's redundant repeated work"
        )
        return IterationPlan(
            target_operator=self.config.operator,
            parent_node_id=str(context["champion_node"]),
            hypothesis=hypothesis,
            expected_bottleneck="Repeated CPU work in the fixture",
            implementation_steps=[f"Set WORK_FACTOR to {target_factor}"],
            target_workloads=[item.workload_id for item in self.config.workloads],
            validation_requirements=["Run the frozen public and private correctness harness"],
            semantic_changes=[SemanticChange(
                change_id="reduce-work", kind=ChangeKind.ALGORITHM,
                description="Remove redundant fixture iterations",
                files=self.config.allowed_files,
                ablation=AblationKind.INDEPENDENT,
                operation="set_work_factor", value=target_factor,
            )],
            stop_condition=f"Stop when WORK_FACTOR is already {target_factor}",
        )

    async def implement(self, plan: IterationPlan, workspace: Path) -> None:
        target = workspace / self.config.allowed_files[0]
        source = target.read_text(encoding="utf-8")
        desired = plan.semantic_changes[0].value
        source, count = re.subn(r"^WORK_FACTOR = \d+$", f"WORK_FACTOR = {desired}", source, count=1, flags=re.MULTILINE)
        if not count:
            raise ValueError("Fixture WORK_FACTOR declaration not found")
        target.write_text(source, encoding="utf-8")

    async def close(self) -> None:
        return None


class ExperimentDriver:
    def __init__(self, config: RunConfig, run_root: Path, agent: Agent | None = None):
        self.config = config
        self.run_root = run_root.resolve()
        self.store = RunStore(self.run_root)
        self.map = IterationMap(self.store)
        self.workspace = WorkspaceManager(config, self.run_root)
        self.runner = SimulatedRunner(config, self.run_root)
        self.agent = agent or SimulationAgent(config, self.store)
        if getattr(self.agent, "config", None) is None:
            self.agent.config = config
        self.config_path = self.run_root / "config.json"
        self.environment = environment_fingerprint(config)
        self.baseline_workspace = self.run_root / "workspaces" / "baseline"

    @property
    def iteration_dir(self) -> Path:
        return self.run_root / "iterations"

    def _write(self, path: Path, value: object) -> None:
        atomic_write(path, json_text(value))

    def initialize(self) -> IterationNode:
        self.config.repository = self.config.repository.resolve()
        self.config.base_revision = resolve_revision(self.config.repository, self.config.base_revision)
        self.store.initialize()
        if self.store.nodes(self.config.operator):
            raise ValueError("Run already initialized; use resume")
        self._write(self.config_path, self.config)
        baseline = self.map.initialize(self.config)
        self.workspace.create(baseline, self.baseline_workspace)
        return baseline

    def load(self) -> None:
        self.config = read_model(self.config_path, RunConfig)
        self.workspace = WorkspaceManager(self.config, self.run_root)
        self.runner = SimulatedRunner(self.config, self.run_root)
        self.environment = environment_fingerprint(self.config)
        if not self.baseline_workspace.exists():
            raise ValueError("Baseline workspace missing; run cannot be safely resumed")

    async def run(self, max_iterations: int | None = None) -> list[IterationNode]:
        if not self.store.nodes(self.config.operator):
            self.initialize()
        else:
            self.load()
        self.config.agent.auto_promote = self.agent.config.agent.auto_promote
        if hasattr(self.agent, "config"):
            self.agent.config = self.config
        self.map.assert_acyclic()
        self.map.invalidate_stale(self.environment, self.config.evaluation.protocol_version, self.config.operator)
        if self.baseline_workspace.exists():
            self.workspace.dispose(self.baseline_workspace)
        champion = self.store.load_node(self.map.require_champion(self.config.operator))
        self.workspace.create(champion, self.baseline_workspace)
        count = max_iterations if max_iterations is not None else self.config.agent.max_iterations
        completed = []
        start_index = len(list(self.iteration_dir.glob("[0-9][0-9][0-9][0-9][0-9][0-9]")))
        try:
            for index in range(start_index, start_index + count):
                node = await self.iterate(index)
                if node is not None:
                    completed.append(node)
        finally:
            await self.runner.wait()
            await self.agent.close()
        return completed

    async def iterate(self, index: int) -> IterationNode | None:
        iteration = self.iteration_dir / f"{index:06d}"
        iteration.mkdir(parents=True, exist_ok=False)
        context = planning_context(self.map, self.config.operator)
        self._write(iteration / "context.json", context)
        self._write(self.run_root / "state.json", {"phase": "PLAN", "iteration": index})
        plan = await self.agent.plan(context)
        if plan.target_operator != self.config.operator:
            raise ValueError("Plan targets an unexpected operator")
        self._write(iteration / "plan.json", plan)
        parent = self.store.load_node(plan.parent_node_id or self.map.require_champion(self.config.operator))
        if parent.hypothesis == plan.hypothesis and parent.generation > 0:
            self._write(iteration / "verdict.json", {"verdict": "STOP", "reason": "Hypothesis already implemented"})
            return None
        node = self.map.create_candidate(self.config, plan)
        self._write(iteration / "node.json", node)
        node = self.map.transition(node, NodeStatus.IMPLEMENTING)
        candidate_workspace = self.run_root / "workspaces" / node.node_id
        self.workspace.create(node, candidate_workspace)
        try:
            await self.agent.implement(plan, candidate_workspace)
            patch = self.workspace.capture(candidate_workspace)
            from .map import patch_identity
            patch_id = patch_identity(node.base_revision, patch)
            node = self.map.capture_patch(node, patch, candidate_revision=self.workspace.candidate_revision(
                candidate_workspace, patch_id,
            ))
            self._write(iteration / "candidate.patch", patch.decode("utf-8"))
            self._write(iteration / "node.json", node)
            return await self.evaluate_candidate(node, parent, candidate_workspace, iteration)
        except Exception as error:
            node = self.map.transition(node, NodeStatus.INVALID, failure_kind=FailureKind.INFRA_ERROR, reason=str(error))
            self._write(iteration / "error.json", {"type": type(error).__name__, "error": str(error)})
            return node

    async def run_ablation(self, node_id: str) -> list[IterationNode]:
        from .ablation import ablation_plan_to_iteration, plan_ablations
        if not self.store.nodes(self.config.operator):
            raise ValueError("Initialize the run before ablation")
        self.load()
        completed = []
        for plan in plan_ablations(self.config, self.map, node_id):
            iteration = self.iteration_dir / f"ablation-{node_id}-{plan.change_id}"
            iteration.mkdir(parents=True, exist_ok=False)
            self._write(iteration / "ablation-plan.json", plan)
            source = self.store.load_node(node_id)
            parent = self.store.load_node(plan.parent_node_id)
            change = next(item for item in source.semantic_changes if item.change_id == plan.change_id)
            iteration_plan = ablation_plan_to_iteration(self.config, plan, change)
            # The ablated implementation is a child of the complete candidate.
            # Its performance baseline is that complete candidate, not the
            # optimization's original parent. This measures the change's effect.
            node = self.map.create_candidate(
                self.config, iteration_plan, ablates_change_id=plan.change_id,
                parent_ids=[source.node_id, parent.node_id],
            )
            node = self.map.transition(node, NodeStatus.IMPLEMENTING)
            candidate_workspace = self.run_root / "workspaces" / node.node_id
            self.workspace.create(source, candidate_workspace)
            # Reverse only the declared, validated change patch inside the
            # source worktree. This is the single-change fixture adapter.
            self.workspace.reverse(candidate_workspace,
                                   (self.store.artifacts / source.patch_path).read_bytes())
            patch = self.workspace.capture(candidate_workspace)
            from .map import patch_identity
            patch_id = patch_identity(node.base_revision, patch)
            node = self.map.capture_patch(node, patch, candidate_revision=self.workspace.candidate_revision(
                candidate_workspace, patch_id,
            ), allow_empty=True)
            self._write(iteration / "candidate.patch", patch.decode("utf-8"))
            source_workspace = self.run_root / "workspaces" / source.node_id
            completed.append(await self.evaluate_candidate(node, source, candidate_workspace, iteration,
                                                          baseline_workspace=source_workspace))
        await self.runner.wait()
        await self.agent.close()
        return completed

    async def _job(self, kind: str, node: IterationNode, workspace: Path,
                   baseline_workspace: Path | None = None) -> dict:
        job_id = f"{node.node_id}-{kind}"
        job = JobSpec(
            job_id=job_id, kind=kind, run_id=self.config.run_id, node_id=node.node_id,
            workspace=workspace, baseline_workspace=baseline_workspace or self.baseline_workspace,
            workload_ids=[item.workload_id for item in self.config.workloads],
            timeout_seconds=self.config.evaluation.timeout_seconds,
        )
        await self.runner.submit(job)
        result = await self.runner.collect(job_id)
        if result.status != JobStatus.SUCCEEDED:
            raise RuntimeError(f"{kind} job {result.status.value}: {result.error}")
        return result.summary

    async def evaluate_candidate(
        self, node: IterationNode, baseline: IterationNode, workspace: Path, iteration: Path,
        baseline_workspace: Path | None = None,
    ) -> IterationNode:
        self._write(self.run_root / "state.json", {"phase": "CORRECTNESS", "node": node.node_id})
        node = self.map.transition(node, NodeStatus.EVALUATING)
        correctness = await self._job("correctness", node, workspace)
        self._write(iteration / "correctness.json", correctness)
        if not correctness["passed"]:
            failure = Evaluation(
                evaluation_id=f"eval-{node.node_id}", node_id=node.node_id,
                baseline_node_id=baseline.node_id, protocol_version=self.config.evaluation.protocol_version,
                environment=self.environment, correctness="FAIL", correctness_cases=correctness["cases"],
                correctness_failures=correctness["failures"], verdict="REJECT",
                reasons=["Correctness failed; benchmark was not run"],
            )
            self.store.add_evaluation(failure)
            node = self.map.transition(node, NodeStatus.REJECTED, failure_kind=FailureKind.IMPLEMENTATION_FAILED,
                                       reason="Correctness failed", evaluation_id=failure.evaluation_id)
            self._write(iteration / "verdict.json", failure)
            return node
        self._write(self.run_root / "state.json", {"phase": "BENCHMARK", "node": node.node_id})
        benchmark = await self._job("benchmark", node, workspace, baseline_workspace)
        self._write(iteration / "benchmark.json", benchmark)
        measurements = {
            workload_id: [Measurement.model_validate(sample) for sample in samples]
            for workload_id, samples in benchmark["measurements"].items()
        }
        verdict = evaluate(
            self.config, evaluation_id=f"eval-{node.node_id}", node_id=node.node_id,
            baseline_node_id=baseline.node_id, environment=self.environment,
            correctness_passed=True, correctness_cases=correctness["cases"], correctness_failures=[],
            measurements=measurements,
        )
        self.store.add_evaluation(verdict)
        self._write(iteration / "verdict.json", verdict)
        if verdict.verdict == "ACCEPT":
            if node.ablates_change_id:
                node = self.map.transition(node, NodeStatus.PARKED, evaluation_id=verdict.evaluation_id,
                                           reason="Ablation completed; explanatory branch only")
            elif self.config.agent.auto_promote:
                node = self.map.promote(node, verdict.evaluation_id)
            else:
                node = self.map.transition(node, NodeStatus.PARKED, evaluation_id=verdict.evaluation_id,
                                           reason="Accepted by evaluator; awaiting manual promotion")
        else:
            node = self.map.transition(node, NodeStatus.REJECTED, failure_kind=FailureKind.INSIGNIFICANT,
                                       reason="; ".join(verdict.reasons), evaluation_id=verdict.evaluation_id)
        self._write(self.run_root / "state.json", {"phase": "VERDICT", "node": node.node_id,
                                                    "verdict": verdict.verdict})
        # Explanatory ablation nodes never promote a new optimization Champion.
        return node
