"""Hardware-independent job contracts."""

from __future__ import annotations

from typing import Protocol

from ..schemas import JobResult, JobSpec, JobStatus


class Runner(Protocol):
    async def submit(self, job: JobSpec) -> str: ...
    async def status(self, job_id: str) -> JobStatus: ...
    async def collect(self, job_id: str) -> JobResult: ...
