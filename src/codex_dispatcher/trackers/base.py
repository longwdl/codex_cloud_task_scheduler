"""Stable, offline-safe port for issue-tracker integrations.

Implementations translate their provider's objects into the immutable DTOs in
this module.  The dispatcher must never depend on a provider SDK object.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class TaskState(StrEnum):
    """Dispatcher-visible task states; unknown provider values are not valid."""

    READY = "ready"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    REVIEW = "review"
    NEEDS_INPUT = "needs_input"
    BLOCKED = "blocked"
    PAUSED = "paused"
    DISCARD = "discard"


@dataclass(frozen=True, slots=True)
class TrackerTask:
    """A reviewed tracker item eligible for dispatch or already being tracked."""

    repository: str
    task_id: str
    issue_number: int
    title: str
    body: str
    state: TaskState
    labels: tuple[str, ...]
    created_at: str
    ready_approved_by: str | None
    is_open: bool = True
    has_unresolved_dependencies: bool = False
    branch_name: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimResult:
    claimed: bool
    task: TrackerTask | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    url: str
    branch_name: str
    title: str
    is_draft: bool


@dataclass(frozen=True, slots=True)
class DraftPullRequestRequest:
    repository: str
    branch_name: str
    base_branch: str
    title: str
    body: str


class Tracker(Protocol):
    """Port for tracker reads and explicitly side-effecting writes.

    ``list_ready_tasks``, ``get_task``, and ``find_pr_by_branch`` are reads.
    Every other method is a write and must be safe for dispatcher retries where
    its provider supports idempotency.
    """

    def list_ready_tasks(self, repository: str) -> tuple[TrackerTask, ...]: ...

    def get_task(self, repository: str, task_id: str) -> TrackerTask | None: ...

    def claim(
        self,
        repository: str,
        task_id: str,
        claimant: str,
        *,
        approved_by: tuple[str, ...] | None = None,
    ) -> ClaimResult: ...

    def set_state(self, repository: str, task_id: str, state: TaskState) -> TrackerTask: ...

    def upsert_run_comment(self, repository: str, task_id: str, marker: str, body: str) -> None: ...

    def find_pr_by_branch(self, repository: str, branch_name: str) -> PullRequest | None: ...

    def create_draft_pr(self, request: DraftPullRequestRequest) -> PullRequest: ...
