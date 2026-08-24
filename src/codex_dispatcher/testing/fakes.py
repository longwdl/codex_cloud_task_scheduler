"""Deterministic in-memory test doubles for dispatcher ports."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

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
        self.branches: dict[tuple[str, str], str] = {}

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
        matches = (
            task is not None
            and task.repository == repository
            and task.is_open
            and task.state is TaskState.READY
            and approved_by is not None
            and task.ready_approved_by in approved_by
        )
        if matches:
            assert task is not None
            labels = tuple(
                "agent:dispatching" if label == "agent:ready" else label
                for label in task.labels
            )
            claimed = replace(task, state=TaskState.DISPATCHING, labels=labels)
            default = ClaimResult(True, claimed)
        else:
            default = ClaimResult(False, task, "task is not open and approved ready")
        outcome = self._outcome("claim", default)
        if "claim" not in self._results and matches:
            assert isinstance(outcome, ClaimResult) and outcome.task is not None
            self.tasks[task_id] = outcome.task
        return outcome  # type: ignore[return-value]

    def set_state(self, repository: str, task_id: str, state: TaskState) -> TrackerTask:
        self._record("set_state", repository, task_id, state)
        task = self.tasks.get(task_id)
        if task is None or task.repository != repository:
            raise KeyError(task_id)
        default = replace(task, state=state)
        labels = tuple(
            f"agent:{state.value}" if label.startswith("agent:") else label
            for label in task.labels
        )
        default = replace(default, labels=labels)
        outcome = self._outcome("set_state", default)
        if "set_state" not in self._results:
            assert isinstance(outcome, TrackerTask)
            self.tasks[task_id] = outcome
        return outcome  # type: ignore[return-value]

    def upsert_run_comment(self, repository: str, task_id: str, marker: str, body: str) -> None:
        self._record("upsert_run_comment", repository, task_id, marker, body)
        self._outcome("upsert_run_comment", None)

    def find_pr_by_branch(self, repository: str, branch_name: str) -> PullRequest | None:
        self._record("find_pr_by_branch", repository, branch_name)
        return self._outcome(
            "find_pr_by_branch", self.pull_requests.get((repository, branch_name))
        )  # type: ignore[return-value]

    def get_branch_head(self, repository: str, branch_name: str) -> str | None:
        self._record("get_branch_head", repository, branch_name)
        return self._outcome(
            "get_branch_head", self.branches.get((repository, branch_name))
        )  # type: ignore[return-value]

    def delete_branch(
        self,
        repository: str,
        branch_name: str,
        expected_head_sha: str,
    ) -> None:
        self._record("delete_branch", repository, branch_name, expected_head_sha)
        current = self.branches.get((repository, branch_name))
        if current is not None and current != expected_head_sha:
            raise ValueError("branch head does not match the expected checkpoint")
        self._outcome("delete_branch", None)
        if "delete_branch" not in self._results and current is not None:
            del self.branches[(repository, branch_name)]

    def create_draft_pr(self, request: DraftPullRequestRequest) -> PullRequest:
        self._record("create_draft_pr", request)
        default = PullRequest(
            1,
            f"https://github.com/{request.repository}/pull/1",
            request.branch_name,
            request.title,
            True,
            request.base_branch,
        )
        outcome = self._outcome("create_draft_pr", default)
        if isinstance(outcome, PullRequest):
            self.pull_requests[(request.repository, request.branch_name)] = outcome
        return outcome  # type: ignore[return-value]
