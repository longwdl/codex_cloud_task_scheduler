"""Read-only recovery planning before any new GitHub Issue is claimed."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256

from codex_dispatcher.config import Config, RepositoryConfig
from codex_dispatcher.repository_admission import (
    RepositoryPolicyIdentity,
    build_repository_policy_identity,
)
from codex_dispatcher.repository_evidence import (
    RepositoryRecoveryReceipt,
    RepositoryTargetReadbackVerdict,
    evaluate_repository_recovery,
    evaluate_repository_target_readback,
)
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
    TerminalGithubClosureKind,
    TerminalGithubClosureState,
    WorkItemArchiveStatus,
    WorkItemDiscardRequest,
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
    PREPARE_WORK_ITEM_DISCARD = "prepare_work_item_discard"
    REJECT_DISCARDED_TURN_RESULT = "reject_discarded_turn_result"
    CLOSE_DISCARDED_PULL_REQUEST = "close_discarded_pull_request"
    RECORD_WORK_ITEM_DISPOSITION = "record_work_item_disposition"
    CLOSE_TERMINAL_ISSUE = "close_terminal_issue"
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
    disposition_pr_number: int | None = None
    disposition_requested_by: str | None = None
    disposition_request_event_id: str | None = None
    disposition_requested_at: str | None = None
    terminal_closure_kind: TerminalGithubClosureKind | None = None
    branch_cleanup_eligible_at: str | None = None
    repository_recovery_receipt: RepositoryRecoveryReceipt | None = None
    repository_target_readback_verdict: RepositoryTargetReadbackVerdict | None = None


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
    """Return the next recovery with executable class and exact-target evidence."""
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("now must include a timezone")
    planned = _plan_ssh_recovery_unchecked(
        config,
        store,
        tracker,
        now=observed_at,
        audit_terminal=audit_terminal,
        terminal_branch_cleanup_fixture_target=terminal_branch_cleanup_fixture_target,
    )
    return _attach_repository_evidence(config, store, planned, observed_at)


def _plan_ssh_recovery_unchecked(
    config: Config,
    store: StateStore,
    tracker: Tracker,
    *,
    now: datetime,
    audit_terminal: bool,
    terminal_branch_cleanup_fixture_target: TerminalBranchCleanupFixtureTarget
    | None,
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
        repository = configured[work_item.repository]
        discard_request = store.get_work_item_discard_request(work_item.work_item_id)
        if task.state is TaskState.DISCARD:
            if discard_request is None:
                return _plan_new_discard_request(
                    repository,
                    tracker,
                    task,
                    work_item,
                    turn=active_turn,
                )
            discard_error = _discard_request_error(
                discard_request,
                task,
                work_item,
                repository,
            )
            if discard_error is not None:
                return _blocked(
                    discard_error,
                    task=task,
                    work_item=work_item,
                    turn=active_turn,
                )
            if active_turn.state in {
                TurnState.CHECKPOINTING,
                TurnState.PUBLISHED,
            }:
                return SshRecoveryPlan(
                    SshRecoveryAction.REJECT_DISCARDED_TURN_RESULT,
                    task=task,
                    work_item=work_item,
                    turn=active_turn,
                )
            if active_turn.state is TurnState.PLANNED:
                return _blocked(
                    "discard_requested_planned_turn_not_cancelled",
                    task=task,
                    work_item=work_item,
                    turn=active_turn,
                )
            return SshRecoveryPlan(
                SshRecoveryAction.RECONCILE_ACTIVE_TURN,
                task=task,
                work_item=work_item,
                turn=active_turn,
            )
        if discard_request is not None:
            return _blocked(
                "discard_request_tracker_state_conflict",
                task=task,
                work_item=work_item,
                turn=active_turn,
            )
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

    observed_at = now
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


def _attach_repository_evidence(
    config: Config,
    store: StateStore,
    planned: SshRecoveryPlan,
    observed_at: datetime,
) -> SshRecoveryPlan:
    if planned.action is SshRecoveryAction.IDLE or planned.task is None:
        return planned
    task = planned.task
    policy = _recovery_policy(config, store, task, planned.work_item)
    created_at = observed_at.astimezone(timezone.utc).isoformat()
    receipt = evaluate_repository_recovery(
        action=planned.action.value,
        policy=policy,
        task=task,
        work_item=planned.work_item,
        turn=planned.turn,
        pull_request=planned.pull_request,
        planning_code=planned.reason,
        created_at=created_at,
    )
    verdict = None
    if planned.work_item is not None:
        verdict = evaluate_repository_target_readback(
            action=planned.action.value,
            policy=policy,
            task=task,
            work_item=planned.work_item,
            turn=planned.turn,
            pull_request=planned.pull_request,
            ledger_evidence=_repository_ledger_evidence(
                store, planned.work_item, planned.turn
            ),
            created_at=created_at,
        )
    enriched = replace(
        planned,
        repository_recovery_receipt=receipt,
        repository_target_readback_verdict=verdict,
    )
    if receipt.decision == "blocked":
        return replace(
            enriched,
            action=SshRecoveryAction.BLOCK,
            reason=receipt.code or "repository_recovery_blocked",
        )
    if verdict is not None and verdict.status == "blocked":
        return replace(
            enriched,
            action=SshRecoveryAction.BLOCK,
            reason=verdict.code or "repository_target_readback_blocked",
        )
    return enriched


def _recovery_policy(
    config: Config,
    store: StateStore,
    task: TrackerTask,
    work_item: WorkItem | None,
) -> RepositoryPolicyIdentity | None:
    if work_item is not None:
        if work_item.repository_policy_sha256 is None:
            return None
        policy = store.get_work_item_repository_policy(work_item.work_item_id)
        if policy is None or policy.policy_sha256 != work_item.repository_policy_sha256:
            return None
        return policy
    policy = store.get_repository_claim_policy(task.repository, task.issue_number)
    if policy is not None:
        return policy
    repository = next(
        (item for item in config.repositories if item.slug == task.repository), None
    )
    admission = config.repository_admission
    if repository is None or admission is None or task.issue_node_id is None:
        return None
    try:
        return build_repository_policy_identity(
            repository=task.repository,
            issue_number=task.issue_number,
            issue_node_id=task.issue_node_id,
            repository_class=repository.repository_class,
            recovery_profiles=admission.recovery_profiles,
            target_readback_profiles=admission.target_readback_profiles,
        )
    except ValueError:
        return None


def _repository_ledger_evidence(
    store: StateStore,
    work_item: WorkItem,
    turn: Turn | None,
) -> dict[str, object]:
    deliveries = tuple(
        item
        for item in store.list_slack_deliveries()
        if item.work_item_id == work_item.work_item_id
    )
    gate = None if turn is None else store.get_turn_completion_gate(turn.turn_id)
    archive = store.get_work_item_archive(work_item.work_item_id)
    absence = store.get_work_item_absence_reconciliation(work_item.work_item_id)
    discard_request = store.get_work_item_discard_request(work_item.work_item_id)
    disposition = store.get_work_item_disposition(work_item.work_item_id)
    terminal_closures = tuple(
        closure
        for closure in store.list_terminal_github_closures()
        if closure.work_item_id == work_item.work_item_id
    )
    return {
        "schema_version": 2,
        "slack_deliveries": [
            {
                "deduplication_key": item.deduplication_key,
                "work_item_id": item.work_item_id,
                "turn_id": item.turn_id,
                "kind": item.kind.value,
                "channel_id": item.channel_id,
                "payload_sha256": item.payload_sha256,
                "state": item.state.value,
                "thread_ts": item.thread_ts,
                "message_ts": item.message_ts,
                "permalink": item.permalink,
            }
            for item in deliveries
        ],
        "actions_completion_gate": (
            None
            if gate is None
            else {
                "turn_id": gate.turn_id,
                "work_item_id": gate.work_item_id,
                "status": gate.status.value,
                "head_sha": gate.head_sha,
                "task_spec_sha256": gate.task_spec_sha256,
                "evidence_sha256": gate.evidence_sha256,
            }
        ),
        "runner_terminal_storage": {
            "archive": (
                None
                if archive is None
                else {
                    "work_item_id": archive.work_item_id,
                    "status": archive.status.value,
                    "expected_head_sha": archive.expected_head_sha,
                    "request_sha256": archive.request_sha256,
                    "response_sha256": archive.response_sha256,
                }
            ),
            "absence": (
                None
                if absence is None
                else {
                    "work_item_id": absence.work_item_id,
                    "expected_head_sha": absence.expected_head_sha,
                    "evidence_sha256": absence.evidence_sha256,
                    "observed_by": absence.observed_by,
                    "observed_at": absence.observed_at,
                }
            ),
        },
        "discard_request": (
            None
            if discard_request is None
            else {
                "work_item_id": discard_request.work_item_id,
                "expected_head_sha": discard_request.expected_head_sha,
                "pr_number": discard_request.pr_number,
                "requested_by": discard_request.requested_by,
                "request_event_id": discard_request.request_event_id,
                "requested_at": discard_request.requested_at,
                "request_sha256": discard_request.request_sha256,
            }
        ),
        "disposition": (
            None
            if disposition is None
            else {
                "work_item_id": disposition.work_item_id,
                "kind": disposition.kind.value,
                "expected_head_sha": disposition.expected_head_sha,
                "pr_number": disposition.pr_number,
                "request_sha256": disposition.request_sha256,
                "reason_code": disposition.reason_code,
            }
        ),
        "terminal_github_closures": [
            {
                "kind": closure.kind.value,
                "repository": closure.repository,
                "issue_number": closure.issue_number,
                "issue_node_id": closure.issue_node_id,
                "pr_number": closure.pr_number,
                "expected_head_sha": closure.expected_head_sha,
                "close_reason": closure.close_reason,
                "state": closure.state.value,
                "request_sha256": closure.request_sha256,
                "outcome": (
                    None if closure.outcome is None else closure.outcome.value
                ),
                "error_code": closure.error_code,
            }
            for closure in terminal_closures
        ],
    }


def _plan_work_item_disposition(
    config: Config,
    store: StateStore,
    tracker: Tracker,
    configured: dict[str, object],
    *,
    audit_terminal: bool,
) -> SshRecoveryPlan | None:
    """Plan one uniform discard lifecycle from an audited maintainer label."""
    for disposition in store.list_work_item_dispositions():
        work_item = store.get_work_item(disposition.work_item_id)
        if work_item is None:
            return _blocked("disposed_work_item_missing")
        terminal_issue_closure = store.get_terminal_github_closure(
            work_item.work_item_id,
            TerminalGithubClosureKind.DISCARDED_ISSUE,
        )
        if (
            not audit_terminal
            and _has_terminal_runner_evidence(store, work_item)
            and terminal_issue_closure is not None
            and terminal_issue_closure.state
            is TerminalGithubClosureState.COMPLETED
        ):
            continue
        task = tracker.get_task(work_item.repository, str(work_item.issue_number))
        error = _binding_error(task, work_item, configured)
        if error is not None:
            return _blocked(error, task=task, work_item=work_item)
        assert task is not None
        repository = configured[work_item.repository]
        if not isinstance(repository, RepositoryConfig):
            return _blocked("persisted_repository_configuration_invalid")
        discard_request = store.get_work_item_discard_request(work_item.work_item_id)
        if discard_request is None:
            return _blocked(
                "disposed_work_item_discard_request_missing",
                task=task,
                work_item=work_item,
            )
        request_error = _discard_request_error(
            discard_request,
            task,
            work_item,
            repository,
        )
        if request_error is not None:
            return _blocked(
                request_error,
                task=task,
                work_item=work_item,
            )
        pull_request = tracker.find_pr_by_branch(
            work_item.repository, work_item.task_branch
        )
        pull_request_error = _discard_pull_request_error(
            discard_request,
            work_item,
            pull_request,
        )
        if pull_request_error is not None:
            return _blocked(
                pull_request_error,
                task=task,
                work_item=work_item,
                pull_request=pull_request,
            )
        if discard_request.pr_number is not None:
            closure_plan = _plan_discard_pull_request_closure(
                store,
                task,
                work_item,
                pull_request,
            )
            if closure_plan is not None:
                return closure_plan
        issue_plan = _plan_terminal_issue_closure(
            store,
            task,
            work_item,
            TerminalGithubClosureKind.DISCARDED_ISSUE,
        )
        if issue_plan is not None:
            return issue_plan
        archive = _plan_disposed_archive(store, task, work_item, disposition)
        if archive is not None:
            return archive

    for discard_request in store.list_work_item_discard_requests():
        if store.get_work_item_disposition(discard_request.work_item_id) is not None:
            continue
        work_item = store.get_work_item(discard_request.work_item_id)
        if work_item is None:
            return _blocked("discard_requested_work_item_missing")
        # A merge confirmed at the exact persisted head is the stronger terminal
        # fact. Keep the immutable request as audit evidence but finish completion.
        if work_item.state is WorkItemState.COMPLETED:
            continue
        task = tracker.get_task(work_item.repository, str(work_item.issue_number))
        error = _binding_error(task, work_item, configured)
        if error is not None:
            return _blocked(error, task=task, work_item=work_item)
        assert task is not None
        repository = configured[work_item.repository]
        if not isinstance(repository, RepositoryConfig):
            return _blocked("persisted_repository_configuration_invalid")
        request_error = _discard_request_error(
            discard_request,
            task,
            work_item,
            repository,
        )
        if request_error is not None:
            return _blocked(request_error, task=task, work_item=work_item)
        pull_request = tracker.find_pr_by_branch(
            work_item.repository, work_item.task_branch
        )
        pull_request_error = _discard_pull_request_error(
            discard_request,
            work_item,
            pull_request,
        )
        if pull_request_error == "merged_pull_request_requires_completion":
            if work_item.state is WorkItemState.REVIEW and pull_request is not None:
                return SshRecoveryPlan(
                    SshRecoveryAction.COMPLETE_MERGED_WORK_ITEM,
                    task=task,
                    work_item=work_item,
                    pull_request=pull_request,
                )
        if pull_request_error is not None:
            return _blocked(
                pull_request_error,
                task=task,
                work_item=work_item,
                pull_request=pull_request,
            )
        if discard_request.pr_number is not None:
            closure_plan = _plan_discard_pull_request_closure(
                store,
                task,
                work_item,
                pull_request,
            )
            if closure_plan is not None:
                return closure_plan
        return SshRecoveryPlan(
            SshRecoveryAction.RECORD_WORK_ITEM_DISPOSITION,
            task=task,
            work_item=work_item,
            disposition_pr_number=discard_request.pr_number,
            disposition_requested_by=discard_request.requested_by,
            disposition_request_event_id=discard_request.request_event_id,
            disposition_requested_at=discard_request.requested_at,
            pull_request=pull_request,
        )

    for work_item in store.list_work_items(include_completed=False):
        if store.get_work_item_disposition(work_item.work_item_id) is not None:
            continue
        task = tracker.get_task(work_item.repository, str(work_item.issue_number))
        if task is None or task.state is not TaskState.DISCARD:
            continue
        error = _binding_error(task, work_item, configured)
        if error is not None:
            return _blocked(error, task=task, work_item=work_item)
        repository = configured[work_item.repository]
        if not isinstance(repository, RepositoryConfig):
            return _blocked("persisted_repository_configuration_invalid")
        return _plan_new_discard_request(
            repository,
            tracker,
            task,
            work_item,
            turn=None,
        )
    return None


def _plan_new_discard_request(
    repository: RepositoryConfig,
    tracker: Tracker,
    task: TrackerTask,
    work_item: WorkItem,
    *,
    turn: Turn | None,
) -> SshRecoveryPlan:
    if task.state_approved_by not in repository.maintainers:
        return _blocked(
            "work_item_discard_approval_untrusted",
            task=task,
            work_item=work_item,
            turn=turn,
        )
    if task.state_approval_event_id is None or task.state_approved_at is None:
        return _blocked(
            "work_item_discard_evidence_incomplete",
            task=task,
            work_item=work_item,
            turn=turn,
        )
    if work_item.state is WorkItemState.COMPLETED:
        return _blocked(
            "completed_work_item_cannot_be_discarded",
            task=task,
            work_item=work_item,
            turn=turn,
        )
    expected_head_sha = work_item.last_published_sha or work_item.base_sha
    pull_request = tracker.find_pr_by_branch(
        work_item.repository, work_item.task_branch
    )
    if pull_request is None:
        if work_item.pr_number is not None:
            return _blocked(
                "work_item_discard_pull_request_missing",
                task=task,
                work_item=work_item,
                turn=turn,
            )
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
                "work_item_discard_pull_request_conflict",
                task=task,
                work_item=work_item,
                turn=turn,
                pull_request=pull_request,
            )
        if pull_request.state is PullRequestState.MERGED:
            if work_item.state is WorkItemState.REVIEW and turn is None:
                return SshRecoveryPlan(
                    SshRecoveryAction.COMPLETE_MERGED_WORK_ITEM,
                    task=task,
                    work_item=work_item,
                    pull_request=pull_request,
                )
            return _blocked(
                "merged_pull_request_requires_completion",
                task=task,
                work_item=work_item,
                turn=turn,
                pull_request=pull_request,
            )
        pr_number = pull_request.number
    return SshRecoveryPlan(
        SshRecoveryAction.PREPARE_WORK_ITEM_DISCARD,
        task=task,
        work_item=work_item,
        turn=turn,
        disposition_pr_number=pr_number,
        disposition_requested_by=task.state_approved_by,
        disposition_request_event_id=task.state_approval_event_id,
        disposition_requested_at=task.state_approved_at,
        pull_request=pull_request,
    )


def _discard_request_error(
    request: WorkItemDiscardRequest,
    task: TrackerTask,
    work_item: WorkItem,
    repository: RepositoryConfig,
) -> str | None:
    if task.state is not TaskState.DISCARD:
        return "discard_request_tracker_state_conflict"
    if task.state_approved_by not in repository.maintainers:
        return "discard_request_approval_untrusted"
    if (
        task.state_approval_event_id != request.request_event_id
        or task.state_approved_at != request.requested_at
        or task.state_approved_by != request.requested_by
    ):
        return "discard_request_authorization_conflict"
    if (work_item.last_published_sha or work_item.base_sha) != request.expected_head_sha:
        return "discard_request_head_conflict"
    if work_item.state is WorkItemState.COMPLETED:
        return "completed_work_item_discard_request_conflict"
    return None


def _discard_pull_request_error(
    request: WorkItemDiscardRequest,
    work_item: WorkItem,
    pull_request: PullRequest | None,
) -> str | None:
    if request.pr_number is None:
        return (
            None
            if pull_request is None
            else "discarded_without_pr_pull_request_appeared"
        )
    if pull_request is None:
        return "discarded_pull_request_missing"
    if (
        pull_request.number != request.pr_number
        or pull_request.url
        != f"https://github.com/{work_item.repository}/pull/{request.pr_number}"
        or pull_request.branch_name != work_item.task_branch
        or pull_request.base_branch != work_item.base_branch
        or pull_request.is_cross_repository
        or pull_request.head_sha != request.expected_head_sha
    ):
        return "discarded_pull_request_conflict"
    if pull_request.state is PullRequestState.MERGED:
        return "merged_pull_request_requires_completion"
    return None


def _plan_discard_pull_request_closure(
    store: StateStore,
    task: TrackerTask,
    work_item: WorkItem,
    pull_request: PullRequest | None,
) -> SshRecoveryPlan | None:
    assert pull_request is not None
    closure = store.get_terminal_github_closure(
        work_item.work_item_id,
        TerminalGithubClosureKind.DISCARDED_PULL_REQUEST,
    )
    if closure is not None and closure.state is TerminalGithubClosureState.BLOCKED:
        return _blocked(
            closure.error_code or "discarded_pull_request_closure_blocked",
            task=task,
            work_item=work_item,
            pull_request=pull_request,
        )
    if closure is not None and closure.state is TerminalGithubClosureState.COMPLETED:
        if pull_request.state is not PullRequestState.CLOSED:
            return _blocked(
                "discarded_pull_request_close_receipt_conflict",
                task=task,
                work_item=work_item,
                pull_request=pull_request,
            )
        return None
    return SshRecoveryPlan(
        SshRecoveryAction.CLOSE_DISCARDED_PULL_REQUEST,
        task=task,
        work_item=work_item,
        pull_request=pull_request,
        terminal_closure_kind=TerminalGithubClosureKind.DISCARDED_PULL_REQUEST,
    )


def _plan_terminal_issue_closure(
    store: StateStore,
    task: TrackerTask,
    work_item: WorkItem,
    kind: TerminalGithubClosureKind,
) -> SshRecoveryPlan | None:
    closure = store.get_terminal_github_closure(work_item.work_item_id, kind)
    expected_reason = (
        "completed"
        if kind is TerminalGithubClosureKind.COMPLETED_ISSUE
        else "not_planned"
    )
    if closure is not None and closure.state is TerminalGithubClosureState.BLOCKED:
        return _blocked(
            closure.error_code or "terminal_issue_closure_blocked",
            task=task,
            work_item=work_item,
        )
    if closure is not None and closure.state is TerminalGithubClosureState.COMPLETED:
        if task.is_open or task.state_reason != expected_reason:
            return _blocked(
                "terminal_issue_close_receipt_conflict",
                task=task,
                work_item=work_item,
            )
        return None
    if not task.is_open and task.state_reason != expected_reason:
        return _blocked(
            "terminal_issue_close_reason_conflict",
            task=task,
            work_item=work_item,
        )
    return SshRecoveryPlan(
        SshRecoveryAction.CLOSE_TERMINAL_ISSUE,
        task=task,
        work_item=work_item,
        terminal_closure_kind=kind,
    )


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
        terminal_issue_closure = store.get_terminal_github_closure(
            work_item.work_item_id,
            TerminalGithubClosureKind.COMPLETED_ISSUE,
        )
        if (
            not audit_terminal
            and work_item.state is WorkItemState.COMPLETED
            and _has_terminal_runner_evidence(store, work_item)
            and terminal_issue_closure is not None
            and terminal_issue_closure.state
            is TerminalGithubClosureState.COMPLETED
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
            TaskState.DISCARD,
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
            TaskState.DISCARD,
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
            if task.state in {TaskState.REVIEW, TaskState.DISCARD}:
                return SshRecoveryPlan(
                    SshRecoveryAction.SYNC_TRACKER_STATE,
                    task=task,
                    work_item=work_item,
                    desired_task_state=TaskState.COMPLETED,
                    pull_request=pull_request,
                )
            issue_plan = _plan_terminal_issue_closure(
                store,
                task,
                work_item,
                TerminalGithubClosureKind.COMPLETED_ISSUE,
            )
            if issue_plan is not None:
                return issue_plan
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
    cutover_at = (
        None
        if fixture_target is not None or runtime is None
        else getattr(runtime, "terminal_branch_retention_cutover_at", None)
    )
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
        if cutover_at is not None and terminal_at < cutover_at:
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
        terminal_issue_kind = (
            TerminalGithubClosureKind.DISCARDED_ISSUE
            if disposition is not None
            else TerminalGithubClosureKind.COMPLETED_ISSUE
        )
        issue_closure = store.get_terminal_github_closure(
            work_item.work_item_id,
            terminal_issue_kind,
        )
        expected_issue_reason = (
            "not_planned" if disposition is not None else "completed"
        )
        if (
            task.is_open
            or task.state_reason != expected_issue_reason
            or issue_closure is None
            or issue_closure.state is not TerminalGithubClosureState.COMPLETED
        ):
            return _blocked(
                "terminal_issue_close_receipt_missing",
                task=task,
                work_item=work_item,
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
