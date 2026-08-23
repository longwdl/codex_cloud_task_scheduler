"""Read-only recovery planning before any new GitHub Issue is claimed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256

from codex_dispatcher.config import Config
from codex_dispatcher.scheduler import SSH_CLI_EXECUTOR_LABEL
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.terminal_retention import (
    TerminalBranchCleanupState,
    terminal_branch_request_sha256,
)
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
from codex_dispatcher.work_items import (
    Turn,
    TurnState,
    WorkItem,
    WorkItemState,
    validate_work_item_id,
)
from codex_dispatcher.work_items import SessionGenerationRole
from codex_dispatcher.work_item_lifecycle import (
    WorkItemArchiveStatus,
    WorkItemDisposition,
    WorkItemDispositionKind,
)


class SshRecoveryAction(StrEnum):
    IDLE = "idle"
    RECONCILE_ACTIVE_TURN = "reconcile_active_turn"
    RESUME_PUBLICATION = "resume_publication"
    RESUME_PREPARATION = "resume_preparation"
    START_CLAIMED_TURN = "start_claimed_turn"
    START_AUTONOMOUS_TURN = "start_autonomous_turn"
    START_FRESH_FINAL_AUDIT = "start_fresh_final_audit"
    RECOVER_ORPHAN_CLAIM = "recover_orphan_claim"
    SYNC_TRACKER_STATE = "sync_tracker_state"
    COMPLETE_MERGED_WORK_ITEM = "complete_merged_work_item"
    RECORD_WORK_ITEM_DISPOSITION = "record_work_item_disposition"
    ARCHIVE_DISPOSED_WORK_ITEM = "archive_disposed_work_item"
    ARCHIVE_COMPLETED_WORK_ITEM = "archive_completed_work_item"
    RECONCILE_WORK_ITEM_ARCHIVE = "reconcile_work_item_archive"
    DELETE_TERMINAL_BRANCH = "delete_terminal_branch"
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
    disposition_kind: WorkItemDispositionKind | None = None
    disposition_pr_number: int | None = None
    disposition_requested_by: str | None = None
    disposition_request_event_id: str | None = None
    disposition_requested_at: str | None = None
    branch_cleanup_eligible_at: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalBranchCleanupFixtureTarget:
    """Exact WorkItem capability for the guarded zero-retention Fixture canary."""

    work_item_id: str

    def __post_init__(self) -> None:
        validate_work_item_id(self.work_item_id)


def plan_ssh_recovery(
    config: Config,
    store: StateStore,
    tracker: Tracker,
    *,
    now: datetime | None = None,
    audit_terminal: bool = True,
    terminal_branch_cleanup_fixture_target: TerminalBranchCleanupFixtureTarget
    | None = None,
) -> SshRecoveryPlan:
    """Return the only safe next recovery action using provider reads only."""
    if type(audit_terminal) is not bool:
        raise TypeError("audit_terminal must be a bool")
    if terminal_branch_cleanup_fixture_target is not None and not isinstance(
        terminal_branch_cleanup_fixture_target,
        TerminalBranchCleanupFixtureTarget,
    ):
        raise TypeError(
            "terminal_branch_cleanup_fixture_target must be an exact Fixture target or None"
        )
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

    disposition = _plan_work_item_disposition(
        config, store, tracker, configured, audit_terminal=audit_terminal
    )
    if disposition is not None:
        return disposition

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
        and store.get_work_item_disposition(item.work_item_id) is None
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
        followup = store.get_planned_work_item_followup(work_item.work_item_id)
        if followup is not None:
            source_turn = store.get_turn(followup.source_turn_id)
            expected_head = work_item.last_published_sha or work_item.base_sha
            if (
                work_item.state is not WorkItemState.READY
                or task.state is not TaskState.RUNNING
                or source_turn is None
                or source_turn.state
                not in {TurnState.FINISHED, TurnState.BLOCKED}
                or followup.head_sha != expected_head
            ):
                return _blocked(
                    "planned_followup_state_conflict",
                    task=task,
                    work_item=work_item,
                    turn=source_turn,
                )
            return SshRecoveryPlan(
                SshRecoveryAction.START_AUTONOMOUS_TURN,
                task,
                work_item,
                source_turn,
            )
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
        config,
        store,
        tracker,
        configured,
        observed_at=observed_at,
        audit_terminal=audit_terminal,
    )
    if completion is not None:
        return completion

    branch_cleanup = _plan_terminal_branch_cleanup(
        config,
        store,
        tracker,
        configured,
        observed_at=observed_at,
        fixture_target=terminal_branch_cleanup_fixture_target,
    )
    if branch_cleanup is not None:
        return branch_cleanup

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


def _plan_work_item_disposition(
    config: Config,
    store: StateStore,
    tracker: Tracker,
    configured: dict[str, object],
    *,
    audit_terminal: bool,
) -> SshRecoveryPlan | None:
    for disposition in store.list_work_item_dispositions():
        work_item = store.get_work_item(disposition.work_item_id)
        if work_item is None:
            return _blocked("disposed_work_item_missing")
        if not audit_terminal and _has_terminal_runner_evidence(store, work_item):
            continue
        task = tracker.get_task(work_item.repository, str(work_item.issue_number))
        error = _binding_error(task, work_item, configured)
        if error is not None:
            return _blocked(error, task=task, work_item=work_item)
        assert task is not None
        repository = configured[work_item.repository]
        if (
            task.state is not TaskState.DISCARD
            or task.state_approved_by not in repository.maintainers
            or task.state_approval_event_id is None
            or task.state_approved_at is None
        ):
            return _blocked(
                "disposed_work_item_tracker_state_conflict",
                task=task,
                work_item=work_item,
            )
        pull_request = tracker.find_pr_by_branch(
            work_item.repository, work_item.task_branch
        )
        disposition_error = _disposed_pull_request_error(
            disposition, work_item, pull_request
        )
        if disposition_error is not None:
            return _blocked(
                disposition_error,
                task=task,
                work_item=work_item,
                pull_request=pull_request,
            )
        archive = _plan_disposed_archive(store, task, work_item, disposition)
        if archive is not None:
            return archive

    undisposed = {
        item.work_item_id: item
        for item in store.list_work_items(include_completed=False)
        if item.state is not WorkItemState.RUNNING
        and store.get_work_item_disposition(item.work_item_id) is None
    }
    if not undisposed:
        return None
    for repository in config.repositories:
        for task in tracker.list_open_tasks(repository.slug, TaskState.DISCARD):
            executor_labels = tuple(
                label for label in task.labels if label.startswith("exec:")
            )
            if executor_labels != (SSH_CLI_EXECUTOR_LABEL,):
                continue
            work_item = store.get_work_item_by_issue(
                task.repository, task.issue_number
            )
            if work_item is None or work_item.work_item_id not in undisposed:
                continue
            error = _binding_error(task, work_item, configured)
            if error is not None:
                return _blocked(error, task=task, work_item=work_item)
            if task.state_approved_by not in repository.maintainers:
                return _blocked(
                    "work_item_disposition_approval_untrusted",
                    task=task,
                    work_item=work_item,
                )
            if (
                task.state_approval_event_id is None
                or task.state_approved_at is None
            ):
                return _blocked(
                    "work_item_disposition_evidence_incomplete",
                    task=task,
                    work_item=work_item,
                )
            if work_item.state in {WorkItemState.COMPLETED, WorkItemState.RUNNING}:
                return _blocked(
                    "work_item_disposition_state_conflict",
                    task=task,
                    work_item=work_item,
                )
            expected_head_sha = work_item.last_published_sha or work_item.base_sha
            pull_request = tracker.find_pr_by_branch(
                work_item.repository, work_item.task_branch
            )
            if pull_request is None:
                if work_item.pr_number is not None:
                    return _blocked(
                        "work_item_disposition_pull_request_missing",
                        task=task,
                        work_item=work_item,
                    )
                kind = WorkItemDispositionKind.ABANDONED
                pr_number = None
            else:
                if (
                    (work_item.pr_number is not None
                     and pull_request.number != work_item.pr_number)
                    or pull_request.url
                    != f"https://github.com/{work_item.repository}/pull/{pull_request.number}"
                    or pull_request.branch_name != work_item.task_branch
                    or pull_request.base_branch != work_item.base_branch
                    or pull_request.is_cross_repository
                    or pull_request.head_sha != expected_head_sha
                ):
                    return _blocked(
                        "work_item_disposition_pull_request_conflict",
                        task=task,
                        work_item=work_item,
                        pull_request=pull_request,
                    )
                if pull_request.state is PullRequestState.MERGED:
                    return _blocked(
                        "merged_pull_request_requires_completion",
                        task=task,
                        work_item=work_item,
                        pull_request=pull_request,
                    )
                kind = WorkItemDispositionKind.SUPERSEDED
                pr_number = pull_request.number
            return SshRecoveryPlan(
                SshRecoveryAction.RECORD_WORK_ITEM_DISPOSITION,
                task=task,
                work_item=work_item,
                disposition_kind=kind,
                disposition_pr_number=pr_number,
                disposition_requested_by=task.state_approved_by,
                disposition_request_event_id=task.state_approval_event_id,
                disposition_requested_at=task.state_approved_at,
                pull_request=pull_request,
            )
    return None


def _disposed_pull_request_error(
    disposition: WorkItemDisposition,
    work_item: WorkItem,
    pull_request: PullRequest | None,
) -> str | None:
    """Revalidate mutable GitHub PR state before irreversible Runner reclaim."""
    if disposition.kind is WorkItemDispositionKind.ABANDONED:
        return (
            None
            if pull_request is None
            else "disposed_abandoned_pull_request_appeared"
        )
    if pull_request is None:
        return "disposed_superseded_pull_request_missing"
    if (
        pull_request.number != disposition.pr_number
        or pull_request.url
        != f"https://github.com/{work_item.repository}/pull/{pull_request.number}"
        or pull_request.branch_name != work_item.task_branch
        or pull_request.base_branch != work_item.base_branch
        or pull_request.is_cross_repository
        or pull_request.head_sha != disposition.expected_head_sha
    ):
        return "disposed_superseded_pull_request_conflict"
    if pull_request.state is PullRequestState.MERGED:
        return "disposed_pull_request_merged_after_authorization"
    return None


def _plan_disposed_archive(
    store: StateStore,
    task: TrackerTask,
    work_item: WorkItem,
    disposition: WorkItemDisposition,
) -> SshRecoveryPlan | None:
    expected_head_sha = disposition.expected_head_sha
    request = RunnerRequest(
        RunnerOperation.ARCHIVE,
        work_item.work_item_id,
        version=NEXT_PROTOCOL_VERSION,
        expected_head_sha=expected_head_sha,
    )
    request_sha256 = sha256(request.to_json().encode("utf-8")).hexdigest()
    absence = store.get_work_item_absence_reconciliation(work_item.work_item_id)
    if absence is not None:
        if absence.expected_head_sha != expected_head_sha:
            return _blocked(
                "work_item_absence_reconciliation_identity_conflict",
                task=task,
                work_item=work_item,
            )
        return None
    record = store.get_work_item_archive(work_item.work_item_id)
    if record is not None:
        if (
            record.expected_head_sha != expected_head_sha
            or record.request_sha256 != request_sha256
        ):
            return _blocked(
                "work_item_archive_identity_conflict",
                task=task,
                work_item=work_item,
            )
        if record.status is WorkItemArchiveStatus.ARCHIVED:
            return None
        if record.status is WorkItemArchiveStatus.BLOCKED:
            return _blocked(
                "work_item_archive_blocked", task=task, work_item=work_item
            )
        action = (
            SshRecoveryAction.RECONCILE_WORK_ITEM_ARCHIVE
            if record.status is WorkItemArchiveStatus.AMBIGUOUS
            else SshRecoveryAction.ARCHIVE_DISPOSED_WORK_ITEM
        )
        return SshRecoveryPlan(
            action,
            task=task,
            work_item=work_item,
            archive_eligible_at=record.eligible_at,
        )
    return SshRecoveryPlan(
        SshRecoveryAction.ARCHIVE_DISPOSED_WORK_ITEM,
        task=task,
        work_item=work_item,
        archive_eligible_at=disposition.eligible_at,
    )


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
        if store.get_work_item_disposition(work_item.work_item_id) is not None:
            continue
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
            or generation.role
            not in {
                SessionGenerationRole.IMPLEMENTATION,
                SessionGenerationRole.CI_REPAIR,
            }
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
    audit_terminal: bool,
) -> SshRecoveryPlan | None:
    for work_item in store.list_work_items():
        if store.get_work_item_disposition(work_item.work_item_id) is not None:
            continue
        if work_item.state not in {WorkItemState.REVIEW, WorkItemState.COMPLETED}:
            continue
        if (
            not audit_terminal
            and work_item.state is WorkItemState.COMPLETED
            and _has_terminal_runner_evidence(store, work_item)
        ):
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


def _has_terminal_runner_evidence(store: StateStore, work_item: WorkItem) -> bool:
    if store.get_work_item_absence_reconciliation(work_item.work_item_id) is not None:
        return True
    archive = store.get_work_item_archive(work_item.work_item_id)
    return archive is not None and archive.status is WorkItemArchiveStatus.ARCHIVED


def _plan_terminal_branch_cleanup(
    config: Config,
    store: StateStore,
    tracker: Tracker,
    configured: dict[str, object],
    *,
    observed_at: datetime,
    fixture_target: TerminalBranchCleanupFixtureTarget | None = None,
) -> SshRecoveryPlan | None:
    runtime = config.ssh_runtime
    retention_seconds = (
        0
        if fixture_target is not None
        else None
        if runtime is None
        else getattr(runtime, "terminal_branch_retention_seconds", None)
    )
    if retention_seconds is None:
        return None
    for work_item in store.list_work_items():
        if (
            fixture_target is not None
            and work_item.work_item_id != fixture_target.work_item_id
        ):
            continue
        record = store.get_terminal_branch_cleanup(work_item.work_item_id)
        if record is not None:
            if record.state is TerminalBranchCleanupState.COMPLETED:
                continue
            if record.state is TerminalBranchCleanupState.BLOCKED:
                return _blocked(
                    "terminal_branch_cleanup_blocked", work_item=work_item
                )
        disposition = store.get_work_item_disposition(work_item.work_item_id)
        expected_head_sha = (
            disposition.expected_head_sha
            if disposition is not None
            else work_item.last_published_sha
            if work_item.state is WorkItemState.COMPLETED
            else None
        )
        if expected_head_sha is None:
            continue
        terminal_at = _terminal_runner_evidence_at(store, work_item)
        if terminal_at is None:
            continue
        eligible = terminal_at + timedelta(
            seconds=retention_seconds
        )
        eligible_at = eligible.isoformat()
        if observed_at < eligible:
            continue
        task = tracker.get_task(work_item.repository, str(work_item.issue_number))
        error = _binding_error(task, work_item, configured)
        if error is not None:
            return _blocked(error, task=task, work_item=work_item)
        assert task is not None
        if not task.is_open:
            return _blocked(
                "terminal_issue_must_remain_open", task=task, work_item=work_item
            )
        pull_request = tracker.find_pr_by_branch(
            work_item.repository, work_item.task_branch
        )
        if disposition is not None:
            if task.state is not TaskState.DISCARD:
                return _blocked(
                    "terminal_disposition_issue_state_conflict",
                    task=task,
                    work_item=work_item,
                    pull_request=pull_request,
                )
            pr_error = _disposed_pull_request_error(
                disposition, work_item, pull_request
            )
            if pr_error is not None:
                return _blocked(
                    pr_error,
                    task=task,
                    work_item=work_item,
                    pull_request=pull_request,
                )
            if (
                disposition.kind is WorkItemDispositionKind.SUPERSEDED
                and pull_request is not None
                and pull_request.state is not PullRequestState.CLOSED
            ):
                return _blocked(
                    "terminal_disposition_pull_request_not_closed",
                    task=task,
                    work_item=work_item,
                    pull_request=pull_request,
                )
        else:
            if task.state is not TaskState.COMPLETED:
                return _blocked(
                    "terminal_completed_issue_state_conflict",
                    task=task,
                    work_item=work_item,
                    pull_request=pull_request,
                )
            pr_error = _completion_pull_request_error(pull_request, work_item)
            if pr_error is not None:
                return _blocked(
                    pr_error,
                    task=task,
                    work_item=work_item,
                    pull_request=pull_request,
                )
            if pull_request is None or pull_request.state is not PullRequestState.MERGED:
                return _blocked(
                    "terminal_completed_pull_request_not_merged",
                    task=task,
                    work_item=work_item,
                    pull_request=pull_request,
                )
        request_sha256 = terminal_branch_request_sha256(
            work_item_id=work_item.work_item_id,
            repository=work_item.repository,
            branch_name=work_item.task_branch,
            expected_head_sha=expected_head_sha,
        )
        if record is not None and (
            record.repository != work_item.repository
            or record.branch_name != work_item.task_branch
            or record.expected_head_sha != expected_head_sha
            or record.eligible_at != eligible_at
            or record.request_sha256 != request_sha256
        ):
            return _blocked(
                "terminal_branch_cleanup_identity_conflict",
                task=task,
                work_item=work_item,
                pull_request=pull_request,
            )
        branch_head = tracker.get_branch_head(
            work_item.repository, work_item.task_branch
        )
        if branch_head is not None and branch_head != expected_head_sha:
            return _blocked(
                "terminal_branch_head_conflict",
                task=task,
                work_item=work_item,
                pull_request=pull_request,
            )
        return SshRecoveryPlan(
            SshRecoveryAction.DELETE_TERMINAL_BRANCH,
            task=task,
            work_item=work_item,
            pull_request=pull_request,
            branch_cleanup_eligible_at=eligible_at,
        )
    return None


def _terminal_runner_evidence_at(
    store: StateStore, work_item: WorkItem
) -> datetime | None:
    absence = store.get_work_item_absence_reconciliation(work_item.work_item_id)
    value: str | None = absence.observed_at if absence is not None else None
    if value is None:
        archive = store.get_work_item_archive(work_item.work_item_id)
        if archive is None or archive.status is not WorkItemArchiveStatus.ARCHIVED:
            return None
        value = archive.runner_archived_at
    try:
        parsed = datetime.fromisoformat(value) if value is not None else None
    except ValueError:
        return None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


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
    absence = store.get_work_item_absence_reconciliation(work_item.work_item_id)
    if absence is not None:
        if absence.expected_head_sha != work_item.last_published_sha:
            return _blocked(
                "work_item_absence_reconciliation_identity_conflict",
                task=task,
                work_item=work_item,
                pull_request=pull_request,
            )
        return None
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
