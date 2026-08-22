"""Pure planning between a claimed GitHub Issue and one persistent SSH WorkItem."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Iterable

from codex_dispatcher.ci_evidence import ActionsEvidenceSnapshot
from codex_dispatcher.config import RepositoryConfig
from codex_dispatcher.handoffs import (
    PublishedCheckpoint,
    SessionHandoffSnapshot,
    build_session_handoff_snapshot,
)
from codex_dispatcher.prompt_builder import (
    ApprovedContextItem,
    CanonicalInputSnapshot,
    PromptSnapshot,
    build_canonical_input_snapshot,
    build_generation_delta_prompt_snapshot,
    build_generation_full_prompt_snapshot,
    build_turn_prompt_snapshot,
    canonical_approved_context_sha256,
)
from codex_dispatcher.scheduler import SSH_CLI_EXECUTOR_LABEL
from codex_dispatcher.runner_protocol import AgentResult
from codex_dispatcher.task_spec import (
    TaskSpec,
    TaskSpecError,
    is_path_allowed,
    parse_task_spec,
)
from codex_dispatcher.trackers.base import TaskState, TrackerTask
from codex_dispatcher.work_items import (
    PromptKind,
    SessionGeneration,
    SessionGenerationState,
    Turn,
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


@dataclass(frozen=True, slots=True)
class SshGenerationTurnPlan:
    work_item: WorkItem
    session_generation: SessionGeneration
    task_spec: TaskSpec
    issue_revision: str
    turn_number: int
    input_head_sha: str
    inputs: CanonicalInputSnapshot
    prompt_kind: PromptKind
    prompt: PromptSnapshot
    handoff: SessionHandoffSnapshot | None


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


def build_ssh_generation_turn_plan(
    *,
    task: TrackerTask,
    repository: RepositoryConfig,
    work_item: WorkItem,
    session_generation: SessionGeneration,
    turn_number: int,
    agent_policy_digest: str,
    comments: Iterable[object] = (),
    delivered_comment_ids: tuple[str, ...] = (),
    delivered_context_sha256: str | None = None,
    prior_status: str | None = None,
    prior_summary: str | None = None,
    handoff: SessionHandoffSnapshot | None = None,
) -> SshGenerationTurnPlan:
    """Freeze a fail-closed full or incremental protocol-v2 Turn."""
    task_spec, issue_revision = _validate_claimed_task(task, repository)
    _validate_existing_binding(
        task,
        repository,
        work_item,
        _runner_root_for(work_item),
    )
    if work_item.state is not WorkItemState.READY:
        raise SshDispatchPlanningError("WorkItem must be ready before planning a Turn")
    if not isinstance(session_generation, SessionGeneration):
        raise TypeError("session_generation must be a SessionGeneration")
    if (
        session_generation.work_item_id != work_item.work_item_id
        or session_generation.policy_sha256 != agent_policy_digest
    ):
        raise SshDispatchPlanningError(
            "session generation identity conflicts with the WorkItem or policy"
        )
    if type(turn_number) is not int or turn_number <= 0:
        raise SshDispatchPlanningError("turn_number must be a positive integer")
    input_head_sha = work_item.last_published_sha or work_item.base_sha
    generation_head_sha = (
        session_generation.last_published_sha or session_generation.start_head_sha
    )
    if input_head_sha != generation_head_sha:
        raise SshDispatchPlanningError(
            "session generation publication anchor conflicts with the WorkItem"
        )
    inputs = build_canonical_input_snapshot(
        issue_title=task.title,
        task_spec=task_spec,
        comments=comments,
        maintainers=repository.maintainers,
    )
    prompt_arguments = {
        "work_item_id": work_item.work_item_id,
        "session_generation_id": session_generation.session_generation_id,
        "session_generation": session_generation.generation_number,
        "agent_policy_digest": agent_policy_digest,
        "turn_number": turn_number,
        "issue_revision": issue_revision,
        "repository": work_item.repository,
        "branch": work_item.task_branch,
        "input_head_sha": input_head_sha,
        "inputs": inputs,
    }
    if session_generation.state is SessionGenerationState.PLANNED:
        if any(
            value
            for value in (
                delivered_comment_ids,
                delivered_context_sha256,
                prior_status,
                prior_summary,
            )
        ):
            raise SshDispatchPlanningError(
                "a planned generation cannot carry resume-only inputs"
            )
        prompt_kind = PromptKind.FULL
        if session_generation.generation_number == 1:
            if handoff is not None:
                raise SshDispatchPlanningError(
                    "the first session generation cannot contain a handoff"
                )
        elif handoff is None:
            raise SshDispatchPlanningError(
                "a replacement session generation requires a durable handoff"
            )
        try:
            prompt = build_generation_full_prompt_snapshot(
                **prompt_arguments, handoff=handoff
            )
        except (TypeError, ValueError) as exc:
            raise SshDispatchPlanningError(str(exc)) from exc
    elif session_generation.state is SessionGenerationState.ACTIVE:
        if handoff is not None:
            raise SshDispatchPlanningError(
                "an active session generation cannot replay a handoff"
            )
        _validate_active_generation_inputs(
            session_generation=session_generation,
            inputs=inputs,
            delivered_comment_ids=delivered_comment_ids,
            delivered_context_sha256=delivered_context_sha256,
        )
        if (
            not isinstance(delivered_comment_ids, tuple)
            or tuple(sorted(delivered_comment_ids)) != delivered_comment_ids
            or len(set(delivered_comment_ids)) != len(delivered_comment_ids)
            or not isinstance(prior_status, str)
            or not isinstance(prior_summary, str)
        ):
            raise SshDispatchPlanningError(
                "active generation resume evidence is incomplete or non-canonical"
            )
        delivered = frozenset(delivered_comment_ids)
        new_items: tuple[ApprovedContextItem, ...] = tuple(
            item for item in inputs.approved_items if item.comment_id not in delivered
        )
        if not new_items:
            raise SshDispatchPlanningError(
                "same-generation resume requires new approved context"
            )
        prompt_kind = PromptKind.DELTA
        prompt = build_generation_delta_prompt_snapshot(
            **prompt_arguments,
            prior_status=prior_status,
            prior_summary=prior_summary,
            new_approved_items=new_items,
        )
    else:
        raise SshDispatchPlanningError(
            "session generation is not eligible to plan a Turn"
        )
    return SshGenerationTurnPlan(
        work_item=work_item,
        session_generation=session_generation,
        task_spec=task_spec,
        issue_revision=issue_revision,
        turn_number=turn_number,
        input_head_sha=input_head_sha,
        inputs=inputs,
        prompt_kind=prompt_kind,
        prompt=prompt,
        handoff=handoff,
    )


def build_ssh_session_handoff_snapshot(
    *,
    task: TrackerTask,
    repository: RepositoryConfig,
    work_item: WorkItem,
    from_generation: SessionGeneration,
    to_session_generation_id: str,
    to_generation_number: int,
    to_agent_policy_sha256: str,
    rotation_reason: str,
    published_checkpoints: tuple[PublishedCheckpoint, ...],
    source_turn: Turn | None,
    source_agent_result: AgentResult | None,
    comments: Iterable[object],
    created_at: str,
    actions_evidence: ActionsEvidenceSnapshot | None = None,
) -> SessionHandoffSnapshot:
    """Freeze trusted Dispatcher/Git/CI facts and a separate untrusted advisory."""
    task_spec, issue_revision = _validate_claimed_task(task, repository)
    _validate_existing_binding(task, repository, work_item, _runner_root_for(work_item))
    if from_generation.work_item_id != work_item.work_item_id:
        raise SshDispatchPlanningError(
            "source session generation does not belong to the WorkItem"
        )
    if from_generation.state is not SessionGenerationState.ACTIVE:
        raise SshDispatchPlanningError(
            "only an active session generation can produce a handoff"
        )
    inputs = build_canonical_input_snapshot(
        issue_title=task.title,
        task_spec=task_spec,
        comments=comments,
        maintainers=repository.maintainers,
    )
    if from_generation.rotation_reason != "legacy_migration":
        if (
            from_generation.baseline_issue_content_sha256
            != inputs.issue_content_sha256
            or from_generation.baseline_task_spec_sha256 != inputs.task_spec_sha256
        ):
            raise SshDispatchPlanningError(
                "source generation semantic baseline conflicts with handoff inputs"
            )
    try:
        return build_session_handoff_snapshot(
            work_item=work_item,
            from_generation=from_generation,
            to_session_generation_id=to_session_generation_id,
            to_generation_number=to_generation_number,
            to_agent_policy_sha256=to_agent_policy_sha256,
            rotation_reason=rotation_reason,
            issue_revision=issue_revision,
            issue_content_sha256=inputs.issue_content_sha256,
            task_spec_sha256=inputs.task_spec_sha256,
            approved_context_sha256=inputs.approved_context_sha256,
            acceptance_criteria=task_spec.acceptance_criteria,
            required_checks=repository.required_checks,
            published_checkpoints=published_checkpoints,
            source_turn_id=source_turn.turn_id if source_turn is not None else None,
            source_result_status=(
                source_turn.result_status if source_turn is not None else None
            ),
            source_result_summary=(
                source_turn.result_summary if source_turn is not None else None
            ),
            source_agent_result=source_agent_result,
            created_at=created_at,
            acceptance_items=task_spec.acceptance_items,
            allowed_paths=task_spec.allowed_paths,
            actions_evidence=actions_evidence,
        )
    except (TypeError, ValueError) as exc:
        raise SshDispatchPlanningError(str(exc)) from exc


def validate_ssh_active_generation_continuity(
    *,
    task: TrackerTask,
    repository: RepositoryConfig,
    work_item: WorkItem,
    session_generation: SessionGeneration,
    comments: Iterable[object],
    delivered_comment_ids: tuple[str, ...],
    delivered_context_sha256: str,
) -> None:
    """Reject semantic or delivered-context drift before any session rotation."""
    task_spec, _ = _validate_claimed_task(task, repository)
    _validate_existing_binding(
        task,
        repository,
        work_item,
        _runner_root_for(work_item),
    )
    if work_item.state is not WorkItemState.READY:
        raise SshDispatchPlanningError("WorkItem must be ready before validation")
    if session_generation.state is not SessionGenerationState.ACTIVE:
        raise SshDispatchPlanningError(
            "only an active session generation has continuity to validate"
        )
    input_head_sha = work_item.last_published_sha or work_item.base_sha
    generation_head_sha = (
        session_generation.last_published_sha or session_generation.start_head_sha
    )
    if input_head_sha != generation_head_sha:
        raise SshDispatchPlanningError(
            "session generation publication anchor conflicts with the WorkItem"
        )
    inputs = build_canonical_input_snapshot(
        issue_title=task.title,
        task_spec=task_spec,
        comments=comments,
        maintainers=repository.maintainers,
    )
    _validate_active_generation_inputs(
        session_generation=session_generation,
        inputs=inputs,
        delivered_comment_ids=delivered_comment_ids,
        delivered_context_sha256=delivered_context_sha256,
    )


def _validate_active_generation_inputs(
    *,
    session_generation: SessionGeneration,
    inputs: CanonicalInputSnapshot,
    delivered_comment_ids: tuple[str, ...],
    delivered_context_sha256: str | None,
) -> None:
    if (
        session_generation.codex_session_id is None
        or session_generation.baseline_issue_content_sha256 is None
        or session_generation.baseline_task_spec_sha256 is None
    ):
        raise SshDispatchPlanningError(
            "active session generation is missing its immutable baseline"
        )
    if (
        inputs.issue_content_sha256
        != session_generation.baseline_issue_content_sha256
        or inputs.task_spec_sha256
        != session_generation.baseline_task_spec_sha256
    ):
        raise SshDispatchPlanningError(
            "Issue semantic content changed after this session generation started"
        )
    if (
        not isinstance(delivered_comment_ids, tuple)
        or tuple(sorted(delivered_comment_ids)) != delivered_comment_ids
        or len(set(delivered_comment_ids)) != len(delivered_comment_ids)
        or not isinstance(delivered_context_sha256, str)
    ):
        raise SshDispatchPlanningError(
            "active generation resume evidence is incomplete or non-canonical"
        )
    current_by_id = {item.comment_id: item for item in inputs.approved_items}
    try:
        delivered_items = tuple(
            current_by_id[comment_id] for comment_id in delivered_comment_ids
        )
    except KeyError as exc:
        raise SshDispatchPlanningError(
            "previously delivered approved context was removed"
        ) from exc
    if canonical_approved_context_sha256(delivered_items) != delivered_context_sha256:
        raise SshDispatchPlanningError(
            "previously delivered approved context was edited"
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
