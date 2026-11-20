"""Workload-level theoretical lower bounds, with explicit modeling assumptions."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PerformanceModel(BaseModel):
    """A simple roofline is valid only when its assumptions fit the workload.

    Vendor peak specifications are not treated as measured effective throughput.
    Asynchronous overlap, launch overhead and synchronization must be accounted
    for separately rather than hidden in a claimed 100% efficiency.
    """

    model_config = ConfigDict(extra="forbid")

    workload_id: str
    bytes_transferred: int = Field(gt=0)
    flops: int = Field(ge=0)
    effective_bandwidth_bytes_per_second: float = Field(gt=0)
    effective_flops_per_second: float = Field(default=0, ge=0)
    launch_overhead_seconds: float = Field(default=0, ge=0)
    synchronization_seconds: float = Field(default=0, ge=0)
    overlap_assumed: bool = True
    assumptions: list[str] = Field(default_factory=list)

    @property
    def memory_seconds(self) -> float:
        return self.bytes_transferred / self.effective_bandwidth_bytes_per_second

    @property
    def compute_seconds(self) -> float:
        if self.flops == 0:
            return 0.0
        if self.effective_flops_per_second == 0:
            raise ValueError("A compute-bound workload requires effective FLOP/s")
        return self.flops / self.effective_flops_per_second

    @property
    def roofline_seconds(self) -> float:
        if self.overlap_assumed:
            return max(self.memory_seconds, self.compute_seconds)
        return self.memory_seconds + self.compute_seconds

    @property
    def lower_bound_seconds(self) -> float:
        return self.roofline_seconds + self.launch_overhead_seconds + self.synchronization_seconds

    def efficiency(self, measured_seconds: float) -> float:
        if measured_seconds <= 0:
            raise ValueError("Measured time must be positive")
        return self.lower_bound_seconds / measured_seconds

    def summary(self, measured_seconds: float) -> dict[str, float | str | bool]:
        return {
            "workload_id": self.workload_id,
            "memory_seconds": self.memory_seconds,
            "compute_seconds": self.compute_seconds,
            "roofline_seconds": self.roofline_seconds,
            "lower_bound_seconds": self.lower_bound_seconds,
            "efficiency": self.efficiency(measured_seconds),
            "overlap_assumed": self.overlap_assumed,
        }
