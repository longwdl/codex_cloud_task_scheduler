"""Pure planning between a claimed GitHub Issue and one persistent SSH WorkItem."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Iterable

from codex_dispatcher.config import RepositoryConfig
from codex_dispatcher.prompt_builder import PromptSnapshot, build_turn_prompt_snapshot
from codex_dispatcher.scheduler import SSH_CLI_EXECUTOR_LABEL
from codex_dispatcher.task_spec import (
    TaskSpec,
    TaskSpecError,
    is_path_allowed,
    parse_task_spec,
)
from codex_dispatcher.trackers.base import TaskState, TrackerTask
from codex_dispatcher.work_items import (
    WorkItem,
    WorkItemState,
    stable_work_item_identity,
    validate_git_sha,
)


class SshDispatchPlanningError(RuntimeError):
    """Raised when trusted state cannot safely map to one SSH WorkItem or Turn."""


class WorkItemAction(StrEnum):
    PREPARE = "prepare"
    RETRY_PREPARE = "retry_prepare"
    REACTIVATE = "reactivate"
    START_TURN = "start_turn"


@dataclass(frozen=True, slots=True)
class WorkItemResolution:
    work_item: WorkItem
    task_spec: TaskSpec
    issue_revision: str
    action: WorkItemAction
    is_new: bool


@dataclass(frozen=True, slots=True)
class SshTurnPlan:
    work_item: WorkItem
    task_spec: TaskSpec
    issue_revision: str
    turn_number: int
    input_head_sha: str
    prompt: PromptSnapshot


def resolve_ssh_work_item(
    *,
    task: TrackerTask,
    repository: RepositoryConfig,
    base_sha: str,
    existing_work_item: WorkItem | None = None,
    runner_root: str = "/srv/codex-runner/work-items",
    created_at: str | None = None,
) -> WorkItemResolution:
    """Resolve a post-claim Issue snapshot to its only valid persistent WorkItem."""
    task_spec, issue_revision = _validate_claimed_task(task, repository)
    validate_git_sha(base_sha, "base_sha")

    if existing_work_item is None:
        work_item = WorkItem.new(
            repository=task.repository,
            issue_number=task.issue_number,
            issue_node_id=_require_issue_node_id(task),
            base_branch=repository.base_branch,
            base_sha=base_sha,
            runner_root=runner_root,
            at=created_at,
        )
        return WorkItemResolution(
            work_item,
            task_spec,
            issue_revision,
            WorkItemAction.PREPARE,
            True,
        )

    _validate_existing_binding(task, repository, existing_work_item, runner_root)
    action_by_state = {
        WorkItemState.DISCOVERED: WorkItemAction.PREPARE,
        WorkItemState.PREPARING: WorkItemAction.RETRY_PREPARE,
        WorkItemState.READY: WorkItemAction.START_TURN,
        WorkItemState.WAITING_INPUT: WorkItemAction.REACTIVATE,
        WorkItemState.REVIEW: WorkItemAction.REACTIVATE,
        WorkItemState.BLOCKED: WorkItemAction.REACTIVATE,
        WorkItemState.PAUSED: WorkItemAction.REACTIVATE,
    }
    if existing_work_item.state is WorkItemState.COMPLETED:
        raise SshDispatchPlanningError(
            "completed WorkItem cannot be reopened; create a new Issue"
        )
    if existing_work_item.state is WorkItemState.RUNNING:
        raise SshDispatchPlanningError(
            "running WorkItem must be reconciled before another Turn"
        )
    try:
        action = action_by_state[existing_work_item.state]
    except KeyError as exc:  # pragma: no cover - enum exhaustiveness guard
        raise SshDispatchPlanningError("unsupported WorkItem state") from exc
    return WorkItemResolution(
        existing_work_item,
        task_spec,
        issue_revision,
        action,
        False,
    )


def build_ssh_turn_plan(
    *,
    task: TrackerTask,
    repository: RepositoryConfig,
    work_item: WorkItem,
    turn_number: int,
    comments: Iterable[object] = (),
) -> SshTurnPlan:
    """Freeze one deterministic Turn snapshot without persisting or invoking SSH."""
    task_spec, issue_revision = _validate_claimed_task(task, repository)
    _validate_existing_binding(
        task,
        repository,
        work_item,
        _runner_root_for(work_item),
    )
    if work_item.state is not WorkItemState.READY:
        raise SshDispatchPlanningError("WorkItem must be ready before planning a Turn")
    if type(turn_number) is not int or turn_number <= 0:
        raise SshDispatchPlanningError("turn_number must be a positive integer")
    input_head_sha = work_item.last_published_sha or work_item.base_sha
    prompt = build_turn_prompt_snapshot(
        work_item_id=work_item.work_item_id,
        turn_number=turn_number,
        issue_revision=issue_revision,
        repository=work_item.repository,
        branch=work_item.task_branch,
        input_head_sha=input_head_sha,
        issue_title=task.title,
        task_spec=task_spec,
        comments=comments,
        maintainers=repository.maintainers,
    )
    return SshTurnPlan(
        work_item,
        task_spec,
        issue_revision,
        turn_number,
        input_head_sha,
        prompt,
    )


def _validate_claimed_task(
    task: TrackerTask, repository: RepositoryConfig
) -> tuple[TaskSpec, str]:
    if not isinstance(task, TrackerTask) or not isinstance(repository, RepositoryConfig):
        raise TypeError("task and repository must use dispatcher DTOs")
    if task.repository != repository.slug:
        raise SshDispatchPlanningError("Issue repository does not match configuration")
    if task.task_id != str(task.issue_number) or task.issue_number <= 0:
        raise SshDispatchPlanningError("Issue number identity is invalid")
    if not task.is_open or task.state is not TaskState.DISPATCHING:
        raise SshDispatchPlanningError("Issue must be open and claimed for dispatch")
    if len(set(task.labels)) != len(task.labels):
        raise SshDispatchPlanningError("Issue labels must not contain duplicates")
    state_labels = tuple(label for label in task.labels if label.startswith("agent:"))
    if state_labels != ("agent:dispatching",):
        raise SshDispatchPlanningError("Issue must have exactly one dispatching state label")
    executor_labels = tuple(label for label in task.labels if label.startswith("exec:"))
    if executor_labels != (SSH_CLI_EXECUTOR_LABEL,):
        raise SshDispatchPlanningError("Issue is not assigned to the SSH CLI executor")
    if task.ready_approved_by not in repository.maintainers:
        raise SshDispatchPlanningError("Issue ready approval is not trusted")
    if task.has_unresolved_dependencies:
        raise SshDispatchPlanningError("Issue has unresolved dependencies")
    _require_issue_node_id(task)
    issue_revision = _require_timestamp(task.updated_at, "updated_at")
    _require_timestamp(task.created_at, "created_at")
    try:
        task_spec = parse_task_spec(task.body)
    except TaskSpecError as exc:
        raise SshDispatchPlanningError("Issue task specification is invalid") from exc
    if any(
        not is_path_allowed(path, repository.allowed_paths, repository.denied_paths)
        for path in task_spec.allowed_paths
    ):
        raise SshDispatchPlanningError("Issue path is outside repository policy")
    return task_spec, issue_revision


def _validate_existing_binding(
    task: TrackerTask,
    repository: RepositoryConfig,
    work_item: WorkItem,
    runner_root: str,
) -> None:
    if not isinstance(work_item, WorkItem):
        raise TypeError("work_item must be a WorkItem")
    issue_node_id = _require_issue_node_id(task)
    expected = stable_work_item_identity(
        repository=task.repository,
        issue_number=task.issue_number,
        issue_node_id=issue_node_id,
        runner_root=runner_root,
    )
    if (
        work_item.repository != task.repository
        or work_item.issue_number != task.issue_number
        or work_item.issue_node_id != issue_node_id
        or work_item.work_item_id != expected.work_item_id
        or work_item.runner_directory != expected.runner_directory
    ):
        raise SshDispatchPlanningError("Issue identity conflicts with persisted WorkItem")
    if work_item.base_branch != repository.base_branch:
        raise SshDispatchPlanningError("configured base branch conflicts with persisted WorkItem")
    if task.branch_name is not None and task.branch_name != work_item.task_branch:
        raise SshDispatchPlanningError("Issue branch binding conflicts with persisted WorkItem")


def _require_issue_node_id(task: TrackerTask) -> str:
    value = task.issue_node_id
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise SshDispatchPlanningError("Issue immutable node ID is missing or invalid")
    return value


def _require_timestamp(value: str | None, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise SshDispatchPlanningError(f"Issue {field} is missing or invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SshDispatchPlanningError(f"Issue {field} is missing or invalid") from exc
    if parsed.tzinfo is None:
        raise SshDispatchPlanningError(f"Issue {field} is missing or invalid")
    return value


def _runner_root_for(work_item: WorkItem) -> str:
    suffix_parts = (
        work_item.repository.replace("/", "__"),
        f"issue-{work_item.issue_number}",
    )
    path = work_item.runner_directory
    suffix = "/".join(suffix_parts)
    if not path.endswith(f"/{suffix}"):
        raise SshDispatchPlanningError("WorkItem runner directory identity is invalid")
    return path[: -(len(suffix) + 1)] or "/"
