"""Single-process Control Host sweep for the persistent SSH CLI workflow."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from codex_dispatcher.config import Config, RepositoryConfig
from codex_dispatcher.dispatcher_lock import DispatcherProcessLock
from codex_dispatcher.github_delivery import GitHubDeliveryCoordinator
from codex_dispatcher.slack_delivery import SlackDeliveryCoordinator
from codex_dispatcher.source_bundle import SourceBundle
from codex_dispatcher.ssh_dispatch_service import (
    CheckpointPublicationInterrupted,
    OfflineSshDispatchService,
)
from codex_dispatcher.ssh_recovery import (
    SshRecoveryAction,
    SshRecoveryPlan,
    plan_ssh_recovery,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.trackers.base import TaskState, Tracker, TrackerComment, TrackerTask
from codex_dispatcher.turn_orchestration import TaskBranchPublisher, TurnProgress
from codex_dispatcher.work_items import Turn, TurnState, WorkItem, WorkItemState


class SourceSnapshotProvider(Protocol):
    """Provide exact source bundles from a trusted, independently refreshed mirror."""

    def current(self, repository: str, base_branch: str) -> SourceBundle: ...

    def exact(self, repository: str, base_sha: str) -> SourceBundle: ...


class ControlSweepStatus(StrEnum):
    IDLE = "idle"
    CLAIM_NOT_ACQUIRED = "claim_not_acquired"
    RETRY = "retry"
    RUNNER_ACTIVE = "runner_active"
    AWAITING_PUBLICATION = "awaiting_publication"
    REVIEW = "review"
    NEEDS_INPUT = "needs_input"
    BLOCKED = "blocked"
    STATE_SYNCHRONIZED = "state_synchronized"


@dataclass(frozen=True, slots=True)
class ControlSweepResult:
    status: ControlSweepStatus
    repository: str | None = None
    issue_number: int | None = None
    work_item_id: str | None = None
    turn_id: str | None = None
    reason: str | None = None


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
        claimant: str = "codex-dispatcher",
        runner_root: str = "/srv/codex-runner/work-items",
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
        self._config = config
        self._store = store
        self._tracker = tracker
        self._dispatch = dispatch
        self._source = source
        self._lock = process_lock
        self._publisher = publisher
        self._delivery = delivery
        self._slack_delivery = slack_delivery
        self._claimant = claimant
        self._runner_root = runner_root
        self._repositories = {item.slug: item for item in config.repositories}

    def run_once(self, *, turn_id: str | None = None) -> ControlSweepResult:
        """Run recovery first and claim at most one new Issue when state is idle."""
        with self._lock:
            recovery = plan_ssh_recovery(self._config, self._store, self._tracker)
            if recovery.action is not SshRecoveryAction.IDLE:
                return self._handle_recovery(recovery, turn_id=turn_id)

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
            source_bundle = (
                None
                if existing is not None
                else self._source.current(task.repository, repository.base_branch)
            )
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

    def _handle_recovery(
        self,
        recovery: SshRecoveryPlan,
        *,
        turn_id: str | None,
    ) -> ControlSweepResult:
        if recovery.action is SshRecoveryAction.BLOCK:
            return _plan_result(
                ControlSweepStatus.BLOCKED,
                recovery,
                reason=recovery.reason or "recovery_blocked",
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
            if self._publisher is None:
                return _plan_result(ControlSweepStatus.AWAITING_PUBLICATION, recovery)
            assert recovery.turn is not None
            return self._publish_checkpoint(running, recovery.turn.turn_id)
        if recovery.action is SshRecoveryAction.RECONCILE_ACTIVE_TURN:
            assert recovery.task is not None and recovery.turn is not None
            progress = self._dispatch.reconcile_turn(recovery.turn.turn_id)
            return self._after_turn(recovery.task, progress)
        if recovery.action is SshRecoveryAction.START_CLAIMED_TURN:
            assert recovery.task is not None
            return self._run_claimed(recovery.task, turn_id=turn_id)
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
        return self._run_claimed(task, turn_id=turn_id)

    def _run_claimed(
        self,
        task: TrackerTask,
        *,
        turn_id: str | None,
    ) -> ControlSweepResult:
        stable_task, comments = self._stable_claimed_snapshot(task)
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
        progress = self._dispatch.run_claimed_turn(
            stable_task,
            comments=comments,
            turn_id=turn_id,
        )
        return self._after_turn(stable_task, progress)

    def _stable_claimed_snapshot(
        self, task: TrackerTask
    ) -> tuple[TrackerTask | None, tuple[TrackerComment, ...]]:
        current = task
        for _ in range(2):
            comments = self._tracker.list_comments(current.repository, current.task_id)
            latest = self._tracker.get_task(current.repository, current.task_id)
            if latest is None or not _same_issue(current, latest):
                return None, ()
            if latest.state is not TaskState.DISPATCHING or not latest.is_open:
                return None, ()
            if latest.updated_at == current.updated_at:
                return latest, comments
            current = latest
        return None, ()

    def _after_turn(
        self,
        task: TrackerTask,
        progress: TurnProgress,
    ) -> ControlSweepResult:
        if (
            progress.turn.state in {TurnState.CHECKPOINTING, TurnState.PUBLISHED}
            and self._publisher is not None
        ):
            return self._publish_checkpoint(task, progress.turn.turn_id)
        mapping = {
            TurnState.STARTING: (ControlSweepStatus.RUNNER_ACTIVE, TaskState.RUNNING),
            TurnState.RUNNING: (ControlSweepStatus.RUNNER_ACTIVE, TaskState.RUNNING),
            TurnState.RECONCILING: (ControlSweepStatus.RUNNER_ACTIVE, TaskState.RUNNING),
            TurnState.CHECKPOINTING: (
                ControlSweepStatus.AWAITING_PUBLICATION,
                TaskState.RUNNING,
            ),
            TurnState.PUBLISHED: (
                ControlSweepStatus.AWAITING_PUBLICATION,
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
