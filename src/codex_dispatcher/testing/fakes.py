"""Deterministic in-memory test doubles for dispatcher ports."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from codex_dispatcher.executors.base import (
    ApplyResult,
    DiffResult,
    PreflightResult,
    RemoteRun,
    RunStatus,
    SubmissionRequest,
)
from codex_dispatcher.trackers.base import (
    ClaimResult,
    DraftPullRequestRequest,
    PullRequest,
    TaskState,
    TrackerComment,
    TrackerTask,
)


@dataclass(frozen=True, slots=True)
class Call:
    method: str
    args: tuple[object, ...]


class _ConfigurableFake:
    """Records every call before returning a configured outcome or raising."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._results: dict[str, Any] = {}
        self._exceptions: dict[str, BaseException] = {}

    def set_result(self, method: str, result: object) -> None:
        self._results[method] = result

    def set_exception(self, method: str, error: BaseException) -> None:
        self._exceptions[method] = error

    def _outcome(self, method: str, default: object) -> object:
        error = self._exceptions.get(method)
        if error is not None:
            raise error
        return self._results.get(method, default)

    def _record(self, method: str, *args: object) -> None:
        self.calls.append(Call(method, args))


class FakeTracker(_ConfigurableFake):
    """Configurable tracker fake with stable default read-only results."""

    def __init__(self) -> None:
        super().__init__()
        self.ready_tasks: tuple[TrackerTask, ...] = ()
        self.tasks: dict[str, TrackerTask] = {}
        self.comments: dict[str, tuple[TrackerComment, ...]] = {}
        self.pull_requests: dict[tuple[str, str], PullRequest] = {}

    def list_ready_tasks(self, repository: str) -> tuple[TrackerTask, ...]:
        self._record("list_ready_tasks", repository)
        default = tuple(task for task in self.ready_tasks if task.repository == repository)
        return self._outcome("list_ready_tasks", default)  # type: ignore[return-value]

    def list_open_tasks(
        self, repository: str, state: TaskState
    ) -> tuple[TrackerTask, ...]:
        self._record("list_open_tasks", repository, state)
        default = tuple(
            task
            for task in (*self.ready_tasks, *self.tasks.values())
            if task.repository == repository and task.state is state and task.is_open
        )
        unique = {task.task_id: task for task in default}
        ordered = tuple(unique[key] for key in sorted(unique, key=lambda item: int(item)))
        return self._outcome("list_open_tasks", ordered)  # type: ignore[return-value]

    def get_task(self, repository: str, task_id: str) -> TrackerTask | None:
        self._record("get_task", repository, task_id)
        task = self.tasks.get(task_id)
        default = task if task is not None and task.repository == repository else None
        return self._outcome("get_task", default)  # type: ignore[return-value]

    def list_comments(
        self, repository: str, task_id: str
    ) -> tuple[TrackerComment, ...]:
        self._record("list_comments", repository, task_id)
        task = self.tasks.get(task_id)
        default = (
            self.comments.get(task_id, ())
            if task is not None and task.repository == repository
            else ()
        )
        return self._outcome("list_comments", default)  # type: ignore[return-value]

    def claim(
        self,
        repository: str,
        task_id: str,
        claimant: str,
        *,
        approved_by: tuple[str, ...] | None = None,
    ) -> ClaimResult:
        self._record("claim", repository, task_id, claimant, approved_by)
        task = self.tasks.get(task_id)
        matches = task is not None and task.repository == repository
        default = ClaimResult(matches, task if matches else None)
        return self._outcome("claim", default)  # type: ignore[return-value]

    def set_state(self, repository: str, task_id: str, state: TaskState) -> TrackerTask:
        self._record("set_state", repository, task_id, state)
        task = self.tasks.get(task_id)
        if task is None or task.repository != repository:
            raise KeyError(task_id)
        default = replace(task, state=state)
        return self._outcome("set_state", default)  # type: ignore[return-value]

    def upsert_run_comment(self, repository: str, task_id: str, marker: str, body: str) -> None:
        self._record("upsert_run_comment", repository, task_id, marker, body)
        self._outcome("upsert_run_comment", None)

    def find_pr_by_branch(self, repository: str, branch_name: str) -> PullRequest | None:
        self._record("find_pr_by_branch", repository, branch_name)
        return self._outcome(
            "find_pr_by_branch", self.pull_requests.get((repository, branch_name))
        )  # type: ignore[return-value]

    def create_draft_pr(self, request: DraftPullRequestRequest) -> PullRequest:
        self._record("create_draft_pr", request)
        default = PullRequest(0, "", request.branch_name, request.title, True)
        return self._outcome("create_draft_pr", default)  # type: ignore[return-value]


class FakeExecutor(_ConfigurableFake):
    """Configurable executor fake whose default remote status is fail-closed."""

    def __init__(self) -> None:
        super().__init__()
        self.runs: tuple[RemoteRun, ...] = ()

    def preflight(self, environment_id: str) -> PreflightResult:
        self._record("preflight", environment_id)
        return self._outcome("preflight", PreflightResult(True))  # type: ignore[return-value]

    def submit(self, request: SubmissionRequest) -> RemoteRun:
        self._record("submit", request)
        default = RemoteRun(
            "", request.local_run_id, request.environment_id, RunStatus.UNKNOWN, request.branch_name
        )
        return self._outcome("submit", default)  # type: ignore[return-value]

    def list_runs(self, environment_id: str) -> tuple[RemoteRun, ...]:
        self._record("list_runs", environment_id)
        default = tuple(run for run in self.runs if run.environment_id == environment_id)
        return self._outcome("list_runs", default)  # type: ignore[return-value]

    def reconcile(self, external_task_id: str) -> RemoteRun | None:
        self._record("reconcile", external_task_id)
        default = next(
            (run for run in self.runs if run.external_task_id == external_task_id), None
        )
        return self._outcome("reconcile", default)  # type: ignore[return-value]

    def fetch_diff(self, external_task_id: str) -> DiffResult:
        self._record("fetch_diff", external_task_id)
        default = DiffResult(external_task_id, False)
        return self._outcome("fetch_diff", default)  # type: ignore[return-value]

    def apply(self, external_task_id: str, worktree: Path) -> ApplyResult:
        self._record("apply", external_task_id, worktree)
        default = ApplyResult(external_task_id, False)
        return self._outcome("apply", default)  # type: ignore[return-value]
