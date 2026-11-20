"""Typed contracts shared by the driver, agents, evaluators, and storage."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
RelativePath = Annotated[str, StringConstraints(pattern=r"^[^/].*"), Field(min_length=1)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def safe_relative_paths(cls, value: Any, info: Any) -> Any:
        if info.field_name in {"files", "allowed_files"}:
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, str) and (Path(item).is_absolute() or ".." in Path(item).parts):
                    raise ValueError(f"Unsafe relative path: {item}")
        return value


class NodeStatus(StrEnum):
    PLANNED = "PLANNED"
    IMPLEMENTING = "IMPLEMENTING"
    EVALUATING = "EVALUATING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PARKED = "PARKED"
    INVALID = "INVALID"
    NEEDS_REVALIDATION = "NEEDS_REVALIDATION"


class BranchStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CHAMPION = "CHAMPION"
    MERGED = "MERGED"
    EXHAUSTED = "EXHAUSTED"
    ABANDONED = "ABANDONED"
    NEEDS_REVALIDATION = "NEEDS_REVALIDATION"


class FailureKind(StrEnum):
    IDEA_REJECTED = "IDEA_REJECTED"
    IMPLEMENTATION_FAILED = "IMPLEMENTATION_FAILED"
    INFRA_ERROR = "INFRA_ERROR"
    INSIGNIFICANT = "INSIGNIFICANT"


class EdgeKind(StrEnum):
    DERIVED_FROM = "DERIVED_FROM"
    EVALUATED_ON = "EVALUATED_ON"
    REJECTED_BY = "REJECTED_BY"
    COMBINES = "COMBINES"
    REIMPLEMENTS = "REIMPLEMENTS"
    SUPERSEDES = "SUPERSEDES"
    ABLATES = "ABLATES"


class ChangeKind(StrEnum):
    MEMORY_ACCESS = "memory_access"
    LAUNCH_CONFIG = "launch_config"
    PARALLELISM = "parallelism"
    ALGORITHM = "algorithm"
    SCHEDULING = "scheduling"
    OTHER = "other"


class AblationKind(StrEnum):
    INDEPENDENT = "INDEPENDENT"
    DEPENDENT = "DEPENDENT"
    INTERACTING = "INTERACTING"
    NOT_ISOLABLE = "NOT_ISOLABLE"


class SemanticChange(ContractModel):
    change_id: Name
    kind: ChangeKind
    description: str = Field(min_length=1)
    files: list[RelativePath] = Field(default_factory=list)
    expected_effect: str = ""
    ablation: AblationKind = AblationKind.NOT_ISOLABLE
    depends_on: list[Name] = Field(default_factory=list)
    # An optional, explicit operation understood by a fixture ablation adapter.
    # It is never a shell command and is not executed by the agent.
    operation: Literal["set_work_factor", "restore_parent_patch"] | None = None
    value: int | None = Field(default=None, ge=1)


class Workload(ContractModel):
    workload_id: Name
    shapes: list[list[int]] = Field(min_length=1)
    dtype: str = "float32"
    weight: float = Field(default=1.0, gt=0)
    role: Literal["target", "guard"] = "target"


class EvaluationConfig(ContractModel):
    protocol_version: str = "sim-v1"
    warmup: int = Field(default=1, ge=0)
    repeats: int = Field(default=5, ge=3)
    min_speedup: float = Field(default=1.05, gt=0)
    max_guard_regression: float = Field(default=0.03, ge=0)
    # A lower bound based on measured paired baseline/candidate variation.
    noise_multiplier: float = Field(default=1.0, ge=0)
    timeout_seconds: float = Field(default=30, gt=0)


class AgentConfig(ContractModel):
    model: str = "claude-opus-5"
    max_turns: int = Field(default=12, ge=1)
    max_budget_usd: float = Field(default=5.0, gt=0)
    max_iterations: int = Field(default=5, ge=1)
    auto_promote: bool = False


class RunConfig(ContractModel):
    schema_version: Literal[1] = 1
    run_id: Name
    mode: Literal["simulated", "local"] = "simulated"
    operator: Name
    repository: Path
    base_revision: str = "HEAD"
    allowed_files: list[RelativePath] = Field(min_length=1)
    workloads: list[Workload] = Field(min_length=1)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    # Local mode takes a fixed executable and arguments, never a shell string.
    command: list[str] | None = None
    private_holdout: Path | None = None

    @model_validator(mode="after")
    def validate_command(self) -> RunConfig:
        if self.mode == "local" and not self.command:
            raise ValueError("local mode requires a fixed command")
        if self.mode == "simulated" and self.command:
            raise ValueError("simulated mode uses its built-in fixture harness")
        return self


class IterationPlan(ContractModel):
    target_operator: Name
    parent_node_id: str | None = None
    hypothesis: str = Field(min_length=1)
    expected_bottleneck: str = ""
    implementation_steps: list[str] = Field(min_length=1)
    target_workloads: list[Name] = Field(min_length=1)
    validation_requirements: list[str] = Field(default_factory=list)
    semantic_changes: list[SemanticChange] = Field(min_length=1)
    risk: str = ""
    stop_condition: str = ""


class IterationNode(ContractModel):
    node_id: Name
    operator: Name
    parent_ids: list[str] = Field(default_factory=list)
    base_revision: str
    candidate_revision: str
    patch_id: str
    patch_path: str | None = None
    parent_patch_paths: list[str] = Field(default_factory=list)
    # Workspaces are disposable materializations, not the code identity itself.
    workspace: RelativePath | None = None
    branch: Name
    generation: int = Field(ge=0)
    hypothesis: str = ""
    semantic_changes: list[SemanticChange] = Field(default_factory=list)
    target_workloads: list[Name] = Field(default_factory=list)
    status: NodeStatus
    branch_status: BranchStatus = BranchStatus.ACTIVE
    evaluation_ids: list[str] = Field(default_factory=list)
    failure_kind: FailureKind | None = None
    failure_reason: str | None = None
    tags: list[str] = Field(default_factory=list)
    ablates_change_id: Name | None = None
    device_profile_id: Name | None = None
    roofline_id: Name | None = None
    profiler_artifact_ids: list[Name] = Field(default_factory=list)
    bottleneck_class: Literal["launch", "memory", "compute", "occupancy", "synchronization", "unknown"] = "unknown"
    created_at: datetime = Field(default_factory=utc_now)


class IterationEdge(ContractModel):
    kind: EdgeKind
    from_node: str
    to_node: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeviceProfile(ContractModel):
    profile_id: Name
    vendor: Literal["nvidia", "metax", "hygon", "ascend", "ppu"]
    host_alias: str
    device_index: int = Field(ge=0)
    device_name: str
    software: dict[str, str] = Field(default_factory=dict)
    peak_bandwidth_bytes_per_second: float | None = Field(default=None, gt=0)
    effective_bandwidth_bytes_per_second: float | None = Field(default=None, gt=0)
    effective_flops_per_second: float | None = Field(default=None, gt=0)
    monitoring_tool: str
    profiler_tools: list[str] = Field(default_factory=list)
    profiler_verified: bool = False


class ProfileArtifact(ContractModel):
    artifact_id: Name
    profile_id: Name
    node_id: Name | None = None
    tool: str
    kind: Literal["device_inspection", "kernel_trace", "instruction_profile", "system_trace"]
    path: RelativePath
    verified: bool = False
    summary: dict[str, Any] = Field(default_factory=dict)


class Environment(ContractModel):
    backend: str
    python: str
    platform: str
    fixture_protocol: str
    details: dict[str, str] = Field(default_factory=dict)


class Measurement(ContractModel):
    candidate_ms: float = Field(gt=0)
    baseline_ms: float = Field(gt=0)

    @property
    def speedup(self) -> float:
        return self.baseline_ms / self.candidate_ms


class WorkloadEvaluation(ContractModel):
    workload_id: Name
    role: Literal["target", "guard"]
    measurements: list[Measurement] = Field(min_length=1)
    candidate_median_ms: float = Field(gt=0)
    baseline_median_ms: float = Field(gt=0)
    speedup: float = Field(gt=0)
    noise: float = Field(ge=0)


class Evaluation(ContractModel):
    evaluation_id: Name
    node_id: Name
    baseline_node_id: Name
    protocol_version: str
    environment: Environment
    correctness: Literal["PASS", "FAIL"]
    correctness_cases: int = Field(ge=0)
    correctness_failures: list[str] = Field(default_factory=list)
    workloads: list[WorkloadEvaluation] = Field(default_factory=list)
    verdict: Literal["ACCEPT", "REJECT", "INVALID"]
    reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class AblationPlan(ContractModel):
    node_id: Name
    parent_node_id: Name
    change_id: Name
    protocol_version: str
    environment_fingerprint: str
    workloads: list[Name]
    construction: Literal["fixture_operation", "restore_parent_patch"]


class JobSpec(ContractModel):
    job_id: Name
    kind: Literal["correctness", "benchmark", "profile"]
    run_id: Name
    node_id: Name
    workspace: Path
    baseline_workspace: Path | None = None
    workload_ids: list[Name]
    timeout_seconds: float = Field(gt=0)


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class JobResult(ContractModel):
    job_id: Name
    status: JobStatus
    summary: dict[str, Any] = Field(default_factory=dict)
    log_path: str | None = None
    error: str | None = None
