"""Single-process Control Host sweep for the persistent SSH CLI workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Protocol

from codex_dispatcher.config import Config, RepositoryConfig
from codex_dispatcher.dispatcher_lock import DispatcherProcessLock
from codex_dispatcher.github_delivery import GitHubDeliveryCoordinator
from codex_dispatcher.slack_delivery import SlackDeliveryCoordinator
from codex_dispatcher.source_bundle import SourceBundle
from codex_dispatcher.ssh_dispatch_service import (
    AutonomyBudgetError,
    CheckpointPublicationInterrupted,
    FinalAuditPreparationError,
    OfflineSshDispatchService,
)
from codex_dispatcher.ssh_recovery import (
    SshRecoveryAction,
    SshRecoveryPlan,
    TerminalBranchCleanupFixtureTarget,
    plan_ssh_recovery,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.terminal_retention import (
    TerminalBranchCleanupOutcome,
    TerminalBranchCleanupState,
    terminal_branch_request_sha256,
)
from codex_dispatcher.trackers.base import (
    PullRequest,
    TaskState,
    Tracker,
    TrackerComment,
    TrackerTask,
)
from codex_dispatcher.trackers.github_cli import GitHubApiMetrics
from codex_dispatcher.turn_orchestration import TaskBranchPublisher, TurnProgress
from codex_dispatcher.work_items import Turn, TurnState, WorkItem, WorkItemState
from codex_dispatcher.work_item_lifecycle import WorkItemArchiveStatus


class SourceSnapshotProvider(Protocol):
    """Provide exact source bundles from a trusted, independently refreshed mirror."""

    def current(self, repository: str, base_branch: str) -> SourceBundle: ...

    def exact(self, repository: str, base_sha: str) -> SourceBundle: ...


class ClaimAcquiredHook(Protocol):
    """Observe an exact successful claim before local WorkItem persistence."""

    def __call__(self, task: TrackerTask) -> None: ...


class CompletionCandidateHook(Protocol):
    """Observe an exact merged completion candidate before local mutation."""

    def __call__(
        self,
        task: TrackerTask,
        work_item: WorkItem,
        pull_request: PullRequest,
    ) -> None: ...


class ControlSweepStatus(StrEnum):
    IDLE = "idle"
    CLAIM_NOT_ACQUIRED = "claim_not_acquired"
    RETRY = "retry"
    RUNNER_ACTIVE = "runner_active"
    AWAITING_PUBLICATION = "awaiting_publication"
    AWAITING_COMPLETION = "awaiting_completion"
    REVIEW = "review"
    NEEDS_INPUT = "needs_input"
    BLOCKED = "blocked"
    STATE_SYNCHRONIZED = "state_synchronized"
    COMPLETED = "completed"
    AWAITING_ARCHIVE = "awaiting_archive"
    ARCHIVED = "archived"
    DISPOSITION_RECORDED = "disposition_recorded"
    BRANCH_CLEANED = "branch_cleaned"


@dataclass(frozen=True, slots=True)
class ControlSweepResult:
    status: ControlSweepStatus
    repository: str | None = None
    issue_number: int | None = None
    work_item_id: str | None = None
    turn_id: str | None = None
    reason: str | None = None
    github_api: GitHubApiMetrics | None = None


class SshControlSweep:
    """Perform exactly one locked recovery-or-dispatch action."""

    def __init__(
        self,
        *,
        config: Config,
        store: StateStore,
        tracker: Tracker,
        dispatch: OfflineSshDispatchService,
        source: SourceSnapshotProvider,
        process_lock: DispatcherProcessLock,
        publisher: TaskBranchPublisher | None = None,
        delivery: GitHubDeliveryCoordinator | None = None,
        slack_delivery: SlackDeliveryCoordinator | None = None,
        claim_acquired_hook: ClaimAcquiredHook | None = None,
        completion_candidate_hook: CompletionCandidateHook | None = None,
        claimant: str = "codex-dispatcher",
        runner_root: str = "/srv/codex-runner/work-items",
        terminal_branch_cleanup_fixture_target: TerminalBranchCleanupFixtureTarget
        | None = None,
    ) -> None:
        if not isinstance(config, Config):
            raise TypeError("config must be a Config")
        if (
            not isinstance(claimant, str)
            or not claimant
            or len(claimant) > 128
            or "\x00" in claimant
        ):
            raise ValueError("claimant must be non-empty bounded text")
        if terminal_branch_cleanup_fixture_target is not None and not isinstance(
            terminal_branch_cleanup_fixture_target,
            TerminalBranchCleanupFixtureTarget,
        ):
            raise TypeError(
                "terminal_branch_cleanup_fixture_target must be an exact Fixture target or None"
            )
        self._config = config
        self._store = store
        self._tracker = tracker
        self._dispatch = dispatch
        self._source = source
        self._lock = process_lock
        self._publisher = publisher
        self._delivery = delivery
        self._slack_delivery = slack_delivery
        self._claim_acquired_hook = claim_acquired_hook
        self._completion_candidate_hook = completion_candidate_hook
        self._claimant = claimant
        self._runner_root = runner_root
        self._repositories = {item.slug: item for item in config.repositories}
        self._audit_terminal = True
        self._terminal_branch_cleanup_fixture_target = (
            terminal_branch_cleanup_fixture_target
        )

    def run_once(self, *, turn_id: str | None = None) -> ControlSweepResult:
        """Run recovery first and claim at most one new Issue when state is idle."""
        if self._terminal_branch_cleanup_fixture_target is not None:
            raise RuntimeError(
                "exact terminal branch cleanup Fixture requires its dedicated entry point"
            )
        with self._lock:
            now = datetime.now(timezone.utc)
            self._audit_terminal = self._terminal_full_audit_due(now)
            recovery = self._plan_recovery(now=now)
            if recovery.action is not SshRecoveryAction.IDLE:
                return self._handle_recovery(recovery, turn_id=turn_id)
            if self._audit_terminal:
                self._store.record_sweep_cursor(
                    "terminal_github_audit", completed_at=now.isoformat()
                )

            candidates = self._dispatch.plan_candidates(self._tracker)
            if not candidates.selected:
                return ControlSweepResult(ControlSweepStatus.IDLE)
            task = candidates.selected[0]
            repository = self._repository(task.repository)
            existing = self._store.get_work_item_by_issue(
                task.repository, task.issue_number
            )
            if existing is not None and existing.state is WorkItemState.COMPLETED:
                return _task_result(
                    ControlSweepStatus.BLOCKED,
                    task,
                    work_item=existing,
                    reason="completed_work_item_cannot_be_reactivated",
                )
            preparation_retry = (
                existing is not None
                and existing.state in {WorkItemState.BLOCKED, WorkItemState.PAUSED}
                and not self._store.runner_preparation_was_acknowledged(
                    existing.work_item_id
                )
            )
            if existing is None:
                source_bundle = self._source.current(
                    task.repository, repository.base_branch
                )
            elif preparation_retry:
                source_bundle = self._source.exact(
                    existing.repository, existing.base_sha
                )
            else:
                source_bundle = None
            claimed = self._tracker.claim(
                task.repository,
                task.task_id,
                self._claimant,
                approved_by=repository.maintainers,
            )
            if not claimed.claimed:
                return _task_result(
                    ControlSweepStatus.CLAIM_NOT_ACQUIRED,
                    task,
                    reason=claimed.reason or "claim_not_acquired",
                )
            if claimed.task is None:
                return _task_result(
                    ControlSweepStatus.RETRY,
                    task,
                    reason="claim_result_missing_task",
                )
            if self._claim_acquired_hook is not None:
                self._claim_acquired_hook(claimed.task)
            if existing is None:
                assert source_bundle is not None
                base_sha = source_bundle.base_sha
            else:
                base_sha = existing.base_sha
            return self._prepare_and_run(
                claimed.task,
                base_sha=base_sha,
                source_bundle=source_bundle,
                turn_id=turn_id,
            )

    def run_terminal_branch_cleanup_fixture_once(self) -> ControlSweepResult:
        """Run only one exact guarded Fixture cleanup after normal recovery is idle."""
        target = self._terminal_branch_cleanup_fixture_target
        if target is None:
            raise RuntimeError("exact terminal branch cleanup Fixture target is missing")
        with self._lock:
            now = datetime.now(timezone.utc)
            ordinary = plan_ssh_recovery(
                self._config,
                self._store,
                self._tracker,
                now=now,
                audit_terminal=True,
            )
            if ordinary.action is not SshRecoveryAction.IDLE:
                raise RuntimeError(
                    "normal recovery must be idle before the exact cleanup Fixture"
                )
            self._audit_terminal = True
            recovery = self._plan_recovery(now=now)
            if (
                recovery.action is not SshRecoveryAction.DELETE_TERMINAL_BRANCH
                or recovery.work_item is None
                or recovery.work_item.work_item_id != target.work_item_id
            ):
                raise RuntimeError(
                    recovery.reason
                    or "exact terminal branch cleanup Fixture target is not eligible"
                )
            return self._handle_recovery(recovery, turn_id=None)

    def _plan_recovery(self, *, now: datetime | None = None) -> SshRecoveryPlan:
        return plan_ssh_recovery(
            self._config,
            self._store,
            self._tracker,
            now=now,
            audit_terminal=self._audit_terminal,
            terminal_branch_cleanup_fixture_target=(
                self._terminal_branch_cleanup_fixture_target
            ),
        )

    def _terminal_full_audit_due(self, now: datetime) -> bool:
        cursor = self._store.get_sweep_cursor("terminal_github_audit")
        if cursor is None:
            return True
        try:
            completed_at = datetime.fromisoformat(cursor)
        except ValueError:
            return True
        if completed_at.tzinfo is None or completed_at.utcoffset() is None:
            return True
        interval = timedelta(
            seconds=self._config.scheduler.terminal_full_scan_interval_seconds
        )
        return now >= completed_at.astimezone(timezone.utc) + interval

    def collect_github_api_metrics(self) -> GitHubApiMetrics | None:
        collect = getattr(self._tracker, "collect_api_metrics", None)
        return collect() if callable(collect) else None

    def _handle_recovery(
        self,
        recovery: SshRecoveryPlan,
        *,
        turn_id: str | None,
    ) -> ControlSweepResult:
        if recovery.action is SshRecoveryAction.BLOCK:
            if recovery.work_item is not None and recovery.reason in {
                "terminal_branch_head_conflict",
                "terminal_branch_cleanup_identity_conflict",
            }:
                cleanup = self._store.get_terminal_branch_cleanup(
                    recovery.work_item.work_item_id
                )
                if (
                    cleanup is not None
                    and cleanup.state is TerminalBranchCleanupState.PREPARED
                ):
                    self._store.block_terminal_branch_cleanup(
                        recovery.work_item.work_item_id,
                        error_code=recovery.reason,
                    )
            return _plan_result(
                ControlSweepStatus.BLOCKED,
                recovery,
                reason=recovery.reason or "recovery_blocked",
            )
        if recovery.action is SshRecoveryAction.COMPLETE_MERGED_WORK_ITEM:
            assert recovery.task is not None
            assert recovery.work_item is not None
            assert recovery.pull_request is not None
            if self._completion_candidate_hook is not None:
                self._completion_candidate_hook(
                    recovery.task,
                    recovery.work_item,
                    recovery.pull_request,
                )
            work_item = self._store.update_work_item_state(
                recovery.work_item.work_item_id,
                WorkItemState.COMPLETED,
            )
            self._deliver_completed(
                recovery.task,
                work_item=work_item,
                pull_request=recovery.pull_request,
            )
            updated = self._set_task_state(recovery.task, TaskState.COMPLETED)
            return _task_result(
                ControlSweepStatus.COMPLETED,
                updated,
                work_item=work_item,
            )
        if recovery.action is SshRecoveryAction.RECORD_WORK_ITEM_DISPOSITION:
            assert recovery.task is not None
            assert recovery.work_item is not None
            assert recovery.disposition_kind is not None
            assert recovery.disposition_requested_by is not None
            assert recovery.disposition_request_event_id is not None
            assert recovery.disposition_requested_at is not None
            expected_head_sha = (
                recovery.work_item.last_published_sha or recovery.work_item.base_sha
            )
            self._store.record_work_item_disposition(
                recovery.work_item.work_item_id,
                kind=recovery.disposition_kind,
                expected_head_sha=expected_head_sha,
                pr_number=recovery.disposition_pr_number,
                requested_by=recovery.disposition_requested_by,
                request_event_id=recovery.disposition_request_event_id,
                requested_at=recovery.disposition_requested_at,
                reason_code="operator_agent_discard",
            )
            return _plan_result(
                ControlSweepStatus.DISPOSITION_RECORDED,
                recovery,
            )
        if recovery.action is SshRecoveryAction.ARCHIVE_COMPLETED_WORK_ITEM:
            assert recovery.work_item is not None
            assert recovery.archive_eligible_at is not None
            archive = self._dispatch.archive_completed_work_item(
                recovery.work_item.work_item_id,
                eligible_at=recovery.archive_eligible_at,
            )
            status = (
                ControlSweepStatus.ARCHIVED
                if archive.status is WorkItemArchiveStatus.ARCHIVED
                else ControlSweepStatus.BLOCKED
                if archive.status is WorkItemArchiveStatus.BLOCKED
                else ControlSweepStatus.AWAITING_ARCHIVE
            )
            return _plan_result(
                status,
                recovery,
                reason=archive.error_code,
            )
        if recovery.action is SshRecoveryAction.DELETE_TERMINAL_BRANCH:
            assert recovery.work_item is not None
            assert recovery.branch_cleanup_eligible_at is not None
            rechecked = self._plan_recovery()
            if (
                rechecked.action is not recovery.action
                or rechecked.work_item is None
                or rechecked.work_item.work_item_id != recovery.work_item.work_item_id
            ):
                return _plan_result(
                    ControlSweepStatus.BLOCKED,
                    rechecked,
                    reason=rechecked.reason or "terminal_branch_cleanup_candidate_changed",
                )
            disposition = self._store.get_work_item_disposition(
                recovery.work_item.work_item_id
            )
            expected_head_sha = (
                disposition.expected_head_sha
                if disposition is not None
                else recovery.work_item.last_published_sha
            )
            assert expected_head_sha is not None
            request_sha256 = terminal_branch_request_sha256(
                work_item_id=recovery.work_item.work_item_id,
                repository=recovery.work_item.repository,
                branch_name=recovery.work_item.task_branch,
                expected_head_sha=expected_head_sha,
            )
            previous = self._store.get_terminal_branch_cleanup(
                recovery.work_item.work_item_id
            )
            cleanup = self._store.prepare_terminal_branch_cleanup(
                recovery.work_item.work_item_id,
                expected_head_sha=expected_head_sha,
                eligible_at=recovery.branch_cleanup_eligible_at,
                request_sha256=request_sha256,
            )
            branch_head = self._tracker.get_branch_head(
                cleanup.repository, cleanup.branch_name
            )
            if branch_head is None:
                outcome = (
                    TerminalBranchCleanupOutcome.RECONCILED_ABSENT
                    if previous is not None
                    else TerminalBranchCleanupOutcome.ALREADY_ABSENT
                )
                self._store.complete_terminal_branch_cleanup(
                    cleanup.work_item_id, outcome=outcome
                )
                return _plan_result(
                    ControlSweepStatus.BRANCH_CLEANED,
                    recovery,
                    reason=outcome.value,
                )
            if branch_head != expected_head_sha:
                self._store.block_terminal_branch_cleanup(
                    cleanup.work_item_id,
                    error_code="terminal_branch_head_conflict",
                )
                return _plan_result(
                    ControlSweepStatus.BLOCKED,
                    recovery,
                    reason="terminal_branch_head_conflict",
                )
            self._tracker.delete_branch(
                cleanup.repository,
                cleanup.branch_name,
                expected_head_sha,
            )
            if self._tracker.get_branch_head(cleanup.repository, cleanup.branch_name) is not None:
                self._store.block_terminal_branch_cleanup(
                    cleanup.work_item_id,
                    error_code="terminal_branch_delete_unconfirmed",
                )
                return _plan_result(
                    ControlSweepStatus.BLOCKED,
                    recovery,
                    reason="terminal_branch_delete_unconfirmed",
                )
            self._store.complete_terminal_branch_cleanup(
                cleanup.work_item_id,
                outcome=TerminalBranchCleanupOutcome.DELETED,
            )
            return _plan_result(
                ControlSweepStatus.BRANCH_CLEANED,
                recovery,
                reason=TerminalBranchCleanupOutcome.DELETED.value,
            )
        if recovery.action is SshRecoveryAction.ARCHIVE_DISPOSED_WORK_ITEM:
            assert recovery.work_item is not None
            assert recovery.archive_eligible_at is not None
            rechecked = self._plan_recovery()
            if (
                rechecked.action is not recovery.action
                or rechecked.work_item is None
                or rechecked.work_item.work_item_id
                != recovery.work_item.work_item_id
            ):
                return _plan_result(
                    ControlSweepStatus.BLOCKED,
                    rechecked,
                    reason=(
                        rechecked.reason
                        or "disposed_archive_candidate_changed"
                    ),
                )
            archive = self._dispatch.archive_disposed_work_item(
                recovery.work_item.work_item_id,
                eligible_at=recovery.archive_eligible_at,
            )
            status = (
                ControlSweepStatus.ARCHIVED
                if archive.status is WorkItemArchiveStatus.ARCHIVED
                else ControlSweepStatus.BLOCKED
                if archive.status is WorkItemArchiveStatus.BLOCKED
                else ControlSweepStatus.AWAITING_ARCHIVE
            )
            return _plan_result(status, recovery, reason=archive.error_code)
        if recovery.action is SshRecoveryAction.RECONCILE_WORK_ITEM_ARCHIVE:
            assert recovery.work_item is not None
            if (
                self._store.get_work_item_disposition(
                    recovery.work_item.work_item_id
                )
                is not None
            ):
                rechecked = self._plan_recovery()
                if (
                    rechecked.action is not recovery.action
                    or rechecked.work_item is None
                    or rechecked.work_item.work_item_id
                    != recovery.work_item.work_item_id
                ):
                    return _plan_result(
                        ControlSweepStatus.BLOCKED,
                        rechecked,
                        reason=(
                            rechecked.reason
                            or "disposed_archive_candidate_changed"
                        ),
                    )
            archive = self._dispatch.reconcile_work_item_archive(
                recovery.work_item.work_item_id
            )
            status = (
                ControlSweepStatus.ARCHIVED
                if archive.status is WorkItemArchiveStatus.ARCHIVED
                else ControlSweepStatus.BLOCKED
                if archive.status is WorkItemArchiveStatus.BLOCKED
                else ControlSweepStatus.AWAITING_ARCHIVE
            )
            return _plan_result(
                status,
                recovery,
                reason=archive.error_code,
            )
        if recovery.action is SshRecoveryAction.SYNC_TRACKER_STATE:
            assert recovery.task is not None
            assert recovery.desired_task_state is not None
            work_item = recovery.work_item
            if recovery.desired_task_state in {
                TaskState.REVIEW,
                TaskState.NEEDS_INPUT,
                TaskState.BLOCKED,
            }:
                assert work_item is not None
                turns = self._store.list_turns(work_item.work_item_id)
                work_item = self._deliver_terminal(
                    recovery.task,
                    work_item=work_item,
                    desired_task_state=recovery.desired_task_state,
                    turn=turns[-1] if turns else None,
                )
            elif recovery.desired_task_state is TaskState.COMPLETED:
                assert work_item is not None
                assert recovery.pull_request is not None
                self._deliver_completed(
                    recovery.task,
                    work_item=work_item,
                    pull_request=recovery.pull_request,
                )
            updated = self._set_task_state(
                recovery.task,
                recovery.desired_task_state,
            )
            return _task_result(
                ControlSweepStatus.STATE_SYNCHRONIZED,
                updated,
                work_item=work_item,
            )
        if recovery.action is SshRecoveryAction.RESUME_PUBLICATION:
            assert recovery.task is not None
            running = self._set_task_state(recovery.task, TaskState.RUNNING)
            assert recovery.turn is not None
            if (
                recovery.turn.state is TurnState.PUBLISHED
                and recovery.turn.result_status == "completed"
            ):
                assert recovery.work_item is not None
                return self._after_turn(
                    running,
                    TurnProgress(recovery.work_item, recovery.turn),
                )
            if self._publisher is None:
                return _plan_result(ControlSweepStatus.AWAITING_PUBLICATION, recovery)
            return self._publish_checkpoint(running, recovery.turn.turn_id)
        if recovery.action is SshRecoveryAction.RECONCILE_ACTIVE_TURN:
            assert recovery.task is not None and recovery.turn is not None
            progress = self._dispatch.reconcile_turn(recovery.turn.turn_id)
            return self._after_turn(recovery.task, progress)
        if recovery.action is SshRecoveryAction.START_CLAIMED_TURN:
            assert recovery.task is not None
            return self._run_claimed(recovery.task, turn_id=turn_id)
        if recovery.action is SshRecoveryAction.START_AUTONOMOUS_TURN:
            assert recovery.task is not None
            return self._run_claimed(
                recovery.task,
                turn_id=turn_id,
                allowed_states=frozenset({TaskState.RUNNING}),
            )
        if recovery.action is SshRecoveryAction.START_FRESH_FINAL_AUDIT:
            assert recovery.task is not None
            return self._run_fresh_final_audit(
                recovery.task, turn_id=turn_id
            )
        if recovery.action is SshRecoveryAction.RESUME_PREPARATION:
            assert recovery.task is not None and recovery.work_item is not None
            bundle = self._source.exact(
                recovery.work_item.repository,
                recovery.work_item.base_sha,
            )
            return self._prepare_and_run(
                recovery.task,
                base_sha=recovery.work_item.base_sha,
                source_bundle=bundle,
                turn_id=turn_id,
            )
        if recovery.action is SshRecoveryAction.RECOVER_ORPHAN_CLAIM:
            assert recovery.task is not None
            repository = self._repository(recovery.task.repository)
            bundle = self._source.current(
                recovery.task.repository,
                repository.base_branch,
            )
            return self._prepare_and_run(
                recovery.task,
                base_sha=bundle.base_sha,
                source_bundle=bundle,
                turn_id=turn_id,
            )
        raise AssertionError(f"unhandled recovery action: {recovery.action}")

    def _prepare_and_run(
        self,
        task: TrackerTask,
        *,
        base_sha: str,
        source_bundle: SourceBundle | None,
        turn_id: str | None,
    ) -> ControlSweepResult:
        work_item = self._dispatch.resolve_and_prepare(
            task,
            base_sha=base_sha,
            source_bundle=source_bundle,
            runner_root=self._runner_root,
        )
        if work_item.state is WorkItemState.PREPARING:
            return _task_result(
                ControlSweepStatus.RETRY,
                task,
                work_item=work_item,
                reason="runner_preparation_ambiguous",
            )
        if work_item.state is WorkItemState.BLOCKED:
            updated = self._set_task_state(task, TaskState.BLOCKED)
            return _task_result(
                ControlSweepStatus.BLOCKED,
                updated,
                work_item=work_item,
                reason="runner_preparation_rejected",
            )
        if work_item.state is not WorkItemState.READY:
            return _task_result(
                ControlSweepStatus.BLOCKED,
                task,
                work_item=work_item,
                reason="prepared_work_item_state_invalid",
            )
        turns = self._store.list_turns(work_item.work_item_id)
        if turns and self._dispatch.requires_fresh_final_audit(turns[-1].turn_id):
            return self._run_fresh_final_audit(task, turn_id=turn_id)
        return self._run_claimed(task, turn_id=turn_id)

    def _run_claimed(
        self,
        task: TrackerTask,
        *,
        turn_id: str | None,
        allowed_states: frozenset[TaskState] = frozenset(
            {TaskState.DISPATCHING}
        ),
    ) -> ControlSweepResult:
        stable_task, comments = self._stable_claimed_snapshot(
            task, allowed_states=allowed_states
        )
        if stable_task is None:
            return _task_result(
                ControlSweepStatus.RETRY,
                task,
                work_item=self._store.get_work_item_by_issue(
                    task.repository, task.issue_number
                ),
                reason="issue_snapshot_changed_during_freeze",
            )
        if self._slack_delivery is not None:
            work_item = self._store.get_work_item_by_issue(
                stable_task.repository,
                stable_task.issue_number,
            )
            if work_item is None:
                raise RuntimeError("claimed Issue has no persistent WorkItem")
            slack_root = self._slack_delivery.ensure_root(
                stable_task,
                work_item=work_item,
            )
            if self._delivery is not None:
                self._delivery.reconcile_execution_link(
                    stable_task,
                    work_item=slack_root.work_item,
                    slack_permalink=slack_root.root_permalink,
                )
        try:
            progress = self._dispatch.run_claimed_turn(
                stable_task,
                comments=comments,
                turn_id=turn_id,
            )
        except AutonomyBudgetError as exc:
            work_item = self._deliver_terminal(
                stable_task,
                work_item=exc.work_item,
                desired_task_state=TaskState.BLOCKED,
                turn=None,
            )
            updated = self._set_task_state(stable_task, TaskState.BLOCKED)
            return _task_result(
                ControlSweepStatus.BLOCKED,
                updated,
                work_item=work_item,
                reason=exc.error_code,
            )
        return self._after_turn(stable_task, progress)

    def _stable_claimed_snapshot(
        self,
        task: TrackerTask,
        *,
        allowed_states: frozenset[TaskState] = frozenset(
            {TaskState.DISPATCHING}
        ),
    ) -> tuple[TrackerTask | None, tuple[TrackerComment, ...]]:
        current = task
        for _ in range(2):
            comments_before = self._tracker.list_comments(
                current.repository, current.task_id
            )
            middle = self._tracker.get_task(current.repository, current.task_id)
            comments_after = self._tracker.list_comments(
                current.repository, current.task_id
            )
            latest = self._tracker.get_task(current.repository, current.task_id)
            if (
                middle is None
                or latest is None
                or not _same_issue(current, middle)
                or not _same_issue(current, latest)
            ):
                return None, ()
            if (
                middle.state not in allowed_states
                or latest.state not in allowed_states
                or not middle.is_open
                or not latest.is_open
            ):
                return None, ()
            if (
                middle == latest
                and latest.updated_at == current.updated_at
                and comments_before == comments_after
            ):
                return latest, comments_after
            current = latest
        return None, ()

    def _after_turn(
        self,
        task: TrackerTask,
        progress: TurnProgress,
    ) -> ControlSweepResult:
        if progress.turn.state is TurnState.CHECKPOINTING and self._publisher is not None:
            return self._publish_checkpoint(task, progress.turn.turn_id)
        agent_result = (
            self._store.get_turn_agent_result(progress.turn.turn_id)
            if progress.turn.state is TurnState.PUBLISHED
            else None
        )
        if (
            progress.turn.state is TurnState.PUBLISHED
            and agent_result is not None
            and agent_result.status.value == "checkpoint"
        ):
            progress = self._deliver_published_checkpoint(task, progress)
            progress = self._dispatch.plan_checkpoint_followup(
                task, progress.turn.turn_id
            )
            updated = self._set_task_state(task, TaskState.RUNNING)
            return _task_result(
                ControlSweepStatus.RETRY,
                updated,
                work_item=progress.work_item,
                turn_id=progress.turn.turn_id,
                reason="agent_checkpoint_followup_planned",
            )
        if (
            progress.turn.state is TurnState.PUBLISHED
            and progress.turn.result_status == "completed"
            and (
                agent_result is not None
                and agent_result.status.value == "completed"
            )
        ):
            progress = self._deliver_published_checkpoint(task, progress)
            progress = self._dispatch.evaluate_completion_gate(
                task, progress.turn.turn_id
            )
            if self._store.get_planned_work_item_followup(
                progress.work_item.work_item_id
            ) is not None:
                updated = self._set_task_state(task, TaskState.RUNNING)
                return _task_result(
                    ControlSweepStatus.RETRY,
                    updated,
                    work_item=progress.work_item,
                    turn_id=progress.turn.turn_id,
                    reason="ci_repair_followup_planned",
                )
        elif (
            progress.turn.state is TurnState.PUBLISHED
            and self._publisher is not None
        ):
            return self._publish_checkpoint(task, progress.turn.turn_id)
        if (
            progress.turn.state is TurnState.FINISHED
            and self._dispatch.requires_fresh_final_audit(progress.turn.turn_id)
        ):
            return self._run_fresh_final_audit(task)
        if progress.turn.state is TurnState.INTERRUPTED:
            context_failure = self._store.get_turn_context_failure(
                progress.turn.turn_id
            )
            if (
                context_failure is not None
                and progress.work_item.state is WorkItemState.READY
            ):
                updated = self._set_task_state(task, TaskState.DISPATCHING)
                return _task_result(
                    ControlSweepStatus.RETRY,
                    updated,
                    work_item=progress.work_item,
                    turn_id=progress.turn.turn_id,
                    reason="clean_context_failure_rotation_pending",
                )
        mapping = {
            TurnState.STARTING: (ControlSweepStatus.RUNNER_ACTIVE, TaskState.RUNNING),
            TurnState.RUNNING: (ControlSweepStatus.RUNNER_ACTIVE, TaskState.RUNNING),
            TurnState.RECONCILING: (ControlSweepStatus.RUNNER_ACTIVE, TaskState.RUNNING),
            TurnState.CHECKPOINTING: (
                ControlSweepStatus.AWAITING_PUBLICATION,
                TaskState.RUNNING,
            ),
            TurnState.PUBLISHED: (
                ControlSweepStatus.AWAITING_COMPLETION,
                TaskState.RUNNING,
            ),
            TurnState.FINISHED: (ControlSweepStatus.REVIEW, TaskState.REVIEW),
            TurnState.NEEDS_INPUT: (
                ControlSweepStatus.NEEDS_INPUT,
                TaskState.NEEDS_INPUT,
            ),
            TurnState.BLOCKED: (ControlSweepStatus.BLOCKED, TaskState.BLOCKED),
        }
        outcome = mapping.get(progress.turn.state)
        if outcome is None:
            return _task_result(
                ControlSweepStatus.BLOCKED,
                task,
                work_item=progress.work_item,
                turn_id=progress.turn.turn_id,
                reason="turn_state_requires_manual_recovery",
            )
        status, desired_state = outcome
        work_item = progress.work_item
        if status in {
            ControlSweepStatus.REVIEW,
            ControlSweepStatus.NEEDS_INPUT,
            ControlSweepStatus.BLOCKED,
        }:
            work_item = self._deliver_terminal(
                task,
                work_item=work_item,
                desired_task_state=desired_state,
                turn=progress.turn,
            )
        updated = self._set_task_state(task, desired_state)
        return _task_result(
            status,
            updated,
            work_item=work_item,
            turn_id=progress.turn.turn_id,
        )

    def _run_fresh_final_audit(
        self,
        task: TrackerTask,
        *,
        turn_id: str | None = None,
    ) -> ControlSweepResult:
        stable_task, comments = self._stable_claimed_snapshot(
            task,
            allowed_states=frozenset(
                {TaskState.DISPATCHING, TaskState.RUNNING}
            ),
        )
        if stable_task is None:
            work_item = self._store.get_work_item_by_issue(
                task.repository, task.issue_number
            )
            return _task_result(
                ControlSweepStatus.RETRY,
                task,
                work_item=work_item,
                reason="final_audit_issue_snapshot_changed",
            )
        try:
            progress = self._dispatch.run_fresh_final_audit(
                stable_task,
                comments=comments,
                turn_id=turn_id,
            )
        except FinalAuditPreparationError as exc:
            work_item = self._store.get_work_item_by_issue(
                stable_task.repository, stable_task.issue_number
            )
            if work_item is None:
                raise RuntimeError("final Audit lost its persistent WorkItem") from exc
            if work_item.state in {WorkItemState.REVIEW, WorkItemState.READY}:
                work_item = self._store.update_work_item_state(
                    work_item.work_item_id, WorkItemState.BLOCKED
                )
            work_item = self._deliver_terminal(
                stable_task,
                work_item=work_item,
                desired_task_state=TaskState.BLOCKED,
                turn=None,
            )
            updated = self._set_task_state(stable_task, TaskState.BLOCKED)
            return _task_result(
                ControlSweepStatus.BLOCKED,
                updated,
                work_item=work_item,
                reason=exc.error_code,
            )
        return self._after_turn(stable_task, progress)

    def _publish_checkpoint(
        self,
        task: TrackerTask,
        turn_id: str,
    ) -> ControlSweepResult:
        assert self._publisher is not None
        try:
            progress = self._dispatch.publish_checkpoint(
                turn_id,
                publisher=self._publisher,
            )
        except CheckpointPublicationInterrupted:
            running = self._set_task_state(task, TaskState.RUNNING)
            work_item = self._store.get_work_item_by_issue(
                task.repository, task.issue_number
            )
            return _task_result(
                ControlSweepStatus.AWAITING_PUBLICATION,
                running,
                work_item=work_item,
                turn_id=turn_id,
                reason="publication_outcome_ambiguous",
            )
        return self._after_turn(task, progress)

    def _set_task_state(self, task: TrackerTask, state: TaskState) -> TrackerTask:
        if task.state is state:
            return task
        return self._tracker.set_state(task.repository, task.task_id, state)

    def _deliver_terminal(
        self,
        task: TrackerTask,
        *,
        work_item: WorkItem,
        desired_task_state: TaskState,
        turn: Turn | None,
    ) -> WorkItem:
        slack_permalink: str | None = None
        if self._slack_delivery is not None:
            slack_root = self._slack_delivery.ensure_root(
                task,
                work_item=work_item,
            )
            work_item = slack_root.work_item
            slack_permalink = slack_root.root_permalink
        if self._delivery is not None:
            github = self._delivery.reconcile(
                task,
                work_item=work_item,
                desired_task_state=desired_task_state,
                slack_permalink=slack_permalink,
            )
            work_item = github.work_item
        if self._slack_delivery is not None:
            slack = self._slack_delivery.reconcile_terminal(
                task,
                work_item=work_item,
                desired_task_state=desired_task_state,
                turn=turn,
            )
            work_item = slack.work_item
        return work_item

    def _deliver_published_checkpoint(
        self,
        task: TrackerTask,
        progress: TurnProgress,
    ) -> TurnProgress:
        """Expose the exact checkpoint through a Draft PR before CI import."""
        work_item = progress.work_item
        slack_permalink: str | None = None
        if self._slack_delivery is not None:
            slack_root = self._slack_delivery.ensure_root(
                task,
                work_item=work_item,
            )
            work_item = slack_root.work_item
            slack_permalink = slack_root.root_permalink
        if self._delivery is not None:
            github = self._delivery.reconcile_published_checkpoint(
                task,
                work_item=work_item,
                slack_permalink=slack_permalink,
            )
            work_item = github.work_item
        return TurnProgress(work_item, progress.turn)

    def _deliver_completed(
        self,
        task: TrackerTask,
        *,
        work_item: WorkItem,
        pull_request: PullRequest,
    ) -> None:
        if self._delivery is not None:
            self._delivery.reconcile_completed(
                task,
                work_item=work_item,
                pull_request=pull_request,
            )

    def _repository(self, slug: str) -> RepositoryConfig:
        try:
            return self._repositories[slug]
        except KeyError as exc:
            raise ValueError("task repository is not configured") from exc


def _same_issue(first: TrackerTask, second: TrackerTask) -> bool:
    return (
        first.repository == second.repository
        and first.task_id == second.task_id
        and first.issue_number == second.issue_number
        and first.issue_node_id == second.issue_node_id
    )


def _task_result(
    status: ControlSweepStatus,
    task: TrackerTask,
    *,
    work_item: WorkItem | None = None,
    turn_id: str | None = None,
    reason: str | None = None,
) -> ControlSweepResult:
    return ControlSweepResult(
        status,
        task.repository,
        task.issue_number,
        None if work_item is None else work_item.work_item_id,
        turn_id,
        reason,
    )


def _plan_result(
    status: ControlSweepStatus,
    plan: SshRecoveryPlan,
    *,
    reason: str | None = None,
) -> ControlSweepResult:
    task = plan.task
    work_item = plan.work_item
    return ControlSweepResult(
        status,
        None if task is None else task.repository,
        None if task is None else task.issue_number,
        None if work_item is None else work_item.work_item_id,
        None if plan.turn is None else plan.turn.turn_id,
        reason,
    )
