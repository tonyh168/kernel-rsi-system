"""Executable CPU fixture for testing the optimization state machine.

This is deliberately NOT a GPU benchmark. Its only purpose is to verify that a
candidate patch, not an agent's claim, drives correctness and performance results.
"""

from __future__ import annotations

import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path


def load_kernel(workspace: Path):
    spec = importlib.util.spec_from_file_location("candidate_kernel", workspace / "kernel.py")
    if spec is None or spec.loader is None:
        raise ImportError("Cannot load candidate kernel")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(workspace: Path, *, holdout: bool) -> dict:
    module = load_kernel(workspace)
    sizes = [0, 1, 7, 64, 127] + ([3, 33, 129] if holdout else [])
    failures = []
    for size in sizes:
        values = list(range(size))
        try:
            actual = module.run_kernel(values)
            expected = [value * 2 for value in values]
            if actual != expected:
                failures.append(f"size={size}: wrong output")
        except Exception as error:
            failures.append(f"size={size}: {type(error).__name__}")
    return {"passed": not failures, "cases": len(sizes), "failures": failures}


def measure_once(workspace: Path, baseline: Path, shape: list[int], warmup: int) -> dict:
    candidate = load_kernel(workspace)
    reference = load_kernel(baseline)
    values = list(range(shape[0]))
    for _ in range(warmup):
        candidate.run_kernel(values)
        reference.run_kernel(values)
    timings = {}
    for label, module in (("baseline", reference), ("candidate", candidate)):
        start = time.perf_counter_ns()
        for _ in range(20):
            module.run_kernel(values)
        timings[label] = max((time.perf_counter_ns() - start) / 20 / 1_000_000, 1e-9)
    return {"candidate_ms": timings["candidate"], "baseline_ms": timings["baseline"]}


def main() -> None:
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    kind = request["kind"]
    workspace = Path(request["workspace"])
    baseline = Path(request["baseline"])
    if kind == "correctness":
        result = check(workspace, holdout=False)
        private = check(workspace, holdout=True)
        result["passed"] = result["passed"] and private["passed"]
        result["cases"] += private["cases"]
        result["failures"] += private["failures"]
    elif kind == "benchmark":
        measurements = {workload["workload_id"]: [] for workload in request["workloads"]}
        for _ in range(request["repeats"]):
            for workload in request["workloads"]:
                measurements[workload["workload_id"]].append(
                    measure_once(workspace, baseline, workload["shapes"][0], request["warmup"])
                )
        result = {"measurements": measurements}
    elif kind == "profile":
        module = load_kernel(workspace)
        result = {"work_factor": module.WORK_FACTOR, "note": "CPU fixture profile, not hardware profiling"}
    else:
        raise ValueError(f"Unknown fixture job: {kind}")
    Path(request["result_path"]).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()
