from __future__ import annotations

import pytest

from flaggems_rsi.evaluator import evaluate, environment_fingerprint
from flaggems_rsi.schemas import Measurement


def test_correctness_failure_rejects_even_fast_candidate(config):
    verdict = evaluate(
        config, evaluation_id="eval-test", node_id="sim_copy_0001", baseline_node_id="sim_copy_0000",
        environment=environment_fingerprint(config), correctness_passed=False,
        correctness_cases=10, correctness_failures=["wrong result"],
        measurements={"small": [Measurement(candidate_ms=0.01, baseline_ms=100)] * 3},
    )
    assert verdict.verdict == "REJECT"
    assert not verdict.workloads
    assert "performance is not considered" in verdict.reasons[0]


def test_insignificant_and_guard_regressions_reject(config):
    samples = {
        "small": [Measurement(candidate_ms=1.0, baseline_ms=1.0)] * 3,
        "guard": [Measurement(candidate_ms=1.0, baseline_ms=1.0)] * 3,
    }
    result = evaluate(config, evaluation_id="eval-test", node_id="sim_copy_0001",
                      baseline_node_id="sim_copy_0000", environment=environment_fingerprint(config),
                      correctness_passed=True, correctness_cases=10, correctness_failures=[], measurements=samples)
    assert result.verdict == "REJECT"
    assert "noise-aware threshold" in result.reasons[0]
    samples["small"] = [Measurement(candidate_ms=0.1, baseline_ms=1.0)] * 3
    samples["guard"] = [Measurement(candidate_ms=1.0, baseline_ms=0.5)] * 3
    result = evaluate(config, evaluation_id="eval-test", node_id="sim_copy_0001",
                      baseline_node_id="sim_copy_0000", environment=environment_fingerprint(config),
                      correctness_passed=True, correctness_cases=10, correctness_failures=[], measurements=samples)
    assert result.verdict == "REJECT"
    assert any("guard regression" in reason for reason in result.reasons)


def test_stable_improvement_is_accepted(config):
    config.evaluation.min_speedup = 1.01
    samples = {name: [Measurement(candidate_ms=0.1, baseline_ms=1.0)] * 3 for name in ("small", "guard")}
    result = evaluate(config, evaluation_id="eval-test", node_id="sim_copy_0001",
                      baseline_node_id="sim_copy_0000", environment=environment_fingerprint(config),
                      correctness_passed=True, correctness_cases=10, correctness_failures=[], measurements=samples)
    assert result.verdict == "ACCEPT"
