from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_dispatcher.control_sweep import (
    ControlSweepStatus,
    SourceSnapshotProvider,
    SshControlSweep,
)
from codex_dispatcher.config import RepositoryAdmissionConfig
from codex_dispatcher.dispatcher_lock import (
    DispatcherLockUnavailable,
    DispatcherProcessLock,
)
from codex_dispatcher.git_publisher import GitPublicationInterrupted
from codex_dispatcher.github_delivery import GitHubDeliveryCoordinator
from codex_dispatcher.publisher import VerifiedBundle
from codex_dispatcher.repository_admission import (
    HIGHER_VALUE_CANARY_REPOSITORY,
    HigherValueCanaryTarget,
    RepositoryClass,
    RepositoryRecoveryProfile,
    RepositoryTargetReadbackProfile,
)
from codex_dispatcher.runner_protocol import (
    NEXT_PROTOCOL_VERSION,
    RunnerOperation,
    RunnerRequest,
    parse_agent_result,
)
from codex_dispatcher.slack_delivery import SlackDeliveryCoordinator
from codex_dispatcher.slack_reporting import (
    SlackDeliveryReceipt,
    SlackReport,
    SlackReportKind,
)
from codex_dispatcher.source_bundle import SourceBundle
from codex_dispatcher.ssh_dispatch_service import OfflineSshDispatchService
from codex_dispatcher.ssh_recovery import TerminalBranchCleanupFixtureTarget
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.terminal_retention import (
    TerminalBranchCleanupOutcome,
    TerminalBranchCleanupState,
)
from codex_dispatcher.testing.fake_runner import (
    FakeBundleVerifier,
    FakeSshRunnerTransport,
    FakeTurnFixture,
)
from codex_dispatcher.testing.fakes import FakeTracker
from codex_dispatcher.trackers.base import (
    PullRequest,
    PullRequestState,
    TaskState,
    TrackerComment,
    TrackerTask,
)
from codex_dispatcher.turn_orchestration import OfflineTurnOrchestrator, TurnProgress
from codex_dispatcher.work_items import Turn, TurnState, WorkItemState
from codex_dispatcher.work_item_lifecycle import WorkItemDispositionKind
from tests.test_scheduler import make_config
from tests.test_ssh_dispatch_planning import BASE_SHA, claimed_task


SESSION = "123e4567-e89b-12d3-a456-426614174000"
TURN_ID = "turn_" + "1" * 32


def _bundle(base_sha: str = BASE_SHA) -> SourceBundle:
    artifact = b"control-sweep-source"
    return SourceBundle(
        artifact,
        base_sha,
        sha256(artifact).hexdigest(),
        len(artifact),
    )


def _blocked_result():
    return parse_agent_result(
        json.dumps(
            {
                "schema_version": 2,
                "status": "blocked",
                "summary": "Fixture stopped at a reviewed boundary",
                "acceptance": [],
                "remaining_work": [],
                "needs_input": [],
                "tests": [{"name": "fixture", "status": "passed"}],
                "changed_paths": [],
                "blocker_code": "fixture_blocked",
                "next_step": "Record the bounded result",
            }
        )
    )


def _completed_result():
    return parse_agent_result(
        json.dumps(
            {
                "schema_version": 2,
                "status": "completed",
                "summary": "Fixture checkpoint completed",
                "acceptance": [],
                "remaining_work": [],
                "needs_input": [],
                "tests": [{"name": "fixture", "status": "passed"}],
                "changed_paths": ["src/codex_dispatcher/main.py"],
                "blocker_code": None,
                "next_step": "Review the published branch",
            }
        )
    )


def _needs_input_result():
    return parse_agent_result(
        json.dumps(
            {
                "schema_version": 2,
                "status": "needs_input",
                "summary": "Fixture requires one reviewed maintainer decision",
                "acceptance": [],
                "remaining_work": ["Apply the maintainer decision"],
                "needs_input": ["Which reviewed marker value should be used?"],
                "tests": [{"name": "fixture", "status": "passed"}],
                "changed_paths": [],
                "blocker_code": None,
                "next_step": "Resume after a maintainer adds /codex-context",
            }
        )
    )


def _ready_task(issue_number: int = 42) -> TrackerTask:
    return replace(
        claimed_task(issue_number),
        state=TaskState.READY,
        labels=("agent:ready", "exec:ssh-cli", "priority:p1"),
    )


class _RecordingSource(SourceSnapshotProvider):
    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.events = events

    def current(self, repository: str, base_branch: str) -> SourceBundle:
        self.calls.append(("current", repository, base_branch))
        if self.events is not None:
            self.events.append("source.current")
        return _bundle()

    def exact(self, repository: str, base_sha: str) -> SourceBundle:
        self.calls.append(("exact", repository, base_sha))
        if self.events is not None:
            self.events.append("source.exact")
        return _bundle(base_sha)


class _FailingSource(_RecordingSource):
    def current(self, repository: str, base_branch: str) -> SourceBundle:
        super().current(repository, base_branch)
        raise RuntimeError("fixture source refresh failed")


class _FailingExactSource(_RecordingSource):
    def exact(self, repository: str, base_sha: str) -> SourceBundle:
        super().exact(repository, base_sha)
        raise RuntimeError("fixture exact source refresh failed")


class _RecordingTracker(FakeTracker):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def claim(
        self,
        repository: str,
        task_id: str,
        claimant: str,
        *,
        approved_by: tuple[str, ...] | None = None,
    ):
        self.events.append("tracker.claim")
        return super().claim(
            repository,
            task_id,
            claimant,
            approved_by=approved_by,
        )


class _ChangingSnapshotTracker(FakeTracker):
    def __init__(self, snapshots: tuple[TrackerTask, ...]) -> None:
        super().__init__()
        self._snapshots = iter(snapshots)

    def get_task(self, repository: str, task_id: str) -> TrackerTask | None:
        self._record("get_task", repository, task_id)
        try:
            return next(self._snapshots)
        except StopIteration:
            return super().get_task(repository, task_id)


class _ChangingCommentsTracker(FakeTracker):
    def __init__(self, snapshots: tuple[tuple[TrackerComment, ...], ...]) -> None:
        super().__init__()
        self._comment_snapshots = iter(snapshots)

    def list_comments(
        self, repository: str, task_id: str
    ) -> tuple[TrackerComment, ...]:
        self._record("list_comments", repository, task_id)
        try:
            return next(self._comment_snapshots)
        except StopIteration:
            return super().list_comments(repository, task_id)


class _InterruptingDeliveryTracker(FakeTracker):
    def __init__(
        self,
        *,
        interrupt_create_once: bool = False,
        interrupt_comment_once: bool = False,
        interrupt_state_once: bool = False,
    ) -> None:
        super().__init__()
        self.interrupt_create_once = interrupt_create_once
        self.interrupt_comment_once = interrupt_comment_once
        self.interrupt_state_once = interrupt_state_once

    def create_draft_pr(self, request):
        pull_request = super().create_draft_pr(request)
        if self.interrupt_create_once:
            self.interrupt_create_once = False
            raise RuntimeError("fixture lost Draft PR receipt")
        return pull_request

    def upsert_run_comment(
        self,
        repository: str,
        task_id: str,
        marker: str,
        body: str,
    ) -> None:
        super().upsert_run_comment(repository, task_id, marker, body)
        if self.interrupt_comment_once:
            self.interrupt_comment_once = False
            raise RuntimeError("fixture lost Issue comment receipt")

    def set_state(self, repository: str, task_id: str, state: TaskState):
        updated = super().set_state(repository, task_id, state)
        if self.interrupt_state_once:
            self.interrupt_state_once = False
            raise RuntimeError("fixture lost Issue label receipt")
        return updated


class _LostBranchDeleteReceiptTracker(FakeTracker):
    def __init__(self) -> None:
        super().__init__()
        self.interrupt_delete_once = True

    def delete_branch(
        self, repository: str, branch_name: str, expected_head_sha: str
    ) -> None:
        super().delete_branch(repository, branch_name, expected_head_sha)
        if self.interrupt_delete_once:
            self.interrupt_delete_once = False
            raise RuntimeError("fixture lost branch deletion receipt")


class _RecordingPublisher:
    def __init__(self, *, interrupt_once: bool = False) -> None:
        self.calls: list[tuple[bytes, object, object]] = []
        self.interrupt_once = interrupt_once

    def publish(self, artifact: bytes, *, plan, work_item):
        self.calls.append((artifact, plan, work_item))
        if self.interrupt_once:
            self.interrupt_once = False
            raise GitPublicationInterrupted("fixture lost receipt")
        return SimpleNamespace(observed_remote_sha=plan.source_sha)


