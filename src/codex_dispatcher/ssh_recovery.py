"""Read-only recovery planning before any new GitHub Issue is claimed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256

from codex_dispatcher.config import Config
from codex_dispatcher.scheduler import SSH_CLI_EXECUTOR_LABEL
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.runner_protocol import (
    NEXT_PROTOCOL_VERSION,
    RunnerOperation,
    RunnerRequest,
)
from codex_dispatcher.trackers.base import (
    PullRequest,
    PullRequestState,
    TaskState,
    Tracker,
    TrackerTask,
)
from codex_dispatcher.work_items import Turn, TurnState, WorkItem, WorkItemState
from codex_dispatcher.work_items import SessionGenerationRole
from codex_dispatcher.work_item_lifecycle import WorkItemArchiveStatus


class SshRecoveryAction(StrEnum):
    IDLE = "idle"
    RECONCILE_ACTIVE_TURN = "reconcile_active_turn"
    RESUME_PUBLICATION = "resume_publication"
    RESUME_PREPARATION = "resume_preparation"
    START_CLAIMED_TURN = "start_claimed_turn"
    START_FRESH_FINAL_AUDIT = "start_fresh_final_audit"
    RECOVER_ORPHAN_CLAIM = "recover_orphan_claim"
    SYNC_TRACKER_STATE = "sync_tracker_state"
    COMPLETE_MERGED_WORK_ITEM = "complete_merged_work_item"
    ARCHIVE_COMPLETED_WORK_ITEM = "archive_completed_work_item"
    RECONCILE_WORK_ITEM_ARCHIVE = "reconcile_work_item_archive"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class SshRecoveryPlan:
    action: SshRecoveryAction
    task: TrackerTask | None = None
    work_item: WorkItem | None = None
    turn: Turn | None = None
    desired_task_state: TaskState | None = None
    reason: str | None = None
    pull_request: PullRequest | None = None
    archive_eligible_at: str | None = None


def plan_ssh_recovery(
    config: Config,
    store: StateStore,
    tracker: Tracker,
    *,
    now: datetime | None = None,
) -> SshRecoveryPlan:
    """Return the only safe next recovery action using provider reads only."""
    configured = {repository.slug: repository for repository in config.repositories}
    active_turn = store.get_active_turn()
    if active_turn is not None:
        work_item = store.get_work_item(active_turn.work_item_id)
        if work_item is None:
            return _blocked("active_turn_work_item_missing", turn=active_turn)
        task = tracker.get_task(work_item.repository, str(work_item.issue_number))
        error = _binding_error(task, work_item, configured)
        if error is not None:
            return _blocked(error, task=task, work_item=work_item, turn=active_turn)
        assert task is not None
        if task.state not in {TaskState.DISPATCHING, TaskState.RUNNING}:
            return _blocked(
                "active_turn_tracker_state_conflict",
                task=task,
                work_item=work_item,
                turn=active_turn,
            )
        if active_turn.state is TurnState.PLANNED:
            return _blocked(
                "planned_turn_prompt_is_not_recoverable",
                task=task,
                work_item=work_item,
                turn=active_turn,
            )
        action = (
            SshRecoveryAction.RESUME_PUBLICATION
            if active_turn.state in {TurnState.CHECKPOINTING, TurnState.PUBLISHED}
            else SshRecoveryAction.RECONCILE_ACTIVE_TURN
        )
        return SshRecoveryPlan(
            action,
            task,
            work_item,
            active_turn,
        )

    final_audit = _plan_fresh_final_audit(config, store, tracker, configured)
    if final_audit is not None:
        return final_audit

    pending_states = {
        WorkItemState.DISCOVERED,
        WorkItemState.PREPARING,
        WorkItemState.READY,
        WorkItemState.RUNNING,
    }
    pending = tuple(
        item
        for item in store.list_work_items(include_completed=False)
        if item.state in pending_states
    )
    if len(pending) > 1:
        return _blocked("multiple_pending_work_items")
    if pending:
        work_item = pending[0]
        task = tracker.get_task(work_item.repository, str(work_item.issue_number))
        error = _binding_error(task, work_item, configured)
        if error is not None:
            return _blocked(error, task=task, work_item=work_item)
        assert task is not None
        if work_item.state is WorkItemState.RUNNING:
            return _blocked("running_work_item_has_no_active_turn", task=task, work_item=work_item)
        if task.state is not TaskState.DISPATCHING:
            return _blocked("pending_work_item_tracker_state_conflict", task=task, work_item=work_item)
        action = (
            SshRecoveryAction.START_CLAIMED_TURN
            if work_item.state is WorkItemState.READY
            else SshRecoveryAction.RESUME_PREPARATION
        )
        return SshRecoveryPlan(action, task, work_item)

    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("now must include a timezone")
    completion = _plan_merged_completion(
        config, store, tracker, configured, observed_at=observed_at
    )
    if completion is not None:
        return completion

    remote_claims: list[TrackerTask] = []
    for repository in config.repositories:
        for state in (TaskState.DISPATCHING, TaskState.RUNNING):
            remote_claims.extend(tracker.list_open_tasks(repository.slug, state))
    if not remote_claims:
        return SshRecoveryPlan(SshRecoveryAction.IDLE)
    if len(remote_claims) > 1:
        return _blocked("multiple_remote_claims")
    task = remote_claims[0]
    repository = configured[task.repository]
    if task.ready_approved_by not in repository.maintainers:
        return _blocked("orphan_claim_approval_untrusted", task=task)
    if _task_identity_error(task) is not None:
        return _blocked("orphan_claim_identity_invalid", task=task)
    executor_labels = tuple(label for label in task.labels if label.startswith("exec:"))
    if executor_labels != (SSH_CLI_EXECUTOR_LABEL,):
        return _blocked("orphan_claim_executor_conflict", task=task)
    existing = store.get_work_item_by_issue(task.repository, task.issue_number)
    if existing is not None:
        desired = {
            WorkItemState.WAITING_INPUT: TaskState.NEEDS_INPUT,
            WorkItemState.REVIEW: TaskState.REVIEW,
            WorkItemState.BLOCKED: TaskState.BLOCKED,
            WorkItemState.PAUSED: TaskState.PAUSED,
            WorkItemState.COMPLETED: TaskState.COMPLETED,
        }.get(existing.state)
        error = _binding_error(task, existing, configured)
        if error is not None:
            return _blocked(error, task=task, work_item=existing)
        if desired is None:
            return _blocked(
                "remote_claim_conflicts_with_unexpected_work_item",
                task=task,
                work_item=existing,
            )
        return SshRecoveryPlan(
            SshRecoveryAction.SYNC_TRACKER_STATE,
            task=task,
            work_item=existing,
            desired_task_state=desired,
        )
    if task.state is TaskState.RUNNING:
        return _blocked("orphan_running_issue")
    return SshRecoveryPlan(SshRecoveryAction.RECOVER_ORPHAN_CLAIM, task=task)


def _plan_fresh_final_audit(
    config: Config,
    store: StateStore,
    tracker: Tracker,
    configured: dict[str, object],
) -> SshRecoveryPlan | None:
    runtime = config.session_runtime
    if runtime is None or not runtime.rotate_before_final_audit:
        return None
    for work_item in store.list_work_items(include_completed=False):
        if work_item.state not in {WorkItemState.REVIEW, WorkItemState.READY}:
            continue
        turns = store.list_turns(work_item.work_item_id)
        if not turns:
            continue
        source_turn = turns[-1]
        gate = store.get_turn_completion_gate(source_turn.turn_id)
        generation = store.get_turn_session_generation(source_turn.turn_id)
        if (
            source_turn.state is not TurnState.FINISHED
            or source_turn.result_status != "completed"
            or gate is None
            or gate.status.value != "passed"
            or generation is None
            or generation.role is not SessionGenerationRole.IMPLEMENTATION
        ):
            continue
        task = tracker.get_task(work_item.repository, str(work_item.issue_number))
        error = _binding_error(task, work_item, configured)
        if error is not None:
            return _blocked(error, task=task, work_item=work_item, turn=source_turn)
        assert task is not None
        if task.state not in {TaskState.DISPATCHING, TaskState.RUNNING}:
            return _blocked(
                "final_audit_tracker_state_conflict",
                task=task,
                work_item=work_item,
                turn=source_turn,
            )
        return SshRecoveryPlan(
            SshRecoveryAction.START_FRESH_FINAL_AUDIT,
            task=task,
            work_item=work_item,
            turn=source_turn,
        )
    return None


def _plan_merged_completion(
    config: Config,
    store: StateStore,
    tracker: Tracker,
    configured: dict[str, object],
    *,
    observed_at: datetime,
) -> SshRecoveryPlan | None:
    for work_item in store.list_work_items():
        if work_item.state not in {WorkItemState.REVIEW, WorkItemState.COMPLETED}:
            continue
        task = tracker.get_task(work_item.repository, str(work_item.issue_number))
        error = _binding_error(task, work_item, configured)
        if error is not None:
            return _blocked(error, task=task, work_item=work_item)
        assert task is not None
        if work_item.state is WorkItemState.COMPLETED and task.state not in {
            TaskState.REVIEW,
            TaskState.COMPLETED,
        }:
            return _blocked(
                "completed_work_item_tracker_state_conflict",
                task=task,
                work_item=work_item,
            )
        if task.state in {TaskState.DISPATCHING, TaskState.RUNNING}:
            continue
        if task.state not in {
            TaskState.READY,
            TaskState.REVIEW,
            TaskState.COMPLETED,
        }:
            return _blocked(
                "review_work_item_tracker_state_conflict",
                task=task,
                work_item=work_item,
            )
        if work_item.last_published_sha is None or work_item.pr_number is None:
            if task.state is TaskState.COMPLETED or work_item.state is WorkItemState.COMPLETED:
                return _blocked(
                    "completed_work_item_has_no_published_pr",
                    task=task,
                    work_item=work_item,
                )
            continue
        pull_request = tracker.find_pr_by_branch(
            work_item.repository,
            work_item.task_branch,
        )
        pr_error = _completion_pull_request_error(pull_request, work_item)
        if pr_error is not None:
            return _blocked(
                pr_error,
                task=task,
                work_item=work_item,
                pull_request=pull_request,
            )
        assert pull_request is not None
        if pull_request.state is PullRequestState.OPEN:
            if task.state is TaskState.COMPLETED:
                return _blocked(
                    "completed_issue_pull_request_not_merged",
                    task=task,
                    work_item=work_item,
                    pull_request=pull_request,
                )
            continue
        if pull_request.state is PullRequestState.CLOSED:
            return _blocked(
                "review_pull_request_closed_without_merge",
                task=task,
                work_item=work_item,
                pull_request=pull_request,
            )
        if task.state is TaskState.READY:
            return _blocked(
                "merged_work_item_cannot_be_reactivated",
                task=task,
                work_item=work_item,
                pull_request=pull_request,
            )
        if work_item.state is WorkItemState.COMPLETED:
            if task.state is TaskState.REVIEW:
                return SshRecoveryPlan(
                    SshRecoveryAction.SYNC_TRACKER_STATE,
                    task=task,
                    work_item=work_item,
                    desired_task_state=TaskState.COMPLETED,
                    pull_request=pull_request,
                )
            archive = _plan_completed_archive(
                config,
                store,
                task,
                work_item,
                pull_request,
                observed_at=observed_at,
            )
            if archive is not None:
                return archive
            continue
        return SshRecoveryPlan(
            SshRecoveryAction.COMPLETE_MERGED_WORK_ITEM,
            task=task,
            work_item=work_item,
            pull_request=pull_request,
        )
    return None


def _plan_completed_archive(
    config: Config,
    store: StateStore,
    task: TrackerTask,
    work_item: WorkItem,
    pull_request: PullRequest,
    *,
    observed_at: datetime,
) -> SshRecoveryPlan | None:
    assert work_item.last_published_sha is not None
    request = RunnerRequest(
        RunnerOperation.ARCHIVE,
        work_item.work_item_id,
        version=NEXT_PROTOCOL_VERSION,
        expected_head_sha=work_item.last_published_sha,
    )
    request_sha256 = sha256(request.to_json().encode("utf-8")).hexdigest()
    record = store.get_work_item_archive(work_item.work_item_id)
    if record is not None:
        if (
            record.expected_head_sha != work_item.last_published_sha
            or record.request_sha256 != request_sha256
        ):
            return _blocked(
                "work_item_archive_identity_conflict",
                task=task,
                work_item=work_item,
                pull_request=pull_request,
            )
        if record.status is WorkItemArchiveStatus.ARCHIVED:
            return None
        if record.status is WorkItemArchiveStatus.BLOCKED:
            return _blocked(
                "work_item_archive_blocked",
                task=task,
                work_item=work_item,
                pull_request=pull_request,
            )
        action = (
            SshRecoveryAction.RECONCILE_WORK_ITEM_ARCHIVE
            if record.status is WorkItemArchiveStatus.AMBIGUOUS
            else SshRecoveryAction.ARCHIVE_COMPLETED_WORK_ITEM
        )
        return SshRecoveryPlan(
            action,
            task=task,
            work_item=work_item,
            pull_request=pull_request,
            archive_eligible_at=record.eligible_at,
        )

    runtime = config.ssh_runtime
    if runtime is None or runtime.completed_retention_seconds is None:
        return None
    try:
        completed_at = datetime.fromisoformat(
            store.get_work_item_completed_at(work_item.work_item_id)
        )
    except (RuntimeError, ValueError):
        return _blocked(
            "completed_work_item_completion_receipt_invalid",
            task=task,
            work_item=work_item,
            pull_request=pull_request,
        )
    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        return _blocked(
            "completed_work_item_completion_receipt_invalid",
            task=task,
            work_item=work_item,
            pull_request=pull_request,
        )
    eligible = completed_at + timedelta(
        seconds=runtime.completed_retention_seconds
    )
    eligible_at = eligible.isoformat()
    if observed_at < eligible:
        return None
    return SshRecoveryPlan(
        SshRecoveryAction.ARCHIVE_COMPLETED_WORK_ITEM,
        task=task,
        work_item=work_item,
        pull_request=pull_request,
        archive_eligible_at=eligible_at,
    )


def _completion_pull_request_error(
    pull_request: PullRequest | None,
    work_item: WorkItem,
) -> str | None:
    if pull_request is None:
        return "persisted_pull_request_missing"
    if (
        pull_request.number != work_item.pr_number
        or pull_request.url
        != f"https://github.com/{work_item.repository}/pull/{work_item.pr_number}"
        or pull_request.branch_name != work_item.task_branch
        or pull_request.base_branch != work_item.base_branch
        or pull_request.is_cross_repository
    ):
        return "persisted_pull_request_identity_conflict"
    if pull_request.head_sha != work_item.last_published_sha:
        return "persisted_pull_request_head_conflict"
    if pull_request.state is PullRequestState.MERGED and pull_request.is_draft:
        return "merged_pull_request_is_still_draft"
    return None


def _binding_error(
    task: TrackerTask | None,
    work_item: WorkItem,
    configured: dict[str, object],
) -> str | None:
    if task is None:
        return "persisted_issue_missing"
    if work_item.repository not in configured:
        return "persisted_repository_not_configured"
    if _task_identity_error(task) is not None:
        return "persisted_issue_identity_invalid"
    if (
        task.repository != work_item.repository
        or task.issue_number != work_item.issue_number
        or task.issue_node_id != work_item.issue_node_id
    ):
        return "persisted_issue_identity_conflict"
    executor_labels = tuple(label for label in task.labels if label.startswith("exec:"))
    if executor_labels != (SSH_CLI_EXECUTOR_LABEL,):
        return "persisted_issue_executor_conflict"
    return None


def _task_identity_error(task: TrackerTask) -> str | None:
    if (
        task.task_id != str(task.issue_number)
        or task.issue_number <= 0
        or not isinstance(task.issue_node_id, str)
        or not task.issue_node_id
        or len(task.issue_node_id) > 256
    ):
        return "invalid"
    return None


def _blocked(
    reason: str,
    *,
    task: TrackerTask | None = None,
    work_item: WorkItem | None = None,
    turn: Turn | None = None,
    pull_request: PullRequest | None = None,
) -> SshRecoveryPlan:
    return SshRecoveryPlan(
        SshRecoveryAction.BLOCK,
        task=task,
        work_item=work_item,
        turn=turn,
        reason=reason,
        pull_request=pull_request,
    )
