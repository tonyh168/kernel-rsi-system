"""Deterministic correctness and performance verdicts; no model judgment."""

from __future__ import annotations

import math
import platform
import statistics
import sys
from hashlib import sha256

from .schemas import (
    Environment,
    Evaluation,
    Measurement,
    RunConfig,
    WorkloadEvaluation,
)


def environment_fingerprint(config: RunConfig) -> Environment:
    return Environment(
        backend=config.mode,
        python=sys.version.split()[0],
        platform=platform.platform(),
        fixture_protocol=config.evaluation.protocol_version,
    )


def fingerprint_text(environment: Environment) -> str:
    import json
    return sha256(json.dumps(environment.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


def paired_noise(measurements: list[Measurement]) -> float:
    if len(measurements) < 2:
        return 0.0
    # Use median absolute paired log-speedup deviation as a robust noise estimate.
    ratios = [math.log(item.speedup) for item in measurements]
    center = statistics.median(ratios)
    mad = statistics.median(abs(value - center) for value in ratios)
    return math.expm1(1.4826 * mad)


def evaluate(
    config: RunConfig,
    *,
    evaluation_id: str,
    node_id: str,
    baseline_node_id: str,
    environment: Environment,
    correctness_passed: bool,
    correctness_cases: int,
    correctness_failures: list[str],
    measurements: dict[str, list[Measurement]],
) -> Evaluation:
    """Convert trusted runner measurements to an auditable ACCEPT/REJECT verdict."""
    reasons: list[str] = []
    workloads: list[WorkloadEvaluation] = []
    if not correctness_passed:
        reasons.append("Correctness failed; performance is not considered")
        return Evaluation(
            evaluation_id=evaluation_id, node_id=node_id, baseline_node_id=baseline_node_id,
            protocol_version=config.evaluation.protocol_version, environment=environment,
            correctness="FAIL", correctness_cases=correctness_cases,
            correctness_failures=correctness_failures, verdict="REJECT", reasons=reasons,
        )
    for workload in config.workloads:
        samples = measurements.get(workload.workload_id, [])
        if len(samples) < config.evaluation.repeats:
            reasons.append(f"Missing repeated measurements: {workload.workload_id}")
            continue
        candidate = statistics.median(item.candidate_ms for item in samples)
        baseline = statistics.median(item.baseline_ms for item in samples)
        noise = paired_noise(samples)
        speedup = baseline / candidate
        workloads.append(WorkloadEvaluation(
            workload_id=workload.workload_id, role=workload.role, measurements=samples,
            candidate_median_ms=candidate, baseline_median_ms=baseline,
            speedup=speedup, noise=noise,
        ))
        threshold = max(config.evaluation.min_speedup,
                        math.exp(noise * config.evaluation.noise_multiplier))
        if workload.role == "target" and speedup < threshold:
            reasons.append(
                f"{workload.workload_id}: speedup {speedup:.4f} below noise-aware threshold {threshold:.4f}"
            )
        if workload.role == "guard" and speedup < 1 - config.evaluation.max_guard_regression:
            reasons.append(f"{workload.workload_id}: guard regression exceeds limit")
    if not workloads:
        reasons.append("No valid performance result")
    return Evaluation(
        evaluation_id=evaluation_id, node_id=node_id, baseline_node_id=baseline_node_id,
        protocol_version=config.evaluation.protocol_version, environment=environment,
        correctness="PASS", correctness_cases=correctness_cases,
        workloads=workloads, verdict="REJECT" if reasons else "ACCEPT", reasons=reasons,
    )
