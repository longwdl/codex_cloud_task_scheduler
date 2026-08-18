"""Strict lost-receipt injection for the dedicated live Fixture only.

This module is not used by the normal dispatcher entry point.  It wraps the
real ports so a successful remote operation can have its local receipt
discarded once, allowing the following normal sweep to prove reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from codex_dispatcher.config import Config
from codex_dispatcher.git_publisher import (
    GitPublicationInterrupted,
    PublicationReceipt,
)
from codex_dispatcher.publisher import PublicationPlan
from codex_dispatcher.runner_transport import RunnerTransport
from codex_dispatcher.ssh_preflight import SshPreflightPlan, SshPreflightStatus
from codex_dispatcher.ssh_recovery import SshRecoveryAction
from codex_dispatcher.trackers.base import (
    ClaimResult,
    DraftPullRequestRequest,
    PullRequest,
    TaskState,
    Tracker,
    TrackerComment,
    TrackerTask,
)
from codex_dispatcher.turn_orchestration import (
    PublicationRecordedHook,
    TaskBranchPublisher,
)
from codex_dispatcher.work_items import Turn, TurnState, WorkItem, WorkItemState


FIXTURE_REPOSITORY = "longwdl/codex-dispatcher-fixture"


class FixtureFaultPoint(StrEnum):
    PUBLISHER_RECEIPT = "publisher-receipt"
    DRAFT_PR_RECEIPT = "draft-pr-receipt"
    ISSUE_COMMENT_RECEIPT = "issue-comment-receipt"
    PUBLICATION_RECORDED = "publication-recorded"
    RECORDED_PUBLICATION_RECOVERY = "recorded-publication-recovery"


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


@dataclass(slots=True)
class FixtureFaultInjection:
    """Wrap real ports and discard exactly one selected Fixture receipt."""

    fault: FixtureFaultPoint
    issue_number: int
    repository: str = FIXTURE_REPOSITORY
    triggered: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.fault, FixtureFaultPoint):
            raise TypeError("fault must be a FixtureFaultPoint")
        if self.repository != FIXTURE_REPOSITORY:
            raise FixtureFaultRejected("fault injection repository is not the fixed Fixture")
        if type(self.issue_number) is not int or self.issue_number <= 0:
            raise ValueError("fixture issue number must be positive")

    def wrap_tracker(self, tracker: Tracker) -> Tracker:
        return _FixtureFaultTracker(self, tracker)

    def wrap_publisher(self, publisher: TaskBranchPublisher) -> TaskBranchPublisher:
        return _FixtureFaultPublisher(self, publisher)

    def wrap_transport(self, transport: RunnerTransport) -> RunnerTransport:
        return _FixtureFaultTransport(self, transport)

    @property
    def publication_recorded_hook(self) -> PublicationRecordedHook | None:
        if self.fault is FixtureFaultPoint.PUBLICATION_RECORDED:
            return self.after_publication_recorded
        return None

    def require_target(self, repository: str, task_id: str | None = None) -> None:
        if repository != self.repository:
            raise FixtureFaultRejected("tracker operation escaped the fixed Fixture repository")
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
        if self._injection.fault is FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY:
            raise FixtureFaultRejected(
                "recorded publication recovery attempted to invoke the Runner"
            )
        return self._delegate.invoke(request, **kwargs)


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
    def __init__(self, injection: FixtureFaultInjection, delegate: Tracker) -> None:
        self._injection = injection
        self._delegate = delegate

    def list_ready_tasks(self, repository: str) -> tuple[TrackerTask, ...]:
        self._injection.require_target(repository)
        return self._delegate.list_ready_tasks(repository)

    def list_open_tasks(
        self, repository: str, state: TaskState
    ) -> tuple[TrackerTask, ...]:
        self._injection.require_target(repository)
        return self._delegate.list_open_tasks(repository, state)

    def get_task(self, repository: str, task_id: str) -> TrackerTask | None:
        self._injection.require_target(repository, task_id)
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
        }:
            raise FixtureFaultRejected("this Fixture fault stage cannot claim an Issue")
        return self._delegate.claim(
            repository,
            task_id,
            claimant,
            approved_by=approved_by,
        )

    def set_state(
        self, repository: str, task_id: str, state: TaskState
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
        }[self._injection.fault]
        if state not in allowed:
            raise FixtureFaultRejected("Fixture fault stage attempted an unexpected state write")
        return self._delegate.set_state(repository, task_id, state)

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
        if (
            self._injection.fault
            is FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY
        ):
            self._delegate.upsert_run_comment(repository, task_id, marker, body)
            return
        if self._injection.fault is not FixtureFaultPoint.ISSUE_COMMENT_RECEIPT:
            raise FixtureFaultRejected("Fixture reached an unexpected Issue comment write")
        self._delegate.upsert_run_comment(repository, task_id, marker, body)
        self._injection.discard_receipt(FixtureFaultPoint.ISSUE_COMMENT_RECEIPT)

    def find_pr_by_branch(
        self, repository: str, branch_name: str
    ) -> PullRequest | None:
        self._injection.require_target(repository)
        expected_prefix = f"codex/issue-{self._injection.issue_number}-"
        if not branch_name.startswith(expected_prefix):
            raise FixtureFaultRejected("PR lookup escaped the fixed Fixture branch")
        return self._delegate.find_pr_by_branch(repository, branch_name)

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
        if self._injection.fault is not FixtureFaultPoint.DRAFT_PR_RECEIPT:
            raise FixtureFaultRejected("Fixture reached an unexpected Draft PR write")
        pull_request = self._delegate.create_draft_pr(request)
        self._injection.discard_receipt(FixtureFaultPoint.DRAFT_PR_RECEIPT)
        return pull_request


def validate_fixture_preflight(
    plan: SshPreflightPlan,
    *,
    fault: FixtureFaultPoint,
    issue_number: int,
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
        FixtureFaultPoint.PUBLISHER_RECEIPT,
        FixtureFaultPoint.PUBLICATION_RECORDED,
    }:
        if (
            plan.status is not SshPreflightStatus.READY_CANDIDATE
            or plan.recovery_action is not SshRecoveryAction.IDLE
            or work_item is not None
            or task.state is not TaskState.READY
        ):
            raise FixtureFaultRejected("initial fault requires one new ready Fixture candidate")
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
