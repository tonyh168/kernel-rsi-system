"""Ablation planning from explicit semantic changes, never guessed reverse diffs."""

from __future__ import annotations

from .evaluator import fingerprint_text
from .map import IterationMap
from .schemas import AblationKind, AblationPlan, IterationPlan, RunConfig, SemanticChange


def plan_ablations(config: RunConfig, iteration_map: IterationMap, node_id: str) -> list[AblationPlan]:
    node = iteration_map.store.load_node(node_id)
    if not node.parent_ids:
        raise ValueError("Cannot ablate a baseline without a candidate")
    if not node.semantic_changes:
        raise ValueError("Candidate has no declared semantic changes")
    evaluations = iteration_map.store.evaluations(node_id)
    if not evaluations:
        raise ValueError("Candidate must be evaluated before ablation")
    evaluation = evaluations[-1]
    parent = iteration_map.store.load_node(node.parent_ids[0])
    if evaluation.baseline_node_id != parent.node_id:
        raise ValueError("Ablation requires a common parent baseline evaluation")
    plans = []
    for change in node.semantic_changes:
        if config.mode != "simulated":
            raise ValueError("The built-in ablation adapter only supports simulated fixtures")
        if change.operation != "set_work_factor" or change.value is None:
            continue
        if change.ablation != AblationKind.INDEPENDENT or change.depends_on:
            continue
        plans.append(AblationPlan(
            node_id=node.node_id, parent_node_id=parent.node_id, change_id=change.change_id,
            protocol_version=evaluation.protocol_version,
            environment_fingerprint=fingerprint_text(evaluation.environment),
            workloads=node.target_workloads, construction="fixture_operation",
        ))
    if not plans:
        raise ValueError("No independently isolable changes; split the patch or supply an explicit adapter")
    return plans


def ablation_plan_to_iteration(config: RunConfig, plan: AblationPlan, source_change: SemanticChange) -> IterationPlan:
    return IterationPlan(
        target_operator=config.operator, parent_node_id=plan.node_id,
        hypothesis=f"Ablation: remove {plan.change_id} from {plan.node_id}",
        expected_bottleneck="Isolate the declared semantic change's contribution",
        implementation_steps=[f"Apply trusted fixture ablation adapter for {plan.change_id}"],
        target_workloads=plan.workloads,
        validation_requirements=["Same correctness, environment, protocol and interleaved measurements"],
        semantic_changes=[source_change],
        risk="This is an explanatory branch, not a proposed optimization",
    )
