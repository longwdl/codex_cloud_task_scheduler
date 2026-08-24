"""Strict lost-receipt injection for the dedicated live Fixture only.

This module is not used by the normal dispatcher entry point.  It wraps the
real ports so a successful remote operation can have its local receipt
discarded once, allowing the following normal sweep to prove reconciliation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
import time
from typing import Protocol

from codex_dispatcher.command_runner import RunningBinaryCommand
from codex_dispatcher.config import SLACK_IDEMPOTENCY_CONTRACT_V1, Config
from codex_dispatcher.git_publisher import (
    GitPublicationInterrupted,
    PublicationReceipt,
)
from codex_dispatcher.publisher import PublicationPlan
from codex_dispatcher.runner_protocol import RunnerOperation, RunnerRequest
from codex_dispatcher.runner_transport import (
    RunnerTransport,
    RunnerTransportInterrupted,
    RunnerTransportRejected,
    RunnerTurnRemoteState,
    RunnerTurnReply,
    parse_runner_turn_reply,
)
from codex_dispatcher.source_bundle import SourceBundle
from codex_dispatcher.slack_reporting import (
    SlackDeliveryReceipt,
    SlackDeliveryState,
    SlackPublisher,
    SlackReport,
    SlackReportKind,
)
from codex_dispatcher.ssh_preflight import SshPreflightPlan, SshPreflightStatus
from codex_dispatcher.ssh_recovery import SshRecoveryAction
from codex_dispatcher.ssh_runner_transport import SshInvocationPlan
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.terminal_retention import (
    TerminalBranchCleanupState,
    terminal_branch_request_sha256,
)
from codex_dispatcher.trackers.base import (
    ClaimResult,
    DraftPullRequestRequest,
    IssueCloseReason,
    PullRequest,
    PullRequestState,
    TaskState,
    Tracker,
    TrackerComment,
    TrackerTask,
)
from codex_dispatcher.turn_orchestration import (
    PublicationRecordedHook,
    TaskBranchPublisher,
)
from codex_dispatcher.work_items import (
    Turn,
    TurnState,
    WorkItem,
    WorkItemState,
    validate_git_sha,
    validate_work_item_id,
)
from codex_dispatcher.work_item_lifecycle import WorkItemArchiveStatus


FIXTURE_REPOSITORY = "longwdl/codex-dispatcher-fixture"
FIXTURE_SLACK_WORKSPACE_ID = "T0BQ60N9WH4"
FIXTURE_SLACK_CHANNEL_ID = "C0BR2D0MS8Y"
_SSH_STATUS_PROOF_ATTEMPTS = 5
_SSH_STATUS_PROOF_DELAY_SECONDS = 0.25


class _FixtureSource(Protocol):
    def current(self, repository: str, base_branch: str) -> SourceBundle: ...

    def exact(self, repository: str, base_sha: str) -> SourceBundle: ...


class FixtureFaultPoint(StrEnum):
    PUBLISHER_RECEIPT = "publisher-receipt"
    DRAFT_PR_RECEIPT = "draft-pr-receipt"
    ISSUE_COMMENT_RECEIPT = "issue-comment-receipt"
    PUBLICATION_RECORDED = "publication-recorded"
    RECORDED_PUBLICATION_RECOVERY = "recorded-publication-recovery"
    CLAIM_ACQUIRED_PROCESS_KILL = "claim-acquired-process-kill"
    START_RECEIPT = "start-receipt"
    SSH_TRANSPORT_PROCESS_KILL = "ssh-transport-process-kill"
    START_STATUS_RECOVERY = "start-status-recovery"
    SLACK_ROOT_RECEIPT = "slack-root-receipt"
    SLACK_TERMINAL_RECEIPT = "slack-terminal-receipt"
    COMPLETION_COMMENT_RECEIPT = "completion-comment-receipt"
    COMPLETION_LABEL_RECEIPT = "completion-label-receipt"
    TERMINAL_BRANCH_DELETE_RECEIPT = "terminal-branch-delete-receipt"
    TERMINAL_BRANCH_DELETE_RECOVERY = "terminal-branch-delete-recovery"


class FixtureFaultRejected(RuntimeError):
    """Raised before a write when the live Fixture stage is not exact."""


class FixtureReceiptLost(RuntimeError):
    """Expected interruption after a proven Fixture write."""


class FixtureProcessInterrupted(RuntimeError):
    """Expected Fixture stop after one durable local state transition."""


def validate_fixture_config(config: Config) -> None:
    """Reject any config that differs from the dedicated Fixture contract."""
    if not isinstance(config, Config):
        raise TypeError("config must be a Config")
    if len(config.repositories) != 1:
        raise FixtureFaultRejected(
            "Fixture fault config must contain only the fixed Fixture repository"
        )
    repository = config.repositories[0]
    if (
        repository.slug != FIXTURE_REPOSITORY
        or repository.base_branch != "main"
        or repository.allowed_paths != ("README.md",)
        or repository.denied_paths
        or repository.maintainers != ("longwdl",)
        or repository.required_checks != ("fixture",)
    ):
        raise FixtureFaultRejected(
            "Fixture fault config does not match the fixed Fixture contract"
        )


def validate_fixture_slack_config(config: Config) -> None:
    """Require the exact Slack channel whose idempotency contract was proven live."""
    validate_fixture_config(config)
    runtime = config.slack_runtime
    if (
        runtime is None
        or runtime.issue_channel_id != FIXTURE_SLACK_CHANNEL_ID
        or runtime.idempotency_contract != SLACK_IDEMPOTENCY_CONTRACT_V1
    ):
        raise FixtureFaultRejected(
            "Slack receipt fault config does not match the fixed Fixture contract"
        )


@dataclass(slots=True)
class FixtureFaultInjection:
    """Wrap real ports and discard exactly one selected Fixture receipt."""

    fault: FixtureFaultPoint
    issue_number: int
    repository: str = FIXTURE_REPOSITORY
    triggered: bool = False
    claim_acquired_callback: Callable[[TrackerTask], None] | None = None
    pinned_base_sha: str | None = None
    expected_work_item_id: str | None = None
    expected_head_sha: str | None = None
    recovery_operations: list[RunnerOperation] = field(default_factory=list)
    ssh_process_pid: int | None = field(default=None, init=False)
    ssh_process_group_id: int | None = field(default=None, init=False)
    ssh_session_id: int | None = field(default=None, init=False)
    ssh_status_state: RunnerTurnRemoteState | None = field(default=None, init=False)
    ssh_status_error_code: str | None = field(default=None, init=False)
    ssh_status_attempts: int = field(default=0, init=False)
    ssh_interrupt_rejection: str | None = field(default=None, init=False)
    slack_receipt: SlackDeliveryReceipt | None = field(default=None, init=False)
    completion_identity_validated: bool = field(default=False, init=False)
    completion_comment_projected: bool = field(default=False, init=False)
    completion_label_projected: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.fault, FixtureFaultPoint):
            raise TypeError("fault must be a FixtureFaultPoint")
        if self.repository != FIXTURE_REPOSITORY:
            raise FixtureFaultRejected("fault injection repository is not the fixed Fixture")
        if type(self.issue_number) is not int or self.issue_number <= 0:
            raise ValueError("fixture issue number must be positive")
        if self.claim_acquired_callback is not None and (
            self.fault is not FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL
            or not callable(self.claim_acquired_callback)
        ):
            raise FixtureFaultRejected(
                "claim callback is allowed only for the process-kill Fixture"
            )
        if self.pinned_base_sha is not None:
            self.pinned_base_sha = validate_git_sha(
                self.pinned_base_sha,
                "pinned_base_sha",
            )
            if self.fault is not FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL:
                raise FixtureFaultRejected(
                    "pinned base is allowed only for the process-kill Fixture"
                )
        if self.fault in {
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECEIPT,
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECOVERY,
        }:
            if self.expected_work_item_id is None or self.expected_head_sha is None:
                raise FixtureFaultRejected(
                    "terminal branch receipt fault requires exact WorkItem and head"
                )
            self.expected_work_item_id = validate_work_item_id(
                self.expected_work_item_id
            )
            self.expected_head_sha = validate_git_sha(
                self.expected_head_sha,
                "expected_head_sha",
            )
        elif self.expected_work_item_id is not None or self.expected_head_sha is not None:
            raise FixtureFaultRejected(
                "terminal branch identity is allowed only for its receipt fault"
            )

    def wrap_tracker(
        self,
        tracker: Tracker,
        *,
        store: StateStore | None = None,
    ) -> Tracker:
        if self.fault in {
            FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT,
            FixtureFaultPoint.COMPLETION_LABEL_RECEIPT,
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECEIPT,
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECOVERY,
        } and not isinstance(store, StateStore):
            raise FixtureFaultRejected(
                "this receipt fault requires durable SQLite proof"
            )
        return _FixtureFaultTracker(self, tracker, store=store)

    def wrap_publisher(self, publisher: TaskBranchPublisher) -> TaskBranchPublisher:
        return _FixtureFaultPublisher(self, publisher)

    def wrap_transport(self, transport: RunnerTransport) -> RunnerTransport:
        return _FixtureFaultTransport(self, transport)

    def wrap_slack_publisher(
        self,
        publisher: SlackPublisher,
        *,
        store: StateStore,
    ) -> SlackPublisher:
        if self.fault in {
            FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT,
            FixtureFaultPoint.COMPLETION_LABEL_RECEIPT,
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECEIPT,
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECOVERY,
        }:
            return _FixtureRejectingSlackPublisher()
        if self.fault not in {
            FixtureFaultPoint.SLACK_ROOT_RECEIPT,
            FixtureFaultPoint.SLACK_TERMINAL_RECEIPT,
        }:
            return publisher
        return _FixtureFaultSlackPublisher(self, publisher, store=store)

    def wrap_source(self, source: _FixtureSource) -> _FixtureSource:
        if self.fault in {
            FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT,
            FixtureFaultPoint.COMPLETION_LABEL_RECEIPT,
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECEIPT,
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECOVERY,
        }:
            return _FixtureRejectingSource()
        if self.fault is not FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL:
            return source
        if self.pinned_base_sha is None:
            raise FixtureFaultRejected(
                "process-kill Fixture requires one independently verified base SHA"
            )
        return _FixturePinnedSource(self, source)

    def ssh_process_started_hook(
        self,
        *,
        store: StateStore,
        status_transport: RunnerTransport,
    ) -> Callable[[RunnerRequest, SshInvocationPlan, RunningBinaryCommand], None] | None:
        """Build the one guarded hook that may interrupt an exact START SSH client."""
        if self.fault is not FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL:
            return None
        if not isinstance(store, StateStore):
            raise TypeError("store must be a StateStore")

        def observe_and_interrupt(
            request: RunnerRequest,
            plan: SshInvocationPlan,
            process: RunningBinaryCommand,
        ) -> None:
            try:
                self._observe_and_interrupt_ssh_start(
                    request=request,
                    plan=plan,
                    process=process,
                    store=store,
                    status_transport=status_transport,
                )
            except FixtureFaultRejected as exc:
                # A proof failure must leave the primary SSH client alone.  Its
                # eventual reply is deliberately treated as ambiguous by the
                # outer Fixture transport, preserving STATUS-only recovery.
                self.ssh_interrupt_rejection = str(exc)
            except Exception:
                self.ssh_interrupt_rejection = "SSH interruption proof failed unexpectedly"

        return observe_and_interrupt

    def _observe_and_interrupt_ssh_start(
        self,
        *,
        request: RunnerRequest,
        plan: SshInvocationPlan,
        process: RunningBinaryCommand,
        store: StateStore,
        status_transport: RunnerTransport,
    ) -> None:
        if self.triggered:
            raise FixtureFaultRejected("live Fixture fault was triggered more than once")
        if request.operation is not RunnerOperation.START or request.turn_id is None:
            return
        if tuple(plan.argv) != process.argv or process.termination_requested:
            raise FixtureFaultRejected("primary SSH process identity is invalid")
        try:
            process_group_id = process.process_group_id
            session_id = process.session_id
        except (OSError, RuntimeError) as exc:
            raise FixtureFaultRejected("primary SSH process is not observable") from exc
        if process_group_id != process.pid or session_id != process.pid:
            raise FixtureFaultRejected("primary SSH process is not an exact session leader")

        _require_persisted_ssh_start(self, store, request)
        status_request = RunnerRequest(
            RunnerOperation.STATUS,
            request.work_item_id,
            turn_id=request.turn_id,
        )
        accepted_reply = None
        for attempt in range(_SSH_STATUS_PROOF_ATTEMPTS):
            self.ssh_status_attempts += 1
            try:
                output = status_transport.invoke(status_request)
                if output.artifact is not None:
                    raise FixtureFaultRejected(
                        "STATUS proof returned an unexpected artifact"
                    )
                reply = parse_runner_turn_reply(output.payload)
            except (RunnerTransportInterrupted, RunnerTransportRejected):
                reply = None
            if reply is not None:
                if (
                    reply.operation is not RunnerOperation.STATUS
                    or reply.work_item_id != request.work_item_id
                    or reply.turn_id != request.turn_id
                ):
                    raise FixtureFaultRejected("STATUS proof identity is invalid")
                if _is_durable_ssh_start_proof(reply):
                    accepted_reply = reply
                    break
                if not (
                    reply.state is RunnerTurnRemoteState.UNKNOWN
                    and reply.error_code == "turn_not_found"
                ):
                    raise FixtureFaultRejected("STATUS did not prove a durable remote Turn")
            if attempt + 1 < _SSH_STATUS_PROOF_ATTEMPTS:
                time.sleep(_SSH_STATUS_PROOF_DELAY_SECONDS)
        if accepted_reply is None:
            raise FixtureFaultRejected("STATUS could not prove a durable remote Turn")

        _require_persisted_ssh_start(self, store, request)
        process.kill_exact_process_group(
            expected_argv=plan.argv,
            expected_pid=process.pid,
        )
        if not process.termination_requested:
            raise FixtureFaultRejected("primary SSH process group was not terminated")
        self.ssh_process_pid = process.pid
        self.ssh_process_group_id = process_group_id
        self.ssh_session_id = session_id
        self.ssh_status_state = accepted_reply.state
        self.ssh_status_error_code = accepted_reply.error_code
        self.triggered = True

    @property
    def publication_recorded_hook(self) -> PublicationRecordedHook | None:
        if self.fault is FixtureFaultPoint.PUBLICATION_RECORDED:
            return self.after_publication_recorded
        return None

    @property
    def claim_acquired_hook(self) -> Callable[[TrackerTask], None] | None:
        if self.fault is FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL:
            return self.after_claim_acquired
        return None

    @property
    def completion_candidate_hook(
        self,
    ) -> Callable[[TrackerTask, WorkItem, PullRequest], None]:
        return self.before_completion_candidate

    def before_completion_candidate(
        self,
        task: TrackerTask,
        work_item: WorkItem,
        pull_request: PullRequest,
    ) -> None:
        self.require_repository(task.repository)
        if self.fault is FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT:
            if (
                not self.completion_identity_validated
                or task.task_id != str(self.issue_number)
                or task.issue_number != self.issue_number
                or task.issue_node_id != work_item.issue_node_id
                or task.state is not TaskState.REVIEW
                or work_item.repository != self.repository
                or work_item.issue_number != self.issue_number
                or work_item.state is not WorkItemState.REVIEW
                or work_item.last_published_sha is None
                or work_item.pr_number != pull_request.number
                or pull_request.state is not PullRequestState.MERGED
                or pull_request.is_draft
                or pull_request.is_cross_repository
                or pull_request.head_sha != work_item.last_published_sha
            ):
                raise FixtureFaultRejected(
                    "completion comment fault candidate identity is invalid"
                )
            return
        if (
            task.issue_number != work_item.issue_number
            or pull_request.number != work_item.pr_number
        ):
            raise FixtureFaultRejected("Fixture completion candidate identity is invalid")
        raise FixtureFaultRejected(
            "Fixture fault stage cannot complete an unrelated WorkItem"
        )

    def require_repository(self, repository: str) -> None:
        if repository != self.repository:
            raise FixtureFaultRejected(
                "tracker operation escaped the fixed Fixture repository"
            )

    def require_target(self, repository: str, task_id: str | None = None) -> None:
        self.require_repository(repository)
        if task_id is not None and task_id != str(self.issue_number):
            raise FixtureFaultRejected("tracker operation escaped the fixed Fixture Issue")

    def discard_receipt(self, operation: FixtureFaultPoint) -> None:
        if operation is not self.fault:
            raise FixtureFaultRejected("live Fixture reached an unexpected write stage")
        if self.triggered:
            raise FixtureFaultRejected("live Fixture fault was triggered more than once")
        self.triggered = True
        raise FixtureReceiptLost(f"Fixture intentionally discarded {operation.value}")

    def after_publication_recorded(self, work_item: WorkItem, turn: Turn) -> None:
        """Stop only after the exact checkpoint anchor is durable in SQLite."""
        self.require_target(work_item.repository)
        if (
            self.fault is not FixtureFaultPoint.PUBLICATION_RECORDED
            or work_item.issue_number != self.issue_number
            or turn.work_item_id != work_item.work_item_id
            or turn.state is not TurnState.CHECKPOINTING
            or turn.output_head_sha is None
            or work_item.last_published_sha != turn.output_head_sha
        ):
            raise FixtureFaultRejected(
                "publication-recorded hook escaped its exact Fixture checkpoint"
            )
        if self.triggered:
            raise FixtureFaultRejected("live Fixture fault was triggered more than once")
        self.triggered = True
        raise FixtureProcessInterrupted(
            "Fixture intentionally stopped after recording the publication"
        )

    def after_claim_acquired(self, task: TrackerTask) -> None:
        """Stop after the exact remote claim and before local WorkItem persistence."""
        self.require_target(task.repository, task.task_id)
        if (
            self.fault is not FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL
            or task.issue_number != self.issue_number
            or task.state is not TaskState.DISPATCHING
            or not task.is_open
        ):
            raise FixtureFaultRejected(
                "claim-acquired hook escaped its exact Fixture claim"
            )
        if self.triggered:
            raise FixtureFaultRejected("live Fixture fault was triggered more than once")
        self.triggered = True
        if self.claim_acquired_callback is not None:
            self.claim_acquired_callback(task)
            raise FixtureFaultRejected("claim-acquired callback unexpectedly returned")
        raise FixtureProcessInterrupted(
            "Fixture intentionally stopped after acquiring the remote claim"
        )


class _FixtureFaultTransport:
    def __init__(
        self,
        injection: FixtureFaultInjection,
        delegate: RunnerTransport,
    ) -> None:
        self._injection = injection
        self._delegate = delegate

    def invoke(self, request, **kwargs):
        self._injection.require_target(self._injection.repository)
        if self._injection.fault in {
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECEIPT,
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECOVERY,
        }:
            raise FixtureFaultRejected(
                "terminal branch receipt fault unexpectedly invoked the Runner"
            )
        if self._injection.fault in {
            FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT,
            FixtureFaultPoint.COMPLETION_LABEL_RECEIPT,
        }:
            raise FixtureFaultRejected(
                "completion receipt fault unexpectedly invoked the Runner"
            )
        if self._injection.fault is FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL:
            raise FixtureFaultRejected(
                "claim-acquired process kill unexpectedly invoked the Runner"
            )
        if self._injection.fault is FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY:
            raise FixtureFaultRejected(
                "recorded publication recovery attempted to invoke the Runner"
            )
        if (
            self._injection.fault is FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL
            and request.operation
            not in {RunnerOperation.PREPARE, RunnerOperation.START}
        ):
            raise FixtureFaultRejected(
                "SSH transport process kill reached an unexpected Runner operation"
            )
        if self._injection.fault is FixtureFaultPoint.START_STATUS_RECOVERY:
            if request.operation in {
                RunnerOperation.PREPARE,
                RunnerOperation.START,
                RunnerOperation.RESUME,
            }:
                raise FixtureFaultRejected(
                    "START recovery attempted to replay Runner execution"
                )
            expected = (
                RunnerOperation.STATUS
                if not self._injection.recovery_operations
                else RunnerOperation.EXPORT
            )
            if request.operation is not expected:
                raise FixtureFaultRejected(
                    "START recovery Runner operation order is invalid"
                )
            output = self._delegate.invoke(request, **kwargs)
            self._injection.recovery_operations.append(request.operation)
            return output
        output = self._delegate.invoke(request, **kwargs)
        if self._injection.fault is FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL:
            if request.operation is RunnerOperation.PREPARE:
                return output
            raise RunnerTransportInterrupted(
                "Fixture SSH interruption was not safely authorized"
            )
        if (
            self._injection.fault is FixtureFaultPoint.START_RECEIPT
            and request.operation is RunnerOperation.START
        ):
            reply = parse_runner_turn_reply(output.payload)
            if (
                output.artifact is not None
                or reply.operation is not RunnerOperation.START
                or reply.work_item_id != request.work_item_id
                or reply.turn_id != request.turn_id
                or reply.state
                not in {RunnerTurnRemoteState.RUNNING, RunnerTurnRemoteState.FINISHED}
            ):
                raise FixtureFaultRejected(
                    "START receipt fault did not receive one exact Runner reply"
                )
            if self._injection.triggered:
                raise FixtureFaultRejected("live Fixture fault was triggered more than once")
            self._injection.triggered = True
            raise RunnerTransportInterrupted(
                "Fixture intentionally discarded the successful START response"
            )
        return output


class _FixtureFaultSlackPublisher:
    """Discard one proven Slack receipt only at an exact durable Fixture stage."""

    def __init__(
        self,
        injection: FixtureFaultInjection,
        delegate: SlackPublisher,
        *,
        store: StateStore,
    ) -> None:
        self._injection = injection
        self._delegate = delegate
        self._store = store

    def publish(self, report: SlackReport) -> SlackDeliveryReceipt:
        if not isinstance(report, SlackReport):
            raise TypeError("report must be a SlackReport")
        work_item = self._store.get_work_item(report.work_item_id)
        delivery = self._store.get_slack_delivery(report.deduplication_key)
        if (
            work_item is None
            or work_item.repository != self._injection.repository
            or work_item.issue_number != self._injection.issue_number
            or report.channel_id != FIXTURE_SLACK_CHANNEL_ID
            or delivery is None
            or delivery.state is not SlackDeliveryState.PREPARED
            or delivery.work_item_id != work_item.work_item_id
            or delivery.kind is not report.kind
            or delivery.turn_id != report.turn_id
            or delivery.thread_ts != report.thread_ts
        ):
            raise FixtureFaultRejected(
                "Slack receipt fault escaped its exact prepared outbox identity"
            )

        if self._injection.fault is FixtureFaultPoint.SLACK_ROOT_RECEIPT:
            self._require_unstarted_root(report, work_item)
        elif report.kind is SlackReportKind.ROOT:
            self._require_unstarted_root(report, work_item)
            # The terminal fault begins from the root-receipt recovery state.  The
            # root retry must be allowed to complete before the one terminal
            # receipt is discarded later in the same sweep.
            return self._delegate.publish(report)
        else:
            self._require_finished_result(report, work_item)

        receipt = self._delegate.publish(report)
        if (
            not isinstance(receipt, SlackDeliveryReceipt)
            or receipt.deduplication_key != report.deduplication_key
            or receipt.channel_id != report.channel_id
            or receipt.thread_ts != (report.thread_ts or receipt.message_ts)
        ):
            raise FixtureFaultRejected(
                "Slack receipt fault did not receive one exact provider receipt"
            )
        self._injection.slack_receipt = receipt
        self._injection.discard_receipt(self._injection.fault)
        raise AssertionError("unreachable")

    def _require_unstarted_root(
        self,
        report: SlackReport,
        work_item: WorkItem,
    ) -> None:
        if (
            report.kind is not SlackReportKind.ROOT
            or report.turn_id is not None
            or report.thread_ts is not None
            or work_item.state is not WorkItemState.READY
            or work_item.codex_session_id is not None
            or work_item.last_published_sha is not None
            or work_item.pr_number is not None
            or work_item.slack_channel_id is not None
            or work_item.slack_thread_ts is not None
            or self._store.get_active_turn() is not None
        ):
            raise FixtureFaultRejected(
                "Slack root receipt fault requires one exact unstarted WorkItem"
            )

    def _require_finished_result(
        self,
        report: SlackReport,
        work_item: WorkItem,
    ) -> None:
        turn = (
            None if report.turn_id is None else self._store.get_turn(report.turn_id)
        )
        root = self._store.get_slack_delivery(
            f"slack:{work_item.work_item_id}:root"
        )
        if (
            report.kind is not SlackReportKind.RESULT
            or work_item.state is not WorkItemState.REVIEW
            or work_item.codex_session_id is None
            or work_item.last_published_sha is None
            or work_item.pr_number is None
            or work_item.slack_channel_id != FIXTURE_SLACK_CHANNEL_ID
            or work_item.slack_thread_ts != report.thread_ts
            or self._store.get_active_turn() is not None
            or turn is None
            or turn.work_item_id != work_item.work_item_id
            or turn.state is not TurnState.FINISHED
            or turn.result_status != "completed"
            or turn.output_head_sha != work_item.last_published_sha
            or root is None
            or root.state is not SlackDeliveryState.DELIVERED
            or root.kind is not SlackReportKind.ROOT
            or root.message_ts != work_item.slack_thread_ts
        ):
            raise FixtureFaultRejected(
                "Slack terminal receipt fault requires one exact finished Fixture Turn"
            )


class _FixtureRejectingSlackPublisher:
    def publish(self, report: SlackReport) -> SlackDeliveryReceipt:
        raise FixtureFaultRejected(
            "completion receipt fault unexpectedly invoked Slack"
        )


class _FixturePinnedSource:
    def __init__(
        self,
        injection: FixtureFaultInjection,
        delegate: _FixtureSource,
    ) -> None:
        self._injection = injection
        self._delegate = delegate

    def current(self, repository: str, base_branch: str) -> SourceBundle:
        self._injection.require_target(repository)
        if base_branch != "main" or self._injection.pinned_base_sha is None:
            raise FixtureFaultRejected(
                "process-kill source escaped its verified Fixture base"
            )
        return self._delegate.exact(repository, self._injection.pinned_base_sha)

    def exact(self, repository: str, base_sha: str) -> SourceBundle:
        self._injection.require_target(repository)
        if base_sha != self._injection.pinned_base_sha:
            raise FixtureFaultRejected(
                "process-kill source requested a different base SHA"
            )
        return self._delegate.exact(repository, base_sha)


class _FixtureRejectingSource:
    def current(self, repository: str, base_branch: str) -> SourceBundle:
        raise FixtureFaultRejected(
            "completion receipt fault unexpectedly requested a source bundle"
        )

    def exact(self, repository: str, base_sha: str) -> SourceBundle:
        raise FixtureFaultRejected(
            "completion receipt fault unexpectedly requested a source bundle"
        )


class _FixtureFaultPublisher:
    def __init__(
        self,
        injection: FixtureFaultInjection,
        delegate: TaskBranchPublisher,
    ) -> None:
        self._injection = injection
        self._delegate = delegate

    def publish(
        self,
        artifact: bytes,
        *,
        plan: PublicationPlan,
        work_item: WorkItem,
    ) -> PublicationReceipt:
        self._injection.require_target(work_item.repository)
        if (
            work_item.issue_number != self._injection.issue_number
            or plan.repository != self._injection.repository
            or plan.work_item_id != work_item.work_item_id
            or plan.target_ref != f"refs/heads/{work_item.task_branch}"
        ):
            raise FixtureFaultRejected("Publisher operation escaped the fixed Fixture identity")
        if (
            self._injection.fault
            is FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY
        ):
            raise FixtureFaultRejected(
                "recorded publication recovery attempted to invoke the Publisher"
            )
        if self._injection.fault is FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL:
            raise FixtureFaultRejected(
                "claim-acquired process kill unexpectedly invoked the Publisher"
            )
        if self._injection.fault is FixtureFaultPoint.START_RECEIPT:
            raise FixtureFaultRejected(
                "START receipt fault unexpectedly invoked the Publisher"
            )
        if self._injection.fault is FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL:
            raise FixtureFaultRejected(
                "SSH transport process kill unexpectedly invoked the Publisher"
            )
        if self._injection.fault is FixtureFaultPoint.SLACK_ROOT_RECEIPT:
            raise FixtureFaultRejected(
                "Slack root receipt fault unexpectedly invoked the Publisher"
            )
        if self._injection.fault in {
            FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT,
            FixtureFaultPoint.COMPLETION_LABEL_RECEIPT,
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECEIPT,
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECOVERY,
        }:
            raise FixtureFaultRejected(
                "this receipt fault unexpectedly invoked the Publisher"
            )
        if self._injection.fault is FixtureFaultPoint.START_STATUS_RECOVERY and (
            self._injection.recovery_operations
            != [RunnerOperation.STATUS, RunnerOperation.EXPORT]
        ):
            raise FixtureFaultRejected(
                "START recovery attempted publication before STATUS and EXPORT"
            )
        receipt = self._delegate.publish(
            artifact,
            plan=plan,
            work_item=work_item,
        )
        if self._injection.fault is FixtureFaultPoint.PUBLISHER_RECEIPT:
            if receipt.reused:
                raise FixtureFaultRejected(
                    "Publisher fault requires a newly written Fixture branch"
                )
            if self._injection.triggered:
                raise FixtureFaultRejected("live Fixture fault was triggered more than once")
            self._injection.triggered = True
            raise GitPublicationInterrupted(
                "Fixture intentionally discarded the successful Publisher receipt"
            )
        return receipt


class _FixtureFaultTracker:
    def __init__(
        self,
        injection: FixtureFaultInjection,
        delegate: Tracker,
        *,
        store: StateStore | None,
    ) -> None:
        self._injection = injection
        self._delegate = delegate
        self._store = store

    def collect_api_metrics(self):
        collect = getattr(self._delegate, "collect_api_metrics", None)
        return collect() if callable(collect) else None

    def list_ready_tasks(self, repository: str) -> tuple[TrackerTask, ...]:
        self._injection.require_target(repository)
        return self._delegate.list_ready_tasks(repository)

    def list_open_tasks(
        self, repository: str, state: TaskState
    ) -> tuple[TrackerTask, ...]:
        self._injection.require_target(repository)
        return self._delegate.list_open_tasks(repository, state)

    def get_task(self, repository: str, task_id: str) -> TrackerTask | None:
        self._injection.require_repository(repository)
        return self._delegate.get_task(repository, task_id)

    def list_comments(
        self, repository: str, task_id: str
    ) -> tuple[TrackerComment, ...]:
        self._injection.require_target(repository, task_id)
        return self._delegate.list_comments(repository, task_id)

    def claim(
        self,
        repository: str,
        task_id: str,
        claimant: str,
        *,
        approved_by: tuple[str, ...] | None = None,
    ) -> ClaimResult:
        self._injection.require_target(repository, task_id)
        if self._injection.fault not in {
            FixtureFaultPoint.PUBLISHER_RECEIPT,
            FixtureFaultPoint.PUBLICATION_RECORDED,
            FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL,
            FixtureFaultPoint.START_RECEIPT,
            FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL,
            FixtureFaultPoint.SLACK_ROOT_RECEIPT,
        }:
            raise FixtureFaultRejected("this Fixture fault stage cannot claim an Issue")
        return self._delegate.claim(
            repository,
            task_id,
            claimant,
            approved_by=approved_by,
        )

    def set_state(
        self,
        repository: str,
        task_id: str,
        state: TaskState,
        *,
        expected_state: TaskState | None = None,
    ) -> TrackerTask:
        self._injection.require_target(repository, task_id)
        allowed = {
            FixtureFaultPoint.PUBLISHER_RECEIPT: {TaskState.RUNNING},
            FixtureFaultPoint.DRAFT_PR_RECEIPT: {TaskState.RUNNING},
            FixtureFaultPoint.ISSUE_COMMENT_RECEIPT: set(),
            FixtureFaultPoint.PUBLICATION_RECORDED: set(),
            FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY: {
                TaskState.RUNNING,
                TaskState.REVIEW,
            },
            FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL: set(),
            FixtureFaultPoint.START_RECEIPT: {TaskState.RUNNING},
            FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL: {TaskState.RUNNING},
            FixtureFaultPoint.START_STATUS_RECOVERY: {
                TaskState.RUNNING,
                TaskState.REVIEW,
            },
            FixtureFaultPoint.SLACK_ROOT_RECEIPT: set(),
            FixtureFaultPoint.SLACK_TERMINAL_RECEIPT: set(),
            FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT: set(),
            FixtureFaultPoint.COMPLETION_LABEL_RECEIPT: {TaskState.COMPLETED},
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECEIPT: set(),
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECOVERY: set(),
        }[self._injection.fault]
        if state not in allowed:
            raise FixtureFaultRejected("Fixture fault stage attempted an unexpected state write")
        if self._injection.fault is FixtureFaultPoint.COMPLETION_LABEL_RECEIPT:
            work_item = self._completion_work_item(WorkItemState.COMPLETED)
            if (
                not self._injection.completion_identity_validated
                or not self._injection.completion_comment_projected
                or task_id != str(work_item.issue_number)
                or state is not TaskState.COMPLETED
            ):
                raise FixtureFaultRejected(
                    "completion label fault was not preceded by exact comment recovery"
                )
            updated = self._delegate.set_state(
                repository,
                task_id,
                state,
                expected_state=expected_state,
            )
            if (
                updated.repository != self._injection.repository
                or updated.issue_number != self._injection.issue_number
                or updated.task_id != str(self._injection.issue_number)
                or updated.issue_node_id != work_item.issue_node_id
                or updated.state is not TaskState.COMPLETED
            ):
                raise FixtureFaultRejected(
                    "completion label fault did not receive an exact completed Issue"
                )
            self._injection.completion_label_projected = True
            self._injection.discard_receipt(
                FixtureFaultPoint.COMPLETION_LABEL_RECEIPT
            )
        return self._delegate.set_state(
            repository,
            task_id,
            state,
            expected_state=expected_state,
        )

    def close_task(
        self,
        repository: str,
        task_id: str,
        *,
        expected_issue_node_id: str,
        reason: IssueCloseReason,
    ) -> TrackerTask:
        self._injection.require_target(repository, task_id)
        return self._delegate.close_task(
            repository,
            task_id,
            expected_issue_node_id=expected_issue_node_id,
            reason=reason,
        )

    def upsert_run_comment(
        self,
        repository: str,
        task_id: str,
        marker: str,
        body: str,
    ) -> None:
        self._injection.require_target(repository, task_id)
        if not marker.startswith("work-item:") or not marker.endswith(":status"):
            raise FixtureFaultRejected("Fixture reached an unexpected Issue comment write")
        if self._injection.fault in {
            FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT,
            FixtureFaultPoint.COMPLETION_LABEL_RECEIPT,
        }:
            work_item = self._completion_work_item(WorkItemState.COMPLETED)
            if (
                not self._injection.completion_identity_validated
                or task_id != str(work_item.issue_number)
                or marker != f"work-item:{work_item.work_item_id}:status"
                or body != self._completion_comment_body(work_item)
            ):
                raise FixtureFaultRejected(
                    "completion comment fault escaped its exact projection identity"
                )
            self._delegate.upsert_run_comment(repository, task_id, marker, body)
            self._injection.completion_comment_projected = True
            if (
                self._injection.fault
                is FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT
            ):
                self._injection.discard_receipt(
                    FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT
                )
            return
        if (
            self._injection.fault
            is FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY
        ):
            self._delegate.upsert_run_comment(repository, task_id, marker, body)
            return
        if self._injection.fault is FixtureFaultPoint.START_STATUS_RECOVERY:
            self._delegate.upsert_run_comment(repository, task_id, marker, body)
            return
        if self._injection.fault is FixtureFaultPoint.SLACK_TERMINAL_RECEIPT:
            self._delegate.upsert_run_comment(repository, task_id, marker, body)
            return
        if self._injection.fault is not FixtureFaultPoint.ISSUE_COMMENT_RECEIPT:
            raise FixtureFaultRejected("Fixture reached an unexpected Issue comment write")
        self._delegate.upsert_run_comment(repository, task_id, marker, body)
        self._injection.discard_receipt(FixtureFaultPoint.ISSUE_COMMENT_RECEIPT)

    def find_pr_by_branch(
        self, repository: str, branch_name: str
    ) -> PullRequest | None:
        self._injection.require_repository(repository)
        pull_request = self._delegate.find_pr_by_branch(repository, branch_name)
        if self._injection.fault not in {
            FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT,
            FixtureFaultPoint.COMPLETION_LABEL_RECEIPT,
        }:
            return pull_request
        expected_state = (
            WorkItemState.REVIEW
            if self._injection.fault
            is FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT
            else WorkItemState.COMPLETED
        )
        work_item = self._completion_work_item(expected_state)
        if branch_name != work_item.task_branch:
            # Recovery planning audits every durable completed WorkItem before
            # it reaches the exact live target.  Those reads are harmless and
            # must not arm the target receipt fault.
            return pull_request
        if (
            pull_request is None
            or pull_request.number != work_item.pr_number
            or pull_request.url
            != f"https://github.com/{repository}/pull/{work_item.pr_number}"
            or pull_request.branch_name != work_item.task_branch
            or pull_request.base_branch != work_item.base_branch
            or pull_request.state is not PullRequestState.MERGED
            or pull_request.is_draft
            or pull_request.is_cross_repository
            or pull_request.head_sha != work_item.last_published_sha
        ):
            raise FixtureFaultRejected(
                "completion receipt fault did not read one exact merged pull request"
            )
        self._injection.completion_identity_validated = True
        return pull_request

    def close_pull_request(
        self,
        repository: str,
        branch_name: str,
        *,
        expected_number: int,
        expected_head_sha: str,
    ) -> PullRequest:
        self._injection.require_repository(repository)
        return self._delegate.close_pull_request(
            repository,
            branch_name,
            expected_number=expected_number,
            expected_head_sha=expected_head_sha,
        )

    def get_branch_head(self, repository: str, branch_name: str) -> str | None:
        self._injection.require_repository(repository)
        if self._injection.fault in {
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECEIPT,
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECOVERY,
        }:
            work_item = self._terminal_branch_work_item()
            if branch_name != work_item.task_branch:
                raise FixtureFaultRejected(
                    "terminal branch receipt read escaped its exact branch"
                )
        return self._delegate.get_branch_head(repository, branch_name)

    def delete_branch(
        self,
        repository: str,
        branch_name: str,
        expected_head_sha: str,
    ) -> None:
        self._injection.require_repository(repository)
        if self._injection.fault is FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECOVERY:
            raise FixtureFaultRejected(
                "terminal branch recovery attempted a second delete"
            )
        if (
            self._injection.fault
            is not FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECEIPT
        ):
            return self._delegate.delete_branch(
                repository, branch_name, expected_head_sha
            )
        work_item = self._terminal_branch_work_item()
        cleanup = self._store.get_terminal_branch_cleanup(work_item.work_item_id)
        request_sha256 = terminal_branch_request_sha256(
            work_item_id=work_item.work_item_id,
            repository=work_item.repository,
            branch_name=work_item.task_branch,
            expected_head_sha=expected_head_sha,
        )
        if (
            branch_name != work_item.task_branch
            or expected_head_sha != self._injection.expected_head_sha
            or cleanup is None
            or cleanup.state is not TerminalBranchCleanupState.PREPARED
            or cleanup.repository != repository
            or cleanup.branch_name != branch_name
            or cleanup.expected_head_sha != expected_head_sha
            or cleanup.request_sha256 != request_sha256
        ):
            raise FixtureFaultRejected(
                "terminal branch receipt delete escaped its exact prepared identity"
            )
        self._delegate.delete_branch(repository, branch_name, expected_head_sha)
        if self._delegate.get_branch_head(repository, branch_name) is not None:
            raise FixtureFaultRejected(
                "terminal branch receipt delete has no independent absence proof"
            )
        self._injection.discard_receipt(
            FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECEIPT
        )

    def _terminal_branch_work_item(self) -> WorkItem:
        if self._store is None:
            raise FixtureFaultRejected(
                "terminal branch receipt fault has no durable SQLite proof"
            )
        work_item = self._store.get_work_item_by_issue(
            self._injection.repository,
            self._injection.issue_number,
        )
        if (
            work_item is None
            or work_item.work_item_id != self._injection.expected_work_item_id
            or work_item.repository != self._injection.repository
            or work_item.issue_number != self._injection.issue_number
            or work_item.state is not WorkItemState.COMPLETED
            or work_item.last_published_sha != self._injection.expected_head_sha
            or work_item.pr_number is None
        ):
            raise FixtureFaultRejected(
                "terminal branch receipt fault has no exact completed WorkItem"
            )
        return work_item

    def _completion_work_item(self, state: WorkItemState) -> WorkItem:
        if self._store is None:
            raise FixtureFaultRejected(
                "completion receipt fault has no durable SQLite proof"
            )
        work_item = self._store.get_work_item_by_issue(
            self._injection.repository,
            self._injection.issue_number,
        )
        if (
            work_item is None
            or work_item.repository != self._injection.repository
            or work_item.issue_number != self._injection.issue_number
            or work_item.state is not state
            or work_item.last_published_sha is None
            or work_item.pr_number is None
        ):
            raise FixtureFaultRejected(
                "completion receipt fault has no exact durable WorkItem"
            )
        return work_item

    def _completion_comment_body(self, work_item: WorkItem) -> str:
        assert work_item.last_published_sha is not None
        assert work_item.pr_number is not None
        lines = [
            f"Codex work item `{work_item.work_item_id}`",
            "",
            f"- Task branch: `{work_item.task_branch}`",
            "- Dispatcher state: `agent:completed`",
            f"- Published checkpoint: `{work_item.last_published_sha}`",
            (
                "- Pull request: "
                f"https://github.com/{work_item.repository}/pull/{work_item.pr_number}"
            ),
        ]
        if (
            work_item.slack_channel_id is None
            and work_item.slack_thread_ts is None
        ):
            return "\n".join(lines)
        if (
            work_item.slack_channel_id is None
            or work_item.slack_thread_ts is None
        ):
            raise FixtureFaultRejected(
                "completed WorkItem has an incomplete Slack thread binding"
            )
        assert self._store is not None
        root = self._store.get_slack_delivery(
            f"slack:{work_item.work_item_id}:root"
        )
        if (
            root is None
            or root.state is not SlackDeliveryState.DELIVERED
            or root.work_item_id != work_item.work_item_id
            or root.kind is not SlackReportKind.ROOT
            or root.channel_id != work_item.slack_channel_id
            or root.message_ts != work_item.slack_thread_ts
            or root.permalink is None
        ):
            raise FixtureFaultRejected(
                "completed WorkItem Slack projection is not durably bound"
            )
        lines.append(f"- Slack execution thread: {root.permalink}")
        return "\n".join(lines)

    def create_draft_pr(self, request: DraftPullRequestRequest) -> PullRequest:
        self._injection.require_target(request.repository)
        expected_prefix = f"codex/issue-{self._injection.issue_number}-"
        if (
            not request.branch_name.startswith(expected_prefix)
            or request.title != f"Codex work for Issue #{self._injection.issue_number}"
        ):
            raise FixtureFaultRejected("Fixture reached an unexpected Draft PR write")
        if (
            self._injection.fault
            is FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY
        ):
            return self._delegate.create_draft_pr(request)
        if self._injection.fault is FixtureFaultPoint.START_STATUS_RECOVERY:
            return self._delegate.create_draft_pr(request)
        if self._injection.fault is FixtureFaultPoint.SLACK_TERMINAL_RECEIPT:
            return self._delegate.create_draft_pr(request)
        if self._injection.fault is not FixtureFaultPoint.DRAFT_PR_RECEIPT:
            raise FixtureFaultRejected("Fixture reached an unexpected Draft PR write")
        pull_request = self._delegate.create_draft_pr(request)
        self._injection.discard_receipt(FixtureFaultPoint.DRAFT_PR_RECEIPT)
        return pull_request


def _require_persisted_ssh_start(
    injection: FixtureFaultInjection,
    store: StateStore,
    request: RunnerRequest,
) -> None:
    work_item = store.get_work_item_by_issue(
        injection.repository,
        injection.issue_number,
    )
    turn = store.get_active_turn()
    if (
        work_item is None
        or work_item.work_item_id != request.work_item_id
        or work_item.repository != injection.repository
        or work_item.issue_number != injection.issue_number
        or work_item.state is not WorkItemState.RUNNING
        or work_item.codex_session_id is not None
        or work_item.last_published_sha is not None
        or work_item.pr_number is not None
        or turn is None
        or turn.turn_id != request.turn_id
        or turn.work_item_id != request.work_item_id
        or turn.state is not TurnState.STARTING
        or turn.prompt_sha256 != request.prompt_sha256
        or turn.input_head_sha != request.input_head_sha
        or turn.output_head_sha is not None
        or turn.result_status is not None
    ):
        raise FixtureFaultRejected(
            "local SQLite does not contain the exact persisted START Turn"
        )


def _is_durable_ssh_start_proof(reply: RunnerTurnReply) -> bool:
    if reply.state is RunnerTurnRemoteState.FINISHED:
        return True
    return (
        reply.state is RunnerTurnRemoteState.UNKNOWN
        and reply.error_code == "turn_outcome_unresolved"
        and reply.session_id is None
    )


def _completion_preflight_matches(
    plan: SshPreflightPlan,
    *,
    issue_number: int,
    work_item_state: WorkItemState,
) -> bool:
    task = plan.task
    work_item = plan.work_item
    pull_request = plan.pull_request
    return bool(
        task is not None
        and work_item is not None
        and pull_request is not None
        and task.repository == FIXTURE_REPOSITORY
        and task.task_id == str(issue_number)
        and task.issue_number == issue_number
        and task.issue_node_id == work_item.issue_node_id
        and task.state is TaskState.REVIEW
        and work_item.repository == FIXTURE_REPOSITORY
        and work_item.issue_number == issue_number
        and work_item.state is work_item_state
        and work_item.last_published_sha is not None
        and work_item.pr_number == pull_request.number
        and pull_request.url
        == f"https://github.com/{FIXTURE_REPOSITORY}/pull/{work_item.pr_number}"
        and pull_request.branch_name == work_item.task_branch
        and pull_request.base_branch == work_item.base_branch
        and pull_request.state is PullRequestState.MERGED
        and not pull_request.is_draft
        and not pull_request.is_cross_repository
        and pull_request.head_sha == work_item.last_published_sha
        and plan.turn is None
    )


def validate_fixture_preflight(
    plan: SshPreflightPlan,
    *,
    fault: FixtureFaultPoint,
    issue_number: int,
    store: StateStore | None = None,
) -> None:
    """Require the exact state immediately preceding the requested lost receipt."""
    if not isinstance(plan, SshPreflightPlan):
        raise TypeError("plan must be an SshPreflightPlan")
    if not isinstance(fault, FixtureFaultPoint):
        raise TypeError("fault must be a FixtureFaultPoint")
    if type(issue_number) is not int or issue_number <= 0:
        raise ValueError("fixture issue number must be positive")
    task = plan.task
    if (
        task is None
        or task.repository != FIXTURE_REPOSITORY
        or task.issue_number != issue_number
        or task.task_id != str(issue_number)
    ):
        raise FixtureFaultRejected("preflight did not resolve the exact Fixture Issue")

    work_item = plan.work_item
    if fault in {
        FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECEIPT,
        FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECOVERY,
    }:
        if store is None:
            raise FixtureFaultRejected(
                "terminal branch receipt fault requires read-only SQLite proof"
            )
        pull_request = plan.pull_request
        archive = (
            None
            if work_item is None
            else store.get_work_item_archive(work_item.work_item_id)
        )
        absence = (
            None
            if work_item is None
            else store.get_work_item_absence_reconciliation(work_item.work_item_id)
        )
        cleanup = (
            None
            if work_item is None
            else store.get_terminal_branch_cleanup(work_item.work_item_id)
        )
        cleanup_invalid = cleanup is not None
        if (
            fault is FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECOVERY
            and work_item is not None
        ):
            cleanup_invalid = (
                cleanup is None
                or cleanup.state is not TerminalBranchCleanupState.PREPARED
                or cleanup.work_item_id != work_item.work_item_id
                or cleanup.repository != work_item.repository
                or cleanup.branch_name != work_item.task_branch
                or cleanup.expected_head_sha != work_item.last_published_sha
            )
        if (
            plan.status is not SshPreflightStatus.READY_RECOVERY
            or plan.recovery_action
            is not SshRecoveryAction.DELETE_TERMINAL_BRANCH
            or work_item is None
            or work_item.repository != FIXTURE_REPOSITORY
            or work_item.issue_number != issue_number
            or task.issue_node_id != work_item.issue_node_id
            or task.state is not TaskState.COMPLETED
            or not task.is_open
            or work_item.state is not WorkItemState.COMPLETED
            or work_item.last_published_sha is None
            or work_item.pr_number is None
            or pull_request is None
            or pull_request.number != work_item.pr_number
            or pull_request.branch_name != work_item.task_branch
            or pull_request.base_branch != work_item.base_branch
            or pull_request.head_sha != work_item.last_published_sha
            or pull_request.state is not PullRequestState.MERGED
            or pull_request.is_draft
            or pull_request.is_cross_repository
            or cleanup_invalid
            or (
                absence is None
                and (
                    archive is None
                    or archive.status is not WorkItemArchiveStatus.ARCHIVED
                )
            )
        ):
            raise FixtureFaultRejected(
                "terminal branch receipt fault requires one exact completed archived Fixture"
            )
        return

    if fault is FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT:
        if (
            plan.status is not SshPreflightStatus.READY_RECOVERY
            or plan.recovery_action
            is not SshRecoveryAction.COMPLETE_MERGED_WORK_ITEM
            or not _completion_preflight_matches(
                plan,
                issue_number=issue_number,
                work_item_state=WorkItemState.REVIEW,
            )
        ):
            raise FixtureFaultRejected(
                "completion comment fault requires one exact merged Fixture PR"
            )
        return

    if fault is FixtureFaultPoint.COMPLETION_LABEL_RECEIPT:
        if (
            plan.status is not SshPreflightStatus.READY_RECOVERY
            or plan.recovery_action is not SshRecoveryAction.SYNC_TRACKER_STATE
            or not _completion_preflight_matches(
                plan,
                issue_number=issue_number,
                work_item_state=WorkItemState.COMPLETED,
            )
        ):
            raise FixtureFaultRejected(
                "completion label fault requires exact completed projection recovery"
            )
        return

    if fault in {
        FixtureFaultPoint.PUBLISHER_RECEIPT,
        FixtureFaultPoint.PUBLICATION_RECORDED,
        FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL,
        FixtureFaultPoint.START_RECEIPT,
        FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL,
        FixtureFaultPoint.SLACK_ROOT_RECEIPT,
    }:
        if (
            plan.status is not SshPreflightStatus.READY_CANDIDATE
            or plan.recovery_action is not SshRecoveryAction.IDLE
            or work_item is not None
            or task.state is not TaskState.READY
        ):
            raise FixtureFaultRejected("initial fault requires one new ready Fixture candidate")
        return

    if fault is FixtureFaultPoint.SLACK_TERMINAL_RECEIPT:
        if store is None:
            raise FixtureFaultRejected(
                "Slack terminal receipt fault requires read-only SQLite proof"
            )
        root = (
            None
            if work_item is None
            else store.get_slack_delivery(f"slack:{work_item.work_item_id}:root")
        )
        if (
            plan.status is not SshPreflightStatus.READY_RECOVERY
            or plan.recovery_action is not SshRecoveryAction.START_CLAIMED_TURN
            or work_item is None
            or work_item.repository != FIXTURE_REPOSITORY
            or work_item.issue_number != issue_number
            or task.issue_node_id != work_item.issue_node_id
            or task.state is not TaskState.DISPATCHING
            or work_item.state is not WorkItemState.READY
            or work_item.codex_session_id is not None
            or work_item.last_published_sha is not None
            or work_item.pr_number is not None
            or work_item.slack_channel_id is not None
            or work_item.slack_thread_ts is not None
            or plan.turn is not None
            or root is None
            or root.kind is not SlackReportKind.ROOT
            or root.state is not SlackDeliveryState.PREPARED
            or root.channel_id != FIXTURE_SLACK_CHANNEL_ID
            or root.turn_id is not None
            or root.thread_ts is not None
        ):
            raise FixtureFaultRejected(
                "Slack terminal receipt fault requires exact root-receipt recovery"
            )
        return

    if fault is FixtureFaultPoint.START_STATUS_RECOVERY:
        turn = plan.turn
        if (
            plan.status is not SshPreflightStatus.READY_RECOVERY
            or plan.recovery_action is not SshRecoveryAction.RECONCILE_ACTIVE_TURN
            or work_item is None
            or work_item.repository != FIXTURE_REPOSITORY
            or work_item.issue_number != issue_number
            or task.issue_node_id != work_item.issue_node_id
            or task.state is not TaskState.RUNNING
            or work_item.state is not WorkItemState.RUNNING
            or work_item.codex_session_id is not None
            or work_item.last_published_sha is not None
            or work_item.pr_number is not None
            or turn is None
            or turn.work_item_id != work_item.work_item_id
            or turn.state is not TurnState.RECONCILING
            or turn.output_head_sha is not None
            or turn.result_status is not None
        ):
            raise FixtureFaultRejected(
                "START STATUS recovery requires one exact ambiguous active Turn"
            )
        return

    if fault is FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY:
        turn = plan.turn
        if (
            plan.status is not SshPreflightStatus.READY_RECOVERY
            or plan.recovery_action is not SshRecoveryAction.RESUME_PUBLICATION
            or work_item is None
            or work_item.repository != FIXTURE_REPOSITORY
            or work_item.issue_number != issue_number
            or task.issue_node_id != work_item.issue_node_id
            or task.state not in {TaskState.DISPATCHING, TaskState.RUNNING}
            or work_item.state is not WorkItemState.RUNNING
            or work_item.last_published_sha is None
            or work_item.pr_number is not None
            or turn is None
            or turn.work_item_id != work_item.work_item_id
            or turn.state is not TurnState.CHECKPOINTING
            or turn.output_head_sha != work_item.last_published_sha
        ):
            raise FixtureFaultRejected(
                "recorded publication recovery requires one exact durable checkpoint"
            )
        return

    if work_item is None or (
        work_item.repository != FIXTURE_REPOSITORY
        or work_item.issue_number != issue_number
        or task.issue_node_id != work_item.issue_node_id
        or task.state is not TaskState.RUNNING
    ):
        raise FixtureFaultRejected("recovery fault does not match the persisted Fixture identity")

    if fault is FixtureFaultPoint.DRAFT_PR_RECEIPT:
        if (
            plan.status is not SshPreflightStatus.READY_RECOVERY
            or plan.recovery_action is not SshRecoveryAction.RESUME_PUBLICATION
            or work_item.state is not WorkItemState.RUNNING
            or work_item.last_published_sha is not None
            or work_item.pr_number is not None
        ):
            raise FixtureFaultRejected("Draft PR fault requires Publisher receipt recovery")
        return

    if (
        plan.status is not SshPreflightStatus.READY_RECOVERY
        or plan.recovery_action is not SshRecoveryAction.SYNC_TRACKER_STATE
        or work_item.state is not WorkItemState.REVIEW
        or work_item.last_published_sha is None
        or work_item.pr_number is not None
    ):
        raise FixtureFaultRejected("Issue comment fault requires unbound Draft PR recovery")


def validate_fixture_orphan_claim_recovery(
    plan: SshPreflightPlan,
    *,
    issue_number: int,
) -> None:
    """Require the exact post-kill state for a remotely claimed, unpersisted Issue."""
    if not isinstance(plan, SshPreflightPlan):
        raise TypeError("plan must be a SshPreflightPlan")
    if type(issue_number) is not int or issue_number <= 0:
        raise ValueError("fixture issue number must be positive")
    task = plan.task
    if (
        plan.status is not SshPreflightStatus.READY_RECOVERY
        or plan.recovery_action is not SshRecoveryAction.RECOVER_ORPHAN_CLAIM
        or task is None
        or task.repository != FIXTURE_REPOSITORY
        or task.task_id != str(issue_number)
        or task.issue_number != issue_number
        or task.state is not TaskState.DISPATCHING
        or not task.is_open
        or plan.work_item is not None
        or plan.turn is not None
    ):
        raise FixtureFaultRejected(
            "process kill did not leave one exact recoverable orphan claim"
        )
