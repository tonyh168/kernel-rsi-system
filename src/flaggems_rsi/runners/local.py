"""Safe fixed-argv subprocess execution for local simulation and future adapters."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from ..schemas import JobResult, JobSpec, JobStatus, RunConfig
from ..storage import atomic_write, json_text, read_jsonl


class LocalRunner:
    """Run configured executables; agents cannot provide commands or arguments."""

    def __init__(self, config: RunConfig, run_root: Path):
        self.config = config
        self.run_root = run_root.resolve()
        self.jobs_dir = self.run_root / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, JobSpec] = {}
        self._status: dict[str, JobStatus] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._terminal: dict[str, JobStatus] = {}
        self.harness = Path(__file__).resolve().parents[3] / "examples" / "sim_kernel" / "harness.py"
        if not self.harness.exists():
            self.harness = Path(sys.argv[0]).resolve().parent.parent / "examples" / "sim_kernel" / "harness.py"
        for job in read_jsonl(self.jobs_dir / "jobs.jsonl"):
            if job.get("event") == "submitted":
                spec = JobSpec.model_validate(job["spec"])
                self._jobs[spec.job_id] = spec
                self._status[spec.job_id] = JobStatus.PENDING
            elif job.get("event") in {"started", "completed", "failed", "timed_out"}:
                job_id = job["spec"]["job_id"]
                self._terminal[job_id] = {
                    "started": JobStatus.RUNNING, "completed": JobStatus.SUCCEEDED,
                    "failed": JobStatus.FAILED, "timed_out": JobStatus.TIMED_OUT,
                }[job["event"]]
        for job_id, status in self._terminal.items():
            self._status[job_id] = status
        for job_id, spec in self._jobs.items():
            if self._status[job_id] in {JobStatus.PENDING, JobStatus.RUNNING}:
                self._status[job_id] = JobStatus.FAILED
                (self.jobs_dir / job_id).mkdir(parents=True, exist_ok=True)
                (self.jobs_dir / job_id / "stderr.log").write_text(
                    "Job was interrupted before the runner restarted", encoding="utf-8",
                )

    def _record(self, event: str, job: JobSpec, data: dict[str, Any] | None = None) -> None:
        from ..storage import append_jsonl
        append_jsonl(self.jobs_dir / "jobs.jsonl", [{
            "event": event, "spec": job.model_dump(mode="json"), "data": data or {},
        }])

    async def submit(self, job: JobSpec) -> str:
        if job.job_id in self._jobs:
            raise ValueError(f"Duplicate job ID: {job.job_id}")
        if not job.workspace.resolve().is_relative_to(self.run_root):
            raise ValueError("Job workspace must be within the run directory")
        self._jobs[job.job_id] = job
        self._status[job.job_id] = JobStatus.PENDING
        self._record("submitted", job)
        self._tasks[job.job_id] = asyncio.create_task(self._run_guarded(job))
        return job.job_id

    async def _run_guarded(self, job: JobSpec) -> None:
        try:
            await self._execute(job)
        except Exception as error:
            self._status[job.job_id] = JobStatus.FAILED
            job_dir = self.jobs_dir / job.job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "stderr.log").write_text(f"{type(error).__name__}: {error}", encoding="utf-8")
            self._record("failed", job, {"error": str(error)})

    async def _execute(self, job: JobSpec) -> None:
        job_dir = self.jobs_dir / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        result_path = job_dir / "result.json"
        if self.config.mode == "simulated":
            if not self.harness.exists():
                raise FileNotFoundError(f"Simulation harness not found: {self.harness}")
            command = [sys.executable, str(self.harness)]
        else:
            command = list(self.config.command or [])
            if not command:
                raise ValueError("Missing fixed local command")
        request = job.model_dump(mode="json")
        baseline_workspace = request.pop("baseline_workspace", None)
        request.update({
            "workloads": [item.model_dump(mode="json") for item in self.config.workloads
                          if item.workload_id in job.workload_ids],
            "repeats": self.config.evaluation.repeats,
            "warmup": self.config.evaluation.warmup,
            "result_path": str(result_path),
            "baseline": baseline_workspace,
        })
        # The simulated evaluator is allowed to know the private holdout, but the
        # JobSpec and every model-facing tool summary never contain its cases.
        if self.config.private_holdout:
            request["private_holdout"] = str(self.config.private_holdout)
        request_path = job_dir / "request.json"
        atomic_write(request_path, json_text(request))
        process = await asyncio.create_subprocess_exec(
            *command, str(request_path), cwd=job.workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
        self._status[job.job_id] = JobStatus.RUNNING
        self._record("started", job)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=job.timeout_seconds)
        except TimeoutError:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            await process.communicate()
            self._status[job.job_id] = JobStatus.TIMED_OUT
            self._record("timed_out", job)
            return
        (job_dir / "stdout.log").write_bytes(stdout)
        (job_dir / "stderr.log").write_bytes(stderr)
        succeeded = process.returncode == 0 and result_path.exists()
        self._status[job.job_id] = JobStatus.SUCCEEDED if succeeded else JobStatus.FAILED
        self._record("completed" if succeeded else "failed", job, {"returncode": process.returncode})

    async def status(self, job_id: str) -> JobStatus:
        if job_id not in self._jobs:
            raise KeyError(job_id)
        return self._status[job_id]

    async def collect(self, job_id: str) -> JobResult:
        task = self._tasks.get(job_id)
        if task:
            await task
        if job_id not in self._jobs:
            raise KeyError(job_id)
        job_dir = self.jobs_dir / job_id
        result_path = job_dir / "result.json"
        status = self._status[job_id]
        summary = json.loads(result_path.read_text()) if status == JobStatus.SUCCEEDED else {}
        error = None
        if status != JobStatus.SUCCEEDED:
            stderr = job_dir / "stderr.log"
            error = stderr.read_text(errors="replace")[-2000:] if stderr.exists() else status.value
        return JobResult(job_id=job_id, status=status, summary=summary,
                         log_path=str(job_dir), error=error)

    async def wait(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks.values())
