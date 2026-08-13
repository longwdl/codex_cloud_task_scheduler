"""Stable, offline-safe port for task-execution integrations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class RunStatus(StrEnum):
    """Normalized remote state; unrecognized states must remain UNKNOWN."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def is_success(self) -> bool:
        """Only an explicit successful terminal state is a success."""

        return self is RunStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class PreflightResult:
    ok: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SubmissionRequest:
    local_run_id: str
    environment_id: str
    prompt: str
    branch_name: str
    base_sha: str


@dataclass(frozen=True, slots=True)
class RemoteRun:
    external_task_id: str
    local_run_id: str | None
    environment_id: str
    status: RunStatus
    branch_name: str
    url: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DiffResult:
    external_task_id: str
    available: bool
    sha256: str | None = None
    path: Path | None = None


@dataclass(frozen=True, slots=True)
class ApplyResult:
    external_task_id: str
    applied: bool
    detail: str | None = None


class Executor(Protocol):
    """Port for a remote task executor.

    ``preflight``, ``list_runs``, ``reconcile``, and ``fetch_diff`` are reads.
    ``submit`` and ``apply`` may write remote state.  Implementations must map
    any provider status they cannot recognize to :attr:`RunStatus.UNKNOWN`.
    """

    def preflight(self, environment_id: str) -> PreflightResult: ...

    def submit(self, request: SubmissionRequest) -> RemoteRun: ...

    def list_runs(self, environment_id: str) -> tuple[RemoteRun, ...]: ...

    def reconcile(self, external_task_id: str) -> RemoteRun | None: ...

    def fetch_diff(self, external_task_id: str) -> DiffResult: ...

    def apply(self, external_task_id: str, worktree: Path) -> ApplyResult: ...