class _InterruptingSlackPublisher:
    def __init__(
        self,
        *,
        interrupt_kind_once: SlackReportKind | None = None,
    ) -> None:
        self.interrupt_kind_once = interrupt_kind_once
        self.calls: list[SlackReport] = []
        self.messages: dict[str, tuple[SlackReport, SlackDeliveryReceipt]] = {}

    def publish(self, report: SlackReport) -> SlackDeliveryReceipt:
        self.calls.append(report)
        existing = self.messages.get(report.deduplication_key)
        if existing is None:
            index = len(self.messages) + 1
            message_ts = f"1700000000.{index:06d}"
            thread_ts = report.thread_ts or message_ts
            query = (
                ""
                if report.kind is SlackReportKind.ROOT
                else f"?thread_ts={thread_ts}&cid={report.channel_id}"
            )
            receipt = SlackDeliveryReceipt(
                report.deduplication_key,
                report.channel_id,
                message_ts,
                thread_ts,
                (
                    f"https://fixture.slack.com/archives/{report.channel_id}/"
                    f"p{message_ts.replace('.', '')}{query}"
                ),
            )
            self.messages[report.deduplication_key] = (report, receipt)
        else:
            original, receipt = existing
            if original != report:
                raise AssertionError("Slack retry changed the persisted payload")
        if self.interrupt_kind_once is report.kind:
            self.interrupt_kind_once = None
            raise RuntimeError("fixture lost Slack receipt")
        return receipt


class SshControlSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = StateStore(root / "state.db")
        self.store.migrate()
        self.transport = FakeSshRunnerTransport()
        self.verifier = FakeBundleVerifier()
        self.dispatch = OfflineSshDispatchService(
            config=make_config(global_max_active=4),
            store=self.store,
            orchestrator=OfflineTurnOrchestrator(
                store=self.store,
                transport=self.transport,
                bundle_verifier=self.verifier,
            ),
        )
        self.lock_path = root / "dispatcher.lock"

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def _sweep(
        self,
        tracker: FakeTracker,
        source: _RecordingSource,
        publisher: _RecordingPublisher | None = None,
        delivery: GitHubDeliveryCoordinator | None = None,
        slack_delivery: SlackDeliveryCoordinator | None = None,
        completion_candidate_hook=None,
        config=None,
        terminal_branch_cleanup_fixture_target=None,
    ) -> SshControlSweep:
        return SshControlSweep(
            config=config or make_config(global_max_active=4),
            store=self.store,
            tracker=tracker,
            dispatch=self.dispatch,
            source=source,
            process_lock=DispatcherProcessLock(self.lock_path),
            publisher=publisher,
            delivery=delivery,
            slack_delivery=slack_delivery,
            completion_candidate_hook=completion_candidate_hook,
            terminal_branch_cleanup_fixture_target=(
                terminal_branch_cleanup_fixture_target
            ),
        )

    def _register_checkpoint(
        self,
        artifact: bytes,
        head_sha: str,
        *,
        changed_paths: tuple[str, ...] = ("src/codex_dispatcher/main.py",),
    ) -> None:
        self.verifier.register(
            VerifiedBundle(
                bundle_sha256=sha256(artifact).hexdigest(),
                head_sha=head_sha,
                parent_anchor_sha=BASE_SHA,
                changed_paths=changed_paths,
                commit_count=1,
                size_bytes=len(artifact),
            )
        )

    def test_idle_full_terminal_audit_records_a_bounded_cursor(self) -> None:
        tracker = FakeTracker()
        sweep = self._sweep(tracker, _RecordingSource())

        first = sweep.run_once()
        cursor = self.store.get_sweep_cursor("terminal_github_audit")
        second = sweep.run_once()

        self.assertEqual(ControlSweepStatus.IDLE, first.status)
        self.assertEqual(ControlSweepStatus.IDLE, second.status)
        self.assertIsNotNone(cursor)
        self.assertEqual(cursor, self.store.get_sweep_cursor("terminal_github_audit"))

    def test_completed_terminal_audit_records_cursor_before_ready_dispatch(self) -> None:
        tracker = FakeTracker()
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        tracker.set_result("claim", SimpleNamespace(claimed=False, task=task, reason="race"))

        result = self._sweep(tracker, _RecordingSource()).run_once()

        self.assertEqual(ControlSweepStatus.CLAIM_NOT_ACQUIRED, result.status)
        self.assertIsNotNone(
            self.store.get_sweep_cursor("terminal_github_audit")
        )

    def test_new_issue_bundles_before_claim_and_runs_one_turn(self) -> None:
        events: list[str] = []
        tracker = _RecordingTracker(events)
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        tracker.comments[task.task_id] = (
            TrackerComment(
                "IC_fixture",
                "alice",
                "/codex-context\nUse the reviewed fixture constraint",
                "2026-08-13T00:30:00Z",
                "2026-08-13T00:30:00Z",
            ),
        )
        source = _RecordingSource(events)
        work_item_id = self._expected_work_item_id(task)
        self.transport.queue_turn(
            work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, _blocked_result()),
        )

        result = self._sweep(tracker, source).run_once(turn_id=TURN_ID)

        self.assertEqual(ControlSweepStatus.BLOCKED, result.status)
        self.assertEqual(["source.current", "tracker.claim"], events)
        self.assertEqual(TaskState.BLOCKED, tracker.tasks[task.task_id].state)
        self.assertEqual(
            [RunnerOperation.PREPARE, RunnerOperation.START],
            [call.operation for call in self.transport.calls],
        )
        persisted = self.store.get_work_item_by_issue(task.repository, task.issue_number)
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(WorkItemState.BLOCKED, persisted.state)
        policy = self.store.get_work_item_repository_policy(persisted.work_item_id)
        self.assertIsNotNone(policy)
        assert policy is not None
        self.assertEqual(policy.policy_sha256, persisted.repository_policy_sha256)
        verdicts = self.store.list_repository_target_readback_verdicts()
        self.assertEqual(1, len(verdicts))
        self.assertEqual("claim_binding", verdicts[0].action)
        self.assertEqual("passed", verdicts[0].status)
        turns = self.store.list_turns(persisted.work_item_id)
        self.assertEqual(("IC_fixture",), turns[0].included_comment_ids)

    def test_claim_hook_runs_after_remote_claim_before_work_item_persistence(self) -> None:
        events: list[str] = []
        tracker = _RecordingTracker(events)
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        source = _RecordingSource(events)

        class ExpectedStop(RuntimeError):
            pass

        def stop_after_claim(claimed: TrackerTask) -> None:
            self.assertEqual(task.issue_node_id, claimed.issue_node_id)
            self.assertEqual(TaskState.DISPATCHING, claimed.state)
            events.append("claim_hook")
            raise ExpectedStop

        sweep = SshControlSweep(
            config=make_config(global_max_active=4),
            store=self.store,
            tracker=tracker,
            dispatch=self.dispatch,
            source=source,
            process_lock=DispatcherProcessLock(self.lock_path),
            claim_acquired_hook=stop_after_claim,
        )

        with self.assertRaises(ExpectedStop):
            sweep.run_once(turn_id=TURN_ID)

        self.assertEqual(["source.current", "tracker.claim", "claim_hook"], events)
        self.assertIsNone(
            self.store.get_work_item_by_issue(task.repository, task.issue_number)
        )
        self.assertEqual([], self.transport.calls)

    def test_needs_input_followup_reuses_issue_session_branch_and_pr(self) -> None:
        tracker = FakeTracker()
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        source = _RecordingSource()
        work_item_id = self._expected_work_item_id(task)
        first_turn_id = "turn_" + "1" * 32
        second_turn_id = "turn_" + "2" * 32
        self.transport.queue_turn(
            work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, _needs_input_result()),
        )
        artifact = b"fixture-followup-result"
        head_sha = "b" * 40
        self._register_checkpoint(artifact, head_sha)
        self.transport.queue_turn(
            work_item_id,
            FakeTurnFixture(SESSION, head_sha, _completed_result(), artifact),
        )
        publisher = _RecordingPublisher()
        delivery = GitHubDeliveryCoordinator(store=self.store, tracker=tracker)
        sweep = self._sweep(
            tracker,
            source,
            publisher=publisher,
            delivery=delivery,
        )

        needs_input = sweep.run_once(turn_id=first_turn_id)

        self.assertEqual(ControlSweepStatus.NEEDS_INPUT, needs_input.status)
        first_item = self.store.get_work_item(work_item_id)
        assert first_item is not None
        self.assertEqual(WorkItemState.WAITING_INPUT, first_item.state)
        self.assertEqual(SESSION, first_item.codex_session_id)
        self.assertIsNone(first_item.last_published_sha)
        self.assertIsNone(first_item.pr_number)

        followup = replace(
            tracker.tasks[task.task_id],
            state=TaskState.READY,
            labels=("agent:ready", "exec:ssh-cli", "priority:p1"),
            ready_approved_by="alice",
            updated_at="2026-08-13T02:00:00Z",
        )
        tracker.ready_tasks = (followup,)
        tracker.tasks[task.task_id] = followup
        tracker.comments[task.task_id] = (
            TrackerComment(
                "IC_followup",
                "alice",
                "/codex-context\nUse the reviewed marker value v2",
                "2026-08-13T01:30:00Z",
                "2026-08-13T01:30:00Z",
            ),
        )

        completed = sweep.run_once(turn_id=second_turn_id)

        self.assertEqual(ControlSweepStatus.REVIEW, completed.status)
        persisted = self.store.get_work_item(work_item_id)
        assert persisted is not None
        self.assertEqual(WorkItemState.REVIEW, persisted.state)
        self.assertEqual(first_item.work_item_id, persisted.work_item_id)
        self.assertEqual(first_item.task_branch, persisted.task_branch)
        self.assertEqual(first_item.runner_directory, persisted.runner_directory)
        self.assertEqual(SESSION, persisted.codex_session_id)
        self.assertEqual(head_sha, persisted.last_published_sha)
        self.assertEqual(1, persisted.pr_number)
        turns = self.store.list_turns(work_item_id)
        self.assertEqual((1, 2), tuple(turn.turn_number for turn in turns))
        self.assertEqual(
            (TurnState.NEEDS_INPUT, TurnState.FINISHED),
            tuple(turn.state for turn in turns),
        )
        self.assertEqual(("IC_followup",), turns[1].included_comment_ids)
        self.assertEqual(
            [
                RunnerOperation.PREPARE,
                RunnerOperation.START,
                RunnerOperation.RESUME,
                RunnerOperation.EXPORT,
            ],
            [call.operation for call in self.transport.calls],
        )
        self.assertEqual([("current", "owner/repo", "main")], source.calls)
        self.assertEqual(1, len(publisher.calls))
        self.assertEqual(
            1,
            sum(call.method == "create_draft_pr" for call in tracker.calls),
        )

    def test_source_failure_happens_before_claim_or_persistence(self) -> None:
        tracker = FakeTracker()
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task

        with self.assertRaisesRegex(RuntimeError, "source refresh failed"):
            self._sweep(tracker, _FailingSource()).run_once(turn_id=TURN_ID)

        self.assertFalse(any(call.method == "claim" for call in tracker.calls))
        self.assertIsNone(
            self.store.get_work_item_by_issue(task.repository, task.issue_number)
        )

    def test_higher_value_canary_base_drift_fails_before_claim(self) -> None:
        base = make_config(global_max_active=1)
        config = replace(
            base,
            repositories=(
                replace(
                    base.repositories[0],
                    slug=HIGHER_VALUE_CANARY_REPOSITORY,
                    allowed_paths=("canary/target.txt",),
                    repository_class=RepositoryClass.HIGHER_VALUE,
                ),
            ),
            repository_admission=RepositoryAdmissionConfig(
                frozenset({RepositoryRecoveryProfile.HIGHER_VALUE_LIVE_V1}),
                frozenset({RepositoryTargetReadbackProfile.HIGHER_VALUE_EXACT_V1}),
            ),
        )
        task = replace(
            _ready_task(77),
            repository=HIGHER_VALUE_CANARY_REPOSITORY,
            body=_ready_task(77).body.replace(
                "- src/codex_dispatcher\n- tests", "- canary/target.txt"
            ),
            issue_node_id="I_kwDOHigherValue77",
        )
        target = HigherValueCanaryTarget(
            task.repository,
            task.issue_number,
            task.issue_node_id or "missing",
            "f" * 40,
        )
        tracker = FakeTracker()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        dispatch = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=OfflineTurnOrchestrator(
                store=self.store,
                transport=self.transport,
                bundle_verifier=self.verifier,
            ),
            higher_value_canary_target=target,
        )
        sweep = SshControlSweep(
            config=config,
            store=self.store,
            tracker=tracker,
            dispatch=dispatch,
            source=_RecordingSource(),
            process_lock=DispatcherProcessLock(self.lock_path),
            higher_value_canary_target=target,
        )

        with self.assertRaisesRegex(ValueError, "base SHA changed"):
            sweep.run_once()

        self.assertFalse(any(call.method == "claim" for call in tracker.calls))
        self.assertIsNone(
            self.store.get_repository_claim_policy(
                target.repository, target.issue_number
            )
        )
        self.assertEqual([], self.transport.calls)

    def test_lost_claim_does_not_persist_or_contact_runner(self) -> None:
        tracker = FakeTracker()
        task = _ready_task()
        tracker.ready_tasks = (task,)
        source = _RecordingSource()

        result = self._sweep(tracker, source).run_once(turn_id=TURN_ID)

        self.assertEqual(ControlSweepStatus.CLAIM_NOT_ACQUIRED, result.status)
        self.assertEqual([("current", "owner/repo", "main")], source.calls)
        self.assertIsNone(
            self.store.get_work_item_by_issue(task.repository, task.issue_number)
        )
        self.assertEqual([], self.transport.calls)

    def test_completed_work_item_is_not_claimed_or_reactivated(self) -> None:
        tracker = FakeTracker()
        ready = _ready_task()
        tracker.ready_tasks = (ready,)
        tracker.tasks[ready.task_id] = ready
        source = _RecordingSource()
        item = self.dispatch.resolve_and_prepare(
            claimed_task(),
            base_sha=BASE_SHA,
            source_bundle=_bundle(),
        )
        self.store.update_work_item_state(item.work_item_id, WorkItemState.RUNNING)
        self.store.update_work_item_state(item.work_item_id, WorkItemState.REVIEW)
        self.store.update_work_item_state(item.work_item_id, WorkItemState.COMPLETED)
        tracker.calls.clear()
        self.transport.calls.clear()

        result = self._sweep(tracker, source).run_once()

        self.assertEqual(ControlSweepStatus.BLOCKED, result.status)
        self.assertEqual("completed_work_item_tracker_state_conflict", result.reason)
        self.assertFalse(any(call.method == "claim" for call in tracker.calls))
        self.assertEqual([], source.calls)
        self.assertEqual([], self.transport.calls)

    def test_merged_pr_completes_local_tombstone_before_issue_projection(self) -> None:
        tracker = FakeTracker()
        task = replace(
            claimed_task(),
            state=TaskState.REVIEW,
            labels=("agent:review", "exec:ssh-cli", "priority:p1"),
        )
        tracker.tasks[task.task_id] = task
        source = _RecordingSource()
        item = self.dispatch.resolve_and_prepare(
            claimed_task(),
            base_sha=BASE_SHA,
            source_bundle=_bundle(),
        )
        self.store.update_work_item_state(item.work_item_id, WorkItemState.RUNNING)
        head_sha = "d" * 40
        self.store.record_published_sha(
            item.work_item_id,
            previous_sha=BASE_SHA,
            head_sha=head_sha,
        )
        item = self.store.update_work_item_state(
            item.work_item_id,
            WorkItemState.REVIEW,
        )
        item = self.store.bind_draft_pr(item.work_item_id, 7)
        tracker.pull_requests[(item.repository, item.task_branch)] = PullRequest(
            number=7,
            url="https://github.com/owner/repo/pull/7",
            branch_name=item.task_branch,
            title="Codex work",
            is_draft=False,
            base_branch=item.base_branch,
            state=PullRequestState.MERGED,
            head_sha=head_sha,
        )
        self.transport.calls.clear()
        delivery = GitHubDeliveryCoordinator(store=self.store, tracker=tracker)

        class ExpectedStop(RuntimeError):
            pass

        def stop_before_completion(*args) -> None:
            raise ExpectedStop

        guarded = self._sweep(
            tracker,
            source,
            delivery=delivery,
            completion_candidate_hook=stop_before_completion,
        )
        with self.assertRaises(ExpectedStop):
            guarded.run_once()
        before_completion = self.store.get_work_item(item.work_item_id)
        assert before_completion is not None
        self.assertEqual(WorkItemState.REVIEW, before_completion.state)

        sweep = self._sweep(tracker, source, delivery=delivery)

        completed = sweep.run_once()

        self.assertEqual(ControlSweepStatus.COMPLETED, completed.status)
        persisted = self.store.get_work_item(item.work_item_id)
        assert persisted is not None
        self.assertEqual(WorkItemState.COMPLETED, persisted.state)
        self.assertEqual(TaskState.COMPLETED, tracker.tasks[task.task_id].state)
        writes = [
            call.method
            for call in tracker.calls
            if call.method in {"upsert_run_comment", "set_state"}
        ]
        self.assertEqual(["upsert_run_comment", "set_state"], writes)
        self.assertEqual([], source.calls)
        self.assertEqual([], self.transport.calls)

        tracker.calls.clear()
        idle = sweep.run_once()
        self.assertEqual(ControlSweepStatus.IDLE, idle.status)
        self.assertFalse(
            any(call.method in {"upsert_run_comment", "set_state"} for call in tracker.calls)
        )

    def test_completed_retention_reconciles_archive_receipt_loss_then_idles(self) -> None:
        tracker = FakeTracker()
        task = replace(
            claimed_task(),
            state=TaskState.COMPLETED,
            labels=("agent:completed", "exec:ssh-cli", "priority:p1"),
        )
        tracker.tasks[task.task_id] = task
        item = self.dispatch.resolve_and_prepare(
            claimed_task(),
            base_sha=BASE_SHA,
            source_bundle=_bundle(),
        )
        head_sha = "d" * 40
        self.store.record_published_sha(
            item.work_item_id,
            previous_sha=BASE_SHA,
            head_sha=head_sha,
        )
        self.transport.set_head(item.work_item_id, head_sha)
        self.store.update_work_item_state(item.work_item_id, WorkItemState.RUNNING)
        item = self.store.update_work_item_state(
            item.work_item_id, WorkItemState.REVIEW
        )
        item = self.store.bind_draft_pr(item.work_item_id, 7)
        item = self.store.update_work_item_state(
            item.work_item_id,
            WorkItemState.COMPLETED,
            updated_at="2026-01-01T00:00:00+00:00",
        )
        tracker.pull_requests[(item.repository, item.task_branch)] = PullRequest(
            number=7,
            url="https://github.com/owner/repo/pull/7",
            branch_name=item.task_branch,
            title="Codex work",
            is_draft=False,
            base_branch=item.base_branch,
            state=PullRequestState.MERGED,
            head_sha=head_sha,
        )
        config = replace(
            make_config(global_max_active=4),
            ssh_runtime=SimpleNamespace(completed_retention_seconds=1),
        )
        sweep = self._sweep(tracker, _RecordingSource(), config=config)
        self.transport.calls.clear()
        self.transport.interrupt_next(RunnerOperation.ARCHIVE)

        awaiting = sweep.run_once()
        reconciled = sweep.run_once()
        idle = sweep.run_once()

        self.assertEqual(ControlSweepStatus.AWAITING_ARCHIVE, awaiting.status)
        self.assertEqual(ControlSweepStatus.ARCHIVED, reconciled.status)
        self.assertEqual(ControlSweepStatus.IDLE, idle.status)
        self.assertEqual(
            [RunnerOperation.ARCHIVE, RunnerOperation.ARCHIVE_STATUS],
            [call.operation for call in self.transport.calls],
        )

    def test_terminal_branch_delete_receipt_loss_reconciles_without_issue_closure(self) -> None:
        tracker = _LostBranchDeleteReceiptTracker()
        task = replace(
            claimed_task(),
            state=TaskState.COMPLETED,
            labels=("agent:completed", "exec:ssh-cli", "priority:p1"),
        )
        tracker.tasks[task.task_id] = task
        item = self.dispatch.resolve_and_prepare(
            claimed_task(), base_sha=BASE_SHA, source_bundle=_bundle()
        )
        head_sha = "d" * 40
        self.store.record_published_sha(
            item.work_item_id, previous_sha=BASE_SHA, head_sha=head_sha
        )
        self.store.update_work_item_state(item.work_item_id, WorkItemState.RUNNING)
        item = self.store.update_work_item_state(item.work_item_id, WorkItemState.REVIEW)
        item = self.store.bind_draft_pr(item.work_item_id, 7)
        item = self.store.update_work_item_state(
            item.work_item_id,
            WorkItemState.COMPLETED,
            updated_at="2026-01-01T00:00:00+00:00",
        )
        tracker.pull_requests[(item.repository, item.task_branch)] = PullRequest(
            number=7,
            url="https://github.com/owner/repo/pull/7",
            branch_name=item.task_branch,
            title="Codex work",
            is_draft=False,
            base_branch=item.base_branch,
            state=PullRequestState.MERGED,
            head_sha=head_sha,
        )
        tracker.branches[(item.repository, item.task_branch)] = head_sha
        archive_request = RunnerRequest(
            RunnerOperation.ARCHIVE,
            item.work_item_id,
            version=NEXT_PROTOCOL_VERSION,
            expected_head_sha=head_sha,
        )
        self.store.prepare_work_item_archive(
            item.work_item_id,
            expected_head_sha=head_sha,
            eligible_at="2026-01-01T00:00:00+00:00",
            request_sha256=sha256(
                archive_request.to_json().encode("utf-8")
            ).hexdigest(),
        )
        self.store.record_work_item_absence_reconciliation(
            item.work_item_id,
            expected_head_sha=head_sha,
            evidence_sha256="e" * 64,
            observed_by="operator",
            observed_at="2026-01-01T00:00:01+00:00",
        )
        config = replace(
            make_config(global_max_active=4),
            ssh_runtime=SimpleNamespace(
                completed_retention_seconds=None,
                terminal_branch_retention_seconds=10 * 365 * 24 * 60 * 60,
            ),
        )
        target = TerminalBranchCleanupFixtureTarget(item.work_item_id)
        sweep = self._sweep(
            tracker,
            _RecordingSource(),
            config=config,
            terminal_branch_cleanup_fixture_target=target,
        )

        with self.assertRaisesRegex(RuntimeError, "lost branch"):
            sweep.run_terminal_branch_cleanup_fixture_once()
        prepared = self.store.get_terminal_branch_cleanup(item.work_item_id)
        reconciled = sweep.run_terminal_branch_cleanup_fixture_once()
        ordinary = self._sweep(tracker, _RecordingSource(), config=config)
        idle = ordinary.run_once()

        self.assertIsNotNone(prepared)
        self.assertIs(TerminalBranchCleanupState.PREPARED, prepared.state)
        self.assertEqual(ControlSweepStatus.BRANCH_CLEANED, reconciled.status)
        self.assertEqual("reconciled_absent", reconciled.reason)
        cleanup = self.store.get_terminal_branch_cleanup(item.work_item_id)
        self.assertIs(TerminalBranchCleanupState.COMPLETED, cleanup.state)
        self.assertIs(TerminalBranchCleanupOutcome.RECONCILED_ABSENT, cleanup.outcome)
        self.assertEqual(ControlSweepStatus.IDLE, idle.status)
        self.assertTrue(tracker.tasks[task.task_id].is_open)
        self.assertIs(TaskState.COMPLETED, tracker.tasks[task.task_id].state)

    def test_disposed_archive_rechecks_pr_immediately_before_runner_call(self) -> None:
        class MergeRaceTracker(FakeTracker):
            def __init__(self, before: PullRequest, after: PullRequest) -> None:
                super().__init__()
                self._before = before
                self._after = after
                self._reads = 0

            def find_pr_by_branch(
                self, repository: str, branch_name: str
            ) -> PullRequest | None:
                self._record("find_pr_by_branch", repository, branch_name)
                self._reads += 1
                return self._before if self._reads == 1 else self._after

        source = _RecordingSource()
        item = self.dispatch.resolve_and_prepare(
            claimed_task(),
            base_sha=BASE_SHA,
            source_bundle=_bundle(),
        )
        self.store.update_work_item_state(item.work_item_id, WorkItemState.RUNNING)
        head_sha = "d" * 40
        self.store.record_published_sha(
            item.work_item_id,
            previous_sha=BASE_SHA,
            head_sha=head_sha,
        )
        item = self.store.update_work_item_state(
            item.work_item_id, WorkItemState.REVIEW
        )
        item = self.store.bind_draft_pr(item.work_item_id, 7)
        self.store.record_work_item_disposition(
            item.work_item_id,
            kind=WorkItemDispositionKind.SUPERSEDED,
            expected_head_sha=head_sha,
            pr_number=7,
            requested_by="alice",
            request_event_id="7001",
            requested_at="2026-08-23T01:00:00Z",
            reason_code="operator_agent_discard",
        )
        open_pr = PullRequest(
            7,
            "https://github.com/owner/repo/pull/7",
            item.task_branch,
            "Superseded fixture",
            True,
            item.base_branch,
            PullRequestState.OPEN,
            False,
            head_sha,
        )
        tracker = MergeRaceTracker(
            open_pr, replace(open_pr, state=PullRequestState.MERGED)
        )
        tracker.tasks[str(item.issue_number)] = replace(
            claimed_task(),
            state=TaskState.DISCARD,
            labels=("agent:discard", "exec:ssh-cli", "priority:p1"),
            state_approved_by="alice",
            state_approval_event_id="7001",
            state_approved_at="2026-08-23T01:00:00Z",
        )
        self.transport.calls.clear()

        result = self._sweep(tracker, source).run_once()

        self.assertEqual(ControlSweepStatus.BLOCKED, result.status)
        self.assertEqual(
            "disposed_pull_request_merged_after_authorization", result.reason
        )
        self.assertEqual([], self.transport.calls)

    def test_lost_completion_comment_receipt_retries_projection_only(self) -> None:
        tracker = _InterruptingDeliveryTracker(interrupt_comment_once=True)
        task = replace(
            claimed_task(),
            state=TaskState.REVIEW,
            labels=("agent:review", "exec:ssh-cli", "priority:p1"),
        )
        tracker.tasks[task.task_id] = task
        source = _RecordingSource()
        item = self.dispatch.resolve_and_prepare(
            claimed_task(),
            base_sha=BASE_SHA,
            source_bundle=_bundle(),
        )
        self.store.update_work_item_state(item.work_item_id, WorkItemState.RUNNING)
        head_sha = "d" * 40
        self.store.record_published_sha(
            item.work_item_id,
            previous_sha=BASE_SHA,
            head_sha=head_sha,
        )
        item = self.store.update_work_item_state(
            item.work_item_id,
            WorkItemState.REVIEW,
        )
        item = self.store.bind_draft_pr(item.work_item_id, 7)
        tracker.pull_requests[(item.repository, item.task_branch)] = PullRequest(
            number=7,
            url="https://github.com/owner/repo/pull/7",
            branch_name=item.task_branch,
            title="Codex work",
            is_draft=False,
            base_branch=item.base_branch,
            state=PullRequestState.MERGED,
            head_sha=head_sha,
        )
        self.transport.calls.clear()
        delivery = GitHubDeliveryCoordinator(store=self.store, tracker=tracker)
        sweep = self._sweep(tracker, source, delivery=delivery)

        with self.assertRaisesRegex(RuntimeError, "lost Issue comment receipt"):
            sweep.run_once()

        interrupted = self.store.get_work_item(item.work_item_id)
        assert interrupted is not None
        self.assertEqual(WorkItemState.COMPLETED, interrupted.state)
        self.assertEqual(TaskState.REVIEW, tracker.tasks[task.task_id].state)

        recovered = sweep.run_once()

        self.assertEqual(ControlSweepStatus.STATE_SYNCHRONIZED, recovered.status)
        self.assertEqual(TaskState.COMPLETED, tracker.tasks[task.task_id].state)
        self.assertEqual([], source.calls)
        self.assertEqual([], self.transport.calls)
        self.assertEqual(
            2,
            sum(call.method == "upsert_run_comment" for call in tracker.calls),
        )
        self.assertEqual(
            0,
            sum(call.method == "create_draft_pr" for call in tracker.calls),
        )

    def test_lost_completion_label_receipt_is_confirmed_without_other_work(self) -> None:
        tracker = _InterruptingDeliveryTracker(interrupt_state_once=True)
        task = replace(
            claimed_task(),
            state=TaskState.REVIEW,
            labels=("agent:review", "exec:ssh-cli", "priority:p1"),
        )
        tracker.tasks[task.task_id] = task
        source = _RecordingSource()
        item = self.dispatch.resolve_and_prepare(
            claimed_task(),
            base_sha=BASE_SHA,
            source_bundle=_bundle(),
        )
        self.store.update_work_item_state(item.work_item_id, WorkItemState.RUNNING)
        head_sha = "d" * 40
        self.store.record_published_sha(
            item.work_item_id,
            previous_sha=BASE_SHA,
            head_sha=head_sha,
        )
        item = self.store.update_work_item_state(
            item.work_item_id,
            WorkItemState.REVIEW,
        )
        item = self.store.bind_draft_pr(item.work_item_id, 7)
        tracker.pull_requests[(item.repository, item.task_branch)] = PullRequest(
            number=7,
            url="https://github.com/owner/repo/pull/7",
            branch_name=item.task_branch,
            title="Codex work",
            is_draft=False,
            base_branch=item.base_branch,
            state=PullRequestState.MERGED,
            head_sha=head_sha,
        )
        self.transport.calls.clear()
        delivery = GitHubDeliveryCoordinator(store=self.store, tracker=tracker)
        sweep = self._sweep(tracker, source, delivery=delivery)

        with self.assertRaisesRegex(RuntimeError, "lost Issue label receipt"):
            sweep.run_once()

        completed = self.store.get_work_item(item.work_item_id)
        assert completed is not None
        self.assertEqual(WorkItemState.COMPLETED, completed.state)
        self.assertEqual(TaskState.COMPLETED, tracker.tasks[task.task_id].state)
        writes_before = tuple(
            call
            for call in tracker.calls
            if call.method in {"upsert_run_comment", "set_state"}
        )

        idle = sweep.run_once()

        self.assertEqual(ControlSweepStatus.IDLE, idle.status)
        self.assertEqual(
            writes_before,
            tuple(
                call
                for call in tracker.calls
                if call.method in {"upsert_run_comment", "set_state"}
            ),
        )
        self.assertEqual([], source.calls)
        self.assertEqual([], self.transport.calls)
        self.assertEqual(
            1,
            sum(call.method == "upsert_run_comment" for call in tracker.calls),
        )
        self.assertEqual(
            1,
            sum(
                call.method == "set_state"
                and call.args[-1] is TaskState.COMPLETED
                for call in tracker.calls
            ),
        )

    def test_interrupted_prepare_retries_exact_persisted_source(self) -> None:
        tracker = FakeTracker()
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        source = _RecordingSource()
        self.transport.interrupt_next(RunnerOperation.PREPARE)

        first = self._sweep(tracker, source).run_once(turn_id=TURN_ID)

        self.assertEqual(ControlSweepStatus.RETRY, first.status)
        item = self.store.get_work_item_by_issue(task.repository, task.issue_number)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(WorkItemState.PREPARING, item.state)
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, _blocked_result()),
        )

        second = self._sweep(tracker, source).run_once(turn_id=TURN_ID)

        self.assertEqual(ControlSweepStatus.BLOCKED, second.status)
        self.assertEqual(
            [
                ("current", "owner/repo", "main"),
                ("exact", "owner/repo", BASE_SHA),
            ],
            source.calls,
        )
        self.assertEqual(
            [
                RunnerOperation.PREPARE,
                RunnerOperation.PREPARE,
                RunnerOperation.START,
            ],
            [call.operation for call in self.transport.calls],
        )

    def test_rejected_prepare_reactivation_retries_prepare_before_start(self) -> None:
        tracker = FakeTracker()
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        source = _RecordingSource()
        self.transport.reject_next(RunnerOperation.PREPARE)
        sweep = self._sweep(tracker, source)

        rejected = sweep.run_once(turn_id=TURN_ID)

        self.assertEqual(ControlSweepStatus.BLOCKED, rejected.status)
        item = self.store.get_work_item_by_issue(task.repository, task.issue_number)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(WorkItemState.BLOCKED, item.state)
        self.assertFalse(
            self.store.runner_preparation_was_acknowledged(item.work_item_id)
        )
        self.assertEqual((), self.store.list_turns(item.work_item_id))

        followup = replace(
            tracker.tasks[task.task_id],
            state=TaskState.READY,
            labels=("agent:ready", "exec:ssh-cli", "priority:p1"),
            ready_approved_by="alice",
            updated_at="2026-08-13T02:00:00Z",
        )
        tracker.ready_tasks = (followup,)
        tracker.tasks[task.task_id] = followup
        claim_count = sum(call.method == "claim" for call in tracker.calls)
        with self.assertRaisesRegex(RuntimeError, "exact source refresh failed"):
            self._sweep(tracker, _FailingExactSource()).run_once(turn_id=TURN_ID)
        self.assertEqual(
            claim_count,
            sum(call.method == "claim" for call in tracker.calls),
        )
        still_blocked = self.store.get_work_item(item.work_item_id)
        self.assertIsNotNone(still_blocked)
        assert still_blocked is not None
        self.assertEqual(WorkItemState.BLOCKED, still_blocked.state)
        self.assertEqual(
            [RunnerOperation.PREPARE],
            [call.operation for call in self.transport.calls],
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, _blocked_result()),
        )

        retried = sweep.run_once(turn_id=TURN_ID)

        self.assertEqual(ControlSweepStatus.BLOCKED, retried.status)
        self.assertTrue(
            self.store.runner_preparation_was_acknowledged(item.work_item_id)
        )
        self.assertEqual(
            [
                RunnerOperation.PREPARE,
                RunnerOperation.PREPARE,
                RunnerOperation.START,
            ],
            [call.operation for call in self.transport.calls],
        )
        self.assertEqual(
            [
                ("current", "owner/repo", "main"),
                ("exact", "owner/repo", BASE_SHA),
            ],
            source.calls,
        )

        turn_followup = replace(
            tracker.tasks[task.task_id],
            state=TaskState.READY,
            labels=("agent:ready", "exec:ssh-cli", "priority:p1"),
            ready_approved_by="alice",
            updated_at="2026-08-13T03:00:00Z",
        )
        tracker.ready_tasks = (turn_followup,)
        tracker.tasks[task.task_id] = turn_followup
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, _blocked_result()),
        )

        blocked_turn_reactivation = sweep.run_once(turn_id="turn_" + "2" * 32)

        self.assertEqual(ControlSweepStatus.BLOCKED, blocked_turn_reactivation.status)
        self.assertEqual(
            [
                RunnerOperation.PREPARE,
                RunnerOperation.PREPARE,
                RunnerOperation.START,
                RunnerOperation.RESUME,
            ],
            [call.operation for call in self.transport.calls],
        )
        self.assertEqual(
            [
                ("current", "owner/repo", "main"),
                ("exact", "owner/repo", BASE_SHA),
            ],
            source.calls,
        )

    def test_operator_reactivation_resumes_pending_fresh_audit(self) -> None:
        tracker = FakeTracker()
        task = claimed_task()
        tracker.tasks[task.task_id] = task
        item = self.dispatch.resolve_and_prepare(
            task,
            base_sha=BASE_SHA,
            source_bundle=_bundle(),
        )
        source_turn = Turn(
            turn_id="turn_" + "8" * 32,
            work_item_id=item.work_item_id,
            turn_number=1,
            state=TurnState.FINISHED,
            issue_revision=task.updated_at,
            prompt_sha256="8" * 64,
            input_head_sha=BASE_SHA,
            output_sha256="9" * 64,
            output_head_sha=BASE_SHA,
            result_status="completed",
            result_summary="passed implementation candidate",
            created_at="2026-08-13T00:00:00Z",
            updated_at="2026-08-13T00:00:01Z",
            finished_at="2026-08-13T00:00:01Z",
        )
        audit_turn = Turn(
            turn_id="turn_" + "9" * 32,
            work_item_id=item.work_item_id,
            turn_number=2,
            state=TurnState.NEEDS_INPUT,
            issue_revision=task.updated_at,
            prompt_sha256="a" * 64,
            input_head_sha=BASE_SHA,
            output_sha256="b" * 64,
            output_head_sha=BASE_SHA,
            result_status="needs_input",
            result_summary="audit requested input",
            created_at="2026-08-13T00:00:02Z",
            updated_at="2026-08-13T00:00:03Z",
            finished_at="2026-08-13T00:00:03Z",
        )

        class ReactivatingDispatch:
            def __init__(inner_self):
                inner_self.audit_calls = 0

            def resolve_and_prepare(inner_self, *args, **kwargs):
                return item

            def requires_fresh_final_audit(inner_self, turn_id):
                self.assertEqual(source_turn.turn_id, turn_id)
                return True

            def run_fresh_final_audit(inner_self, *args, **kwargs):
                inner_self.audit_calls += 1
                return TurnProgress(item, audit_turn)

            def run_claimed_turn(inner_self, *args, **kwargs):
                self.fail("implementation Turn must not resume after audit reactivation")

        dispatch = ReactivatingDispatch()
        sweep = SshControlSweep(
            config=make_config(global_max_active=4),
            store=self.store,
            tracker=tracker,
            dispatch=dispatch,  # type: ignore[arg-type]
            source=_RecordingSource(),
            process_lock=DispatcherProcessLock(self.lock_path),
        )
        with patch.object(self.store, "list_turns", return_value=(source_turn,)):
            result = sweep._prepare_and_run(
                task,
                base_sha=BASE_SHA,
                source_bundle=None,
                turn_id=audit_turn.turn_id,
            )

        self.assertEqual(ControlSweepStatus.NEEDS_INPUT, result.status)
        self.assertEqual(1, dispatch.audit_calls)

    def test_interrupted_start_is_reconciled_without_prompt_replay(self) -> None:
        tracker = FakeTracker()
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        source = _RecordingSource()
        work_item_id = self._expected_work_item_id(task)
        self.transport.queue_turn(
            work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, _blocked_result()),
        )
        self.transport.interrupt_next(RunnerOperation.START)

        first = self._sweep(tracker, source).run_once(turn_id=TURN_ID)
        second = self._sweep(tracker, source).run_once()

        self.assertEqual(ControlSweepStatus.RUNNER_ACTIVE, first.status)
        self.assertEqual(ControlSweepStatus.BLOCKED, second.status)
        self.assertEqual(
            [RunnerOperation.PREPARE, RunnerOperation.START, RunnerOperation.STATUS],
            [call.operation for call in self.transport.calls],
        )
        self.assertEqual(TaskState.BLOCKED, tracker.tasks[task.task_id].state)

    def test_changing_issue_snapshot_retries_without_starting_turn(self) -> None:
        task = _ready_task()
        claimed = replace(
            task,
            state=TaskState.DISPATCHING,
            labels=("agent:dispatching", "exec:ssh-cli", "priority:p1"),
        )
        tracker = _ChangingSnapshotTracker(
            (
                replace(claimed, updated_at="2026-08-13T01:01:00Z"),
                replace(claimed, updated_at="2026-08-13T01:02:00Z"),
            )
        )
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task

        result = self._sweep(tracker, _RecordingSource()).run_once(turn_id=TURN_ID)

        self.assertEqual(ControlSweepStatus.RETRY, result.status)
        self.assertEqual("issue_snapshot_changed_during_freeze", result.reason)
        self.assertIsNone(self.store.get_active_turn())
        self.assertEqual(
            [RunnerOperation.PREPARE],
            [call.operation for call in self.transport.calls],
        )

    def test_continuously_changing_comments_retry_without_starting_turn(self) -> None:
        task = _ready_task()
        comments = tuple(
            (
                TrackerComment(
                    "C1",
                    "alice",
                    f"/codex-context revision {revision}",
                    "2026-08-13T01:00:00Z",
                    f"2026-08-13T01:0{revision}:00Z",
                ),
            )
            for revision in range(1, 5)
        )
        tracker = _ChangingCommentsTracker(comments)
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task

        result = self._sweep(tracker, _RecordingSource()).run_once(turn_id=TURN_ID)

        self.assertEqual(ControlSweepStatus.RETRY, result.status)
        self.assertEqual("issue_snapshot_changed_during_freeze", result.reason)
        self.assertIsNone(self.store.get_active_turn())
        self.assertEqual(
            [RunnerOperation.PREPARE],
            [call.operation for call in self.transport.calls],
        )

    def test_checkpoint_waits_for_publisher_and_recovery_does_not_restart(self) -> None:
        tracker = FakeTracker()
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        source = _RecordingSource()
        work_item_id = self._expected_work_item_id(task)
        self.transport.queue_turn(
            work_item_id,
            FakeTurnFixture(SESSION, "b" * 40, _blocked_result()),
        )

        first = self._sweep(tracker, source).run_once(turn_id=TURN_ID)
        second = self._sweep(tracker, source).run_once()

        self.assertEqual(ControlSweepStatus.AWAITING_PUBLICATION, first.status)
        self.assertEqual(ControlSweepStatus.AWAITING_PUBLICATION, second.status)
        active = self.store.get_active_turn()
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(TurnState.CHECKPOINTING, active.state)
        self.assertEqual(TaskState.RUNNING, tracker.tasks[task.task_id].state)
        self.assertEqual(
            [RunnerOperation.PREPARE, RunnerOperation.START],
            [call.operation for call in self.transport.calls],
        )

    def test_checkpoint_is_published_and_finalized_in_the_same_sweep(self) -> None:
        tracker = FakeTracker()
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        artifact = b"fixture-result-bundle"
        head_sha = "b" * 40
        self._register_checkpoint(artifact, head_sha)
        work_item_id = self._expected_work_item_id(task)
        self.transport.queue_turn(
            work_item_id,
            FakeTurnFixture(
                SESSION,
                head_sha,
                _completed_result(),
                artifact,
            ),
        )
        publisher = _RecordingPublisher()
        delivery = GitHubDeliveryCoordinator(store=self.store, tracker=tracker)
        slack_publisher = _InterruptingSlackPublisher()
        slack_delivery = SlackDeliveryCoordinator(
            store=self.store,
            publisher=slack_publisher,
            channel_id="C0BR2D0MS8Y",
        )

        result = self._sweep(
            tracker,
            _RecordingSource(),
            publisher=publisher,
            delivery=delivery,
            slack_delivery=slack_delivery,
        ).run_once(turn_id=TURN_ID)

        self.assertEqual(ControlSweepStatus.REVIEW, result.status)
        self.assertEqual(TaskState.REVIEW, tracker.tasks[task.task_id].state)
        self.assertIsNone(self.store.get_active_turn())
        self.assertEqual(1, len(publisher.calls))
        self.assertEqual(artifact, publisher.calls[0][0])
        self.assertEqual(
            [RunnerOperation.PREPARE, RunnerOperation.START, RunnerOperation.EXPORT],
            [call.operation for call in self.transport.calls],
        )
        item = self.store.get_work_item(work_item_id)
        assert item is not None
        self.assertEqual(head_sha, item.last_published_sha)
        self.assertEqual(1, item.pr_number)
        self.assertEqual("1700000000.000001", item.slack_thread_ts)
        self.assertEqual(
            [SlackReportKind.ROOT, SlackReportKind.RESULT],
            [report.kind for report in slack_publisher.calls],
        )
        methods = [call.method for call in tracker.calls]
        comment_writes = [
            index
            for index, method in enumerate(methods)
            if method == "upsert_run_comment"
        ]
        self.assertEqual(2, len(comment_writes))
        self.assertLess(
            comment_writes[0],
            methods.index("create_draft_pr"),
        )
        self.assertLess(
            methods.index("create_draft_pr"),
            comment_writes[-1],
        )
        review_write = next(
            index
            for index, call in enumerate(tracker.calls)
            if call.method == "set_state" and call.args[-1] is TaskState.REVIEW
        )
        self.assertLess(comment_writes[-1], review_write)
        issue_comment = next(
            call for call in tracker.calls if call.method == "upsert_run_comment"
        )
        self.assertIn("fixture.slack.com", issue_comment.args[-1])

    def test_completed_v2_checkpoint_binds_draft_pr_before_ci_evaluation(self) -> None:
        tracker = FakeTracker()
        task = claimed_task()
        tracker.tasks[task.task_id] = task
        item = self.dispatch.resolve_and_prepare(
            task,
            base_sha=BASE_SHA,
            source_bundle=_bundle(),
        )
        item = self.store.update_work_item_state(
            item.work_item_id, WorkItemState.RUNNING
        )
        head_sha = "e" * 40
        self.store.record_published_sha(
            item.work_item_id,
            previous_sha=BASE_SHA,
            head_sha=head_sha,
        )
        item = self.store.get_work_item(item.work_item_id)
        assert item is not None
        turn = Turn(
            turn_id=TURN_ID,
            work_item_id=item.work_item_id,
            turn_number=1,
            state=TurnState.PUBLISHED,
            issue_revision=task.updated_at,
            prompt_sha256="f" * 64,
            input_head_sha=BASE_SHA,
            output_sha256="a" * 64,
            output_head_sha=head_sha,
            result_status="completed",
            result_summary="completion candidate",
            created_at="2026-08-13T00:00:00Z",
            updated_at="2026-08-13T00:00:01Z",
        )

        class PendingDispatch:
            def evaluate_completion_gate(inner_self, observed_task, turn_id):
                persisted = self.store.get_work_item(item.work_item_id)
                assert persisted is not None
                self.assertEqual(1, persisted.pr_number)
                self.assertEqual(TURN_ID, turn_id)
                self.assertEqual(task, observed_task)
                return TurnProgress(persisted, turn)

        delivery = GitHubDeliveryCoordinator(store=self.store, tracker=tracker)
        sweep = SshControlSweep(
            config=make_config(global_max_active=4),
            store=self.store,
            tracker=tracker,
            dispatch=PendingDispatch(),  # type: ignore[arg-type]
            source=_RecordingSource(),
            process_lock=DispatcherProcessLock(self.lock_path),
            delivery=delivery,
        )

        with patch.object(
            self.store,
            "get_turn_agent_result",
            return_value=_completed_result(),
        ):
            result = sweep._after_turn(task, TurnProgress(item, turn))

        self.assertEqual(ControlSweepStatus.AWAITING_COMPLETION, result.status)
        self.assertEqual(TaskState.RUNNING, tracker.tasks[task.task_id].state)
        self.assertEqual(
            1,
            sum(call.method == "create_draft_pr" for call in tracker.calls),
        )

    def test_lost_slack_root_receipt_recovers_before_starting_codex(self) -> None:
        tracker = FakeTracker()
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        work_item_id = self._expected_work_item_id(task)
        self.transport.queue_turn(
            work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, _blocked_result()),
        )
        slack_publisher = _InterruptingSlackPublisher(
            interrupt_kind_once=SlackReportKind.ROOT
        )
        slack_delivery = SlackDeliveryCoordinator(
            store=self.store,
            publisher=slack_publisher,
            channel_id="C0BR2D0MS8Y",
        )
        sweep = self._sweep(
            tracker,
            _RecordingSource(),
            slack_delivery=slack_delivery,
        )

        with self.assertRaisesRegex(RuntimeError, "lost Slack receipt"):
            sweep.run_once(turn_id=TURN_ID)

        self.assertEqual(
            [RunnerOperation.PREPARE],
            [call.operation for call in self.transport.calls],
        )
        recovered = sweep.run_once(turn_id=TURN_ID)

        self.assertEqual(ControlSweepStatus.BLOCKED, recovered.status)
        self.assertEqual(
            [RunnerOperation.PREPARE, RunnerOperation.START],
            [call.operation for call in self.transport.calls],
        )
        self.assertEqual(2, len(slack_publisher.messages))
        self.assertEqual(
            [SlackReportKind.ROOT, SlackReportKind.ROOT, SlackReportKind.FAILURE],
            [report.kind for report in slack_publisher.calls],
        )

    def test_lost_slack_terminal_receipt_recovers_before_issue_label(self) -> None:
        tracker = FakeTracker()
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        artifact = b"fixture-lost-slack-terminal"
        head_sha = "b" * 40
        self._register_checkpoint(artifact, head_sha)
        work_item_id = self._expected_work_item_id(task)
        self.transport.queue_turn(
            work_item_id,
            FakeTurnFixture(SESSION, head_sha, _completed_result(), artifact),
        )
        publisher = _RecordingPublisher()
        delivery = GitHubDeliveryCoordinator(store=self.store, tracker=tracker)
        slack_publisher = _InterruptingSlackPublisher(
            interrupt_kind_once=SlackReportKind.RESULT
        )
        slack_delivery = SlackDeliveryCoordinator(
            store=self.store,
            publisher=slack_publisher,
            channel_id="C0BR2D0MS8Y",
        )
        sweep = self._sweep(
            tracker,
            _RecordingSource(),
            publisher=publisher,
            delivery=delivery,
            slack_delivery=slack_delivery,
        )

        with self.assertRaisesRegex(RuntimeError, "lost Slack receipt"):
            sweep.run_once(turn_id=TURN_ID)

        interrupted = self.store.get_work_item(work_item_id)
        assert interrupted is not None
        self.assertEqual(WorkItemState.REVIEW, interrupted.state)
        self.assertEqual(1, interrupted.pr_number)
        self.assertEqual(TaskState.DISPATCHING, tracker.tasks[task.task_id].state)
        runner_calls = tuple(self.transport.calls)

        recovered = sweep.run_once()

        self.assertEqual(ControlSweepStatus.STATE_SYNCHRONIZED, recovered.status)
        self.assertEqual(TaskState.REVIEW, tracker.tasks[task.task_id].state)
        self.assertEqual(runner_calls, tuple(self.transport.calls))
        self.assertEqual(1, len(publisher.calls))
        self.assertEqual(2, len(slack_publisher.messages))
        self.assertEqual(3, len(slack_publisher.calls))
        self.assertEqual(
            1,
            sum(call.method == "create_draft_pr" for call in tracker.calls),
        )

    def test_lost_draft_pr_receipt_is_recovered_without_reexecution(self) -> None:
        tracker = _InterruptingDeliveryTracker(interrupt_create_once=True)
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        artifact = b"fixture-lost-pr-receipt"
        head_sha = "b" * 40
        self._register_checkpoint(artifact, head_sha)
        work_item_id = self._expected_work_item_id(task)
        self.transport.queue_turn(
            work_item_id,
            FakeTurnFixture(SESSION, head_sha, _completed_result(), artifact),
        )
        publisher = _RecordingPublisher()
        delivery = GitHubDeliveryCoordinator(store=self.store, tracker=tracker)
        sweep = self._sweep(
            tracker,
            _RecordingSource(),
            publisher=publisher,
            delivery=delivery,
        )

        with self.assertRaisesRegex(RuntimeError, "lost Draft PR receipt"):
            sweep.run_once(turn_id=TURN_ID)

        interrupted = self.store.get_work_item(work_item_id)
        assert interrupted is not None
        self.assertEqual(WorkItemState.REVIEW, interrupted.state)
        self.assertEqual(head_sha, interrupted.last_published_sha)
        self.assertIsNone(interrupted.pr_number)
        self.assertEqual(TaskState.DISPATCHING, tracker.tasks[task.task_id].state)
        runner_calls = tuple(self.transport.calls)

        recovered = sweep.run_once()

        self.assertEqual(ControlSweepStatus.STATE_SYNCHRONIZED, recovered.status)
        self.assertEqual(TaskState.REVIEW, tracker.tasks[task.task_id].state)
        self.assertEqual(runner_calls, tuple(self.transport.calls))
        self.assertEqual(1, len(publisher.calls))
        self.assertEqual(
            1,
            sum(call.method == "create_draft_pr" for call in tracker.calls),
        )
        persisted = self.store.get_work_item(work_item_id)
        assert persisted is not None
        self.assertEqual(1, persisted.pr_number)

    def test_lost_publisher_then_pr_receipts_recover_a_running_issue(self) -> None:
        tracker = _InterruptingDeliveryTracker(interrupt_create_once=True)
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        artifact = b"fixture-lost-publisher-then-pr"
        head_sha = "b" * 40
        self._register_checkpoint(artifact, head_sha)
        work_item_id = self._expected_work_item_id(task)
        self.transport.queue_turn(
            work_item_id,
            FakeTurnFixture(SESSION, head_sha, _completed_result(), artifact),
        )
        publisher = _RecordingPublisher(interrupt_once=True)
        delivery = GitHubDeliveryCoordinator(store=self.store, tracker=tracker)
        sweep = self._sweep(
            tracker,
            _RecordingSource(),
            publisher=publisher,
            delivery=delivery,
        )

        interrupted_publish = sweep.run_once(turn_id=TURN_ID)

        self.assertEqual(
            ControlSweepStatus.AWAITING_PUBLICATION,
            interrupted_publish.status,
        )
        self.assertEqual(TaskState.RUNNING, tracker.tasks[task.task_id].state)

        with self.assertRaisesRegex(RuntimeError, "lost Draft PR receipt"):
            sweep.run_once()

        interrupted_pr = self.store.get_work_item(work_item_id)
        assert interrupted_pr is not None
        self.assertEqual(WorkItemState.REVIEW, interrupted_pr.state)
        self.assertEqual(head_sha, interrupted_pr.last_published_sha)
        self.assertIsNone(interrupted_pr.pr_number)
        self.assertEqual(TaskState.RUNNING, tracker.tasks[task.task_id].state)

        recovered = sweep.run_once()

        self.assertEqual(ControlSweepStatus.STATE_SYNCHRONIZED, recovered.status)
        self.assertEqual(TaskState.REVIEW, tracker.tasks[task.task_id].state)
        self.assertEqual(
            [
                RunnerOperation.PREPARE,
                RunnerOperation.START,
                RunnerOperation.EXPORT,
                RunnerOperation.EXPORT,
            ],
            [call.operation for call in self.transport.calls],
        )
        self.assertEqual(2, len(publisher.calls))
        self.assertEqual(
            1,
            sum(call.method == "create_draft_pr" for call in tracker.calls),
        )
        persisted = self.store.get_work_item(work_item_id)
        assert persisted is not None
        self.assertEqual(1, persisted.pr_number)

    def test_issue_state_waits_for_recoverable_comment_delivery(self) -> None:
        tracker = _InterruptingDeliveryTracker(interrupt_comment_once=True)
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        artifact = b"fixture-lost-comment-receipt"
        head_sha = "b" * 40
        self._register_checkpoint(artifact, head_sha)
        work_item_id = self._expected_work_item_id(task)
        self.transport.queue_turn(
            work_item_id,
            FakeTurnFixture(SESSION, head_sha, _completed_result(), artifact),
        )
        publisher = _RecordingPublisher()
        delivery = GitHubDeliveryCoordinator(store=self.store, tracker=tracker)
        sweep = self._sweep(
            tracker,
            _RecordingSource(),
            publisher=publisher,
            delivery=delivery,
        )

        with self.assertRaisesRegex(RuntimeError, "lost Issue comment receipt"):
            sweep.run_once(turn_id=TURN_ID)

        interrupted = self.store.get_work_item(work_item_id)
        assert interrupted is not None
        self.assertEqual(1, interrupted.pr_number)
        self.assertEqual(TaskState.DISPATCHING, tracker.tasks[task.task_id].state)
        runner_calls = tuple(self.transport.calls)

        recovered = sweep.run_once()

        self.assertEqual(ControlSweepStatus.STATE_SYNCHRONIZED, recovered.status)
        self.assertEqual(TaskState.REVIEW, tracker.tasks[task.task_id].state)
        self.assertEqual(runner_calls, tuple(self.transport.calls))
        self.assertEqual(1, len(publisher.calls))
        self.assertEqual(
            1,
            sum(call.method == "create_draft_pr" for call in tracker.calls),
        )
        self.assertEqual(
            2,
            sum(call.method == "upsert_run_comment" for call in tracker.calls),
        )

    def test_ambiguous_publish_retries_without_restarting_codex(self) -> None:
        tracker = FakeTracker()
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        artifact = b"fixture-ambiguous-bundle"
        head_sha = "b" * 40
        self._register_checkpoint(artifact, head_sha)
        work_item_id = self._expected_work_item_id(task)
        self.transport.queue_turn(
            work_item_id,
            FakeTurnFixture(SESSION, head_sha, _completed_result(), artifact),
        )
        publisher = _RecordingPublisher(interrupt_once=True)
        sweep = self._sweep(tracker, _RecordingSource(), publisher=publisher)

        first = sweep.run_once(turn_id=TURN_ID)
        second = sweep.run_once()

        self.assertEqual(ControlSweepStatus.AWAITING_PUBLICATION, first.status)
        self.assertEqual("publication_outcome_ambiguous", first.reason)
        self.assertEqual(ControlSweepStatus.REVIEW, second.status)
        self.assertEqual(2, len(publisher.calls))
        self.assertEqual(
            [
                RunnerOperation.PREPARE,
                RunnerOperation.START,
                RunnerOperation.EXPORT,
                RunnerOperation.EXPORT,
            ],
            [call.operation for call in self.transport.calls],
        )

    def test_recorded_publish_recovers_without_export_or_repush(self) -> None:
        tracker = FakeTracker()
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        artifact = b"fixture-recorded-bundle"
        head_sha = "b" * 40
        self._register_checkpoint(artifact, head_sha)
        work_item_id = self._expected_work_item_id(task)
        self.transport.queue_turn(
            work_item_id,
            FakeTurnFixture(SESSION, head_sha, _completed_result(), artifact),
        )

        first = self._sweep(tracker, _RecordingSource()).run_once(turn_id=TURN_ID)
        self.assertEqual(ControlSweepStatus.AWAITING_PUBLICATION, first.status)
        self.store.record_published_sha(
            work_item_id,
            previous_sha=BASE_SHA,
            head_sha=head_sha,
        )
        calls_before = tuple(self.transport.calls)
        publisher = _RecordingPublisher()

        recovered = self._sweep(
            tracker, _RecordingSource(), publisher=publisher
        ).run_once()

        self.assertEqual(ControlSweepStatus.REVIEW, recovered.status)
        self.assertEqual((), tuple(publisher.calls))
        self.assertEqual(calls_before, tuple(self.transport.calls))

    def test_issue_policy_change_after_turn_cannot_widen_publication(self) -> None:
        tracker = FakeTracker()
        task = _ready_task()
        tracker.ready_tasks = (task,)
        tracker.tasks[task.task_id] = task
        artifact = b"fixture-policy-bundle"
        head_sha = "b" * 40
        self._register_checkpoint(
            artifact,
            head_sha,
            changed_paths=("src/other.py",),
        )
        work_item_id = self._expected_work_item_id(task)
        self.transport.queue_turn(
            work_item_id,
            FakeTurnFixture(SESSION, head_sha, _completed_result(), artifact),
        )

        first = self._sweep(tracker, _RecordingSource()).run_once(turn_id=TURN_ID)
        self.assertEqual(ControlSweepStatus.AWAITING_PUBLICATION, first.status)
        running = tracker.tasks[task.task_id]
        tracker.tasks[task.task_id] = replace(
            running,
            body=running.body.replace("- src/codex_dispatcher", "- src"),
            updated_at="2026-08-13T02:00:00Z",
        )
        publisher = _RecordingPublisher()

        rejected = self._sweep(
            tracker, _RecordingSource(), publisher=publisher
        ).run_once()

        self.assertEqual(ControlSweepStatus.BLOCKED, rejected.status)
        self.assertEqual([], publisher.calls)
        turn = self.store.get_turn(TURN_ID)
        assert turn is not None
        self.assertEqual("publication_rejected", turn.error_code)

    def test_lost_terminal_tracker_write_is_repaired_before_new_claim(self) -> None:
        tracker = FakeTracker()
        task = claimed_task()
        tracker.tasks[task.task_id] = task
        source = _RecordingSource()
        item = self.dispatch.resolve_and_prepare(
            task,
            base_sha=BASE_SHA,
            source_bundle=_bundle(),
        )
        self.store.update_work_item_state(item.work_item_id, WorkItemState.RUNNING)
        self.store.update_work_item_state(item.work_item_id, WorkItemState.BLOCKED)
        self.transport.calls.clear()

        result = self._sweep(tracker, source).run_once()

        self.assertEqual(ControlSweepStatus.STATE_SYNCHRONIZED, result.status)
        self.assertEqual(TaskState.BLOCKED, tracker.tasks[task.task_id].state)
        self.assertEqual([], source.calls)
        self.assertEqual([], self.transport.calls)

    def test_process_lock_prevents_overlapping_sweeps(self) -> None:
        tracker = FakeTracker()
        source = _RecordingSource()
        owner = DispatcherProcessLock(self.lock_path)
        owner.acquire()
        try:
            with self.assertRaises(DispatcherLockUnavailable):
                self._sweep(tracker, source).run_once()
        finally:
            owner.release()

        self.assertEqual([], tracker.calls)
        self.assertEqual([], source.calls)

    @staticmethod
    def _expected_work_item_id(task: TrackerTask) -> str:
        from codex_dispatcher.work_items import stable_work_item_identity

        assert task.issue_node_id is not None
        return stable_work_item_identity(
            repository=task.repository,
            issue_number=task.issue_number,
            issue_node_id=task.issue_node_id,
        ).work_item_id


if __name__ == "__main__":
    unittest.main()
