"""Read-only recovery planning before any new GitHub Issue is claimed."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from codex_dispatcher.config import Config
from codex_dispatcher.scheduler import SSH_CLI_EXECUTOR_LABEL
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.trackers.base import TaskState, Tracker, TrackerTask
from codex_dispatcher.work_items import Turn, TurnState, WorkItem, WorkItemState


class SshRecoveryAction(StrEnum):
    IDLE = "idle"
    RECONCILE_ACTIVE_TURN = "reconcile_active_turn"
    RESUME_PUBLICATION = "resume_publication"
    RESUME_PREPARATION = "resume_preparation"
    START_CLAIMED_TURN = "start_claimed_turn"
    RECOVER_ORPHAN_CLAIM = "recover_orphan_claim"
    SYNC_TRACKER_STATE = "sync_tracker_state"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class SshRecoveryPlan:
    action: SshRecoveryAction
    task: TrackerTask | None = None
    work_item: WorkItem | None = None
    turn: Turn | None = None
    desired_task_state: TaskState | None = None
    reason: str | None = None


def plan_ssh_recovery(
    config: Config,
    store: StateStore,
    tracker: Tracker,
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
    if task.state is TaskState.RUNNING:
        return _blocked("orphan_running_issue")
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
    return SshRecoveryPlan(SshRecoveryAction.RECOVER_ORPHAN_CLAIM, task=task)


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
) -> SshRecoveryPlan:
    return SshRecoveryPlan(
        SshRecoveryAction.BLOCK,
        task=task,
        work_item=work_item,
        turn=turn,
        reason=reason,
    )
