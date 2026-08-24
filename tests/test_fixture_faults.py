from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_dispatcher.config import SlackRuntimeConfig
from codex_dispatcher.fixture_fault_cli import _create_backup, _run
from codex_dispatcher.fixture_faults import (
    FIXTURE_REPOSITORY,
    FIXTURE_SLACK_CHANNEL_ID,
    FixtureFaultInjection,
    FixtureFaultPoint,
    FixtureFaultRejected,
    FixtureProcessInterrupted,
    FixtureReceiptLost,
    _is_durable_ssh_start_proof,
    validate_fixture_preflight,
    validate_fixture_slack_config,
)
from codex_dispatcher.git_publisher import (
    GitPublicationInterrupted,
    PublicationReceipt,
)
from codex_dispatcher.publisher import PublicationPlan
from codex_dispatcher.runner_protocol import (
    NEXT_PROTOCOL_VERSION,
    RunnerOperation,
    RunnerRequest,
)
from codex_dispatcher.runner_transport import (
    RunnerTransportInterrupted,
    RunnerTurnRemoteState,
    RunnerTurnReply,
    RunnerWireOutput,
)
from codex_dispatcher.control_sweep import ControlSweepResult, ControlSweepStatus
from codex_dispatcher.ssh_runtime import SshPreflightInspection
from codex_dispatcher.ssh_runner_transport import SshInvocationPlan
from codex_dispatcher.ssh_preflight import SshPreflightPlan, SshPreflightStatus
from codex_dispatcher.ssh_recovery import SshRecoveryAction
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.terminal_retention import (
    TerminalBranchCleanupState,
    terminal_branch_request_sha256,
)
from codex_dispatcher.slack_reporting import (
    SlackDeliveryReceipt,
    SlackDeliveryState,
    SlackReportKind,
    build_slack_report,
)
from codex_dispatcher.testing.fakes import FakeTracker
from codex_dispatcher.trackers.base import (
    DraftPullRequestRequest,
    PullRequest,
    PullRequestState,
    TaskState,
    TrackerTask,
)
from codex_dispatcher.work_items import Turn, TurnState, WorkItem, WorkItemState
from tests.test_scheduler import make_config


ISSUE = 7
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def _task(state: TaskState) -> TrackerTask:
    return TrackerTask(
        repository=FIXTURE_REPOSITORY,
        task_id=str(ISSUE),
        issue_number=ISSUE,
        title="Fixture fault",
        body="fixture",
        state=state,
        labels=(f"agent:{state.value}", "exec:ssh-cli"),
        created_at="2026-08-19T00:00:00Z",
        ready_approved_by="longwdl",
        issue_node_id="I_fixture_fault_7",
        updated_at="2026-08-19T00:00:00Z",
    )


def _running_item() -> WorkItem:
    item = WorkItem.new(
        repository=FIXTURE_REPOSITORY,
        issue_number=ISSUE,
        issue_node_id="I_fixture_fault_7",
        base_branch="main",
        base_sha=BASE_SHA,
        at="2026-08-19T00:00:00Z",
    )
    for state in (
        WorkItemState.PREPARING,
        WorkItemState.READY,
        WorkItemState.RUNNING,
    ):
        item = item.transition_to(state, at="2026-08-19T00:00:00Z")
    return item


def _plan(item: WorkItem) -> PublicationPlan:
    return PublicationPlan(
        work_item_id=item.work_item_id,
        repository=item.repository,
        source_sha=HEAD_SHA,
        target_ref=f"refs/heads/{item.task_branch}",
        expected_remote_sha=None,
        bundle_sha256="c" * 64,
        changed_paths=("README.md",),
        commit_count=1,
        size_bytes=1,
    )


def _completion_comment(item: WorkItem) -> str:
    assert item.last_published_sha is not None
    assert item.pr_number is not None
    return "\n".join(
        (
            f"Codex work item `{item.work_item_id}`",
            "",
            f"- Task branch: `{item.task_branch}`",
            "- Dispatcher state: `agent:completed`",
            f"- Published checkpoint: `{item.last_published_sha}`",
            f"- Pull request: https://github.com/{item.repository}/pull/{item.pr_number}",
        )
    )


def _checkpoint_turn(item: WorkItem) -> Turn:
    turn = Turn.new(
        work_item_id=item.work_item_id,
        turn_number=1,
        issue_revision="revision-1",
        prompt_sha256="d" * 64,
        input_head_sha=BASE_SHA,
        issue_allowed_paths=("README.md",),
        turn_id="turn_" + "1" * 32,
        at="2026-08-19T00:00:00Z",
    )
    return replace(
        turn,
        state=TurnState.CHECKPOINTING,
        output_sha256="e" * 64,
        output_head_sha=HEAD_SHA,
        result_status="completed",
        result_summary="Fixture checkpoint",
        started_at="2026-08-19T00:00:01Z",
        updated_at="2026-08-19T00:00:02Z",
    )


def _reconciling_turn(item: WorkItem) -> Turn:
    turn = Turn.new(
        work_item_id=item.work_item_id,
        turn_number=1,
        issue_revision="revision-1",
        prompt_sha256="d" * 64,
        input_head_sha=BASE_SHA,
        issue_allowed_paths=("README.md",),
        turn_id="turn_" + "2" * 32,
        at="2026-08-19T00:00:00Z",
    )
    return replace(
        turn,
        state=TurnState.RECONCILING,
        started_at="2026-08-19T00:00:01Z",
        updated_at="2026-08-19T00:00:02Z",
    )


class FixtureFaultTests(unittest.TestCase):
    def test_slack_receipt_faults_require_exact_durable_stages(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with StateStore(Path(root) / "state.db") as store:
                store.migrate()
                item = WorkItem.new(
                    repository=FIXTURE_REPOSITORY,
                    issue_number=ISSUE,
                    issue_node_id="I_fixture_fault_7",
                    base_branch="main",
                    base_sha=BASE_SHA,
                    at="2026-08-19T00:00:00Z",
                )
                store.create_work_item(item)
                store.update_work_item_state(
                    item.work_item_id,
                    WorkItemState.PREPARING,
                )
                ready = store.update_work_item_state(
                    item.work_item_id,
                    WorkItemState.READY,
                )
                root_report = build_slack_report(
                    work_item_id=ready.work_item_id,
                    kind=SlackReportKind.ROOT,
                    channel_id=FIXTURE_SLACK_CHANNEL_ID,
                    text="Fixture root",
                )
                store.prepare_slack_delivery(root_report)
                root_receipt = SlackDeliveryReceipt(
                    deduplication_key=root_report.deduplication_key,
                    channel_id=FIXTURE_SLACK_CHANNEL_ID,
                    message_ts="1700000000.000001",
                    thread_ts="1700000000.000001",
                    permalink=(
                        "https://fixture.slack.com/archives/"
                        f"{FIXTURE_SLACK_CHANNEL_ID}/p1700000000000001"
                    ),
                )
                calls = []
                delegate = SimpleNamespace(
                    publish=lambda report: (calls.append(report), root_receipt)[1]
                )
                root_injection = FixtureFaultInjection(
                    FixtureFaultPoint.SLACK_ROOT_RECEIPT,
                    ISSUE,
                )

                with self.assertRaisesRegex(FixtureReceiptLost, "slack-root-receipt"):
                    root_injection.wrap_slack_publisher(
                        delegate,
                        store=store,
                    ).publish(root_report)

                self.assertTrue(root_injection.triggered)
                self.assertEqual(root_receipt, root_injection.slack_receipt)
                self.assertEqual(
                    SlackDeliveryState.PREPARED,
                    store.get_slack_delivery(root_report.deduplication_key).state,
                )
                self.assertEqual([root_report], calls)

                # A terminal fault is permitted to recover only that exact
                # prepared root before it reaches the finished result report.
                terminal_calls = []

                def publish(report):
                    terminal_calls.append(report)
                    if report.kind is SlackReportKind.ROOT:
                        return root_receipt
                    return SlackDeliveryReceipt(
                        deduplication_key=report.deduplication_key,
                        channel_id=FIXTURE_SLACK_CHANNEL_ID,
                        message_ts="1700000000.000002",
                        thread_ts=root_receipt.thread_ts,
                        permalink=(
                            "https://fixture.slack.com/archives/"
                            f"{FIXTURE_SLACK_CHANNEL_ID}/p1700000000000002"
                            "?thread_ts=1700000000.000001&"
                            f"cid={FIXTURE_SLACK_CHANNEL_ID}"
                        ),
                    )

                terminal_injection = FixtureFaultInjection(
                    FixtureFaultPoint.SLACK_TERMINAL_RECEIPT,
                    ISSUE,
                )
                terminal_publisher = terminal_injection.wrap_slack_publisher(
                    SimpleNamespace(publish=publish),
                    store=store,
                )
                self.assertEqual(root_receipt, terminal_publisher.publish(root_report))
                self.assertFalse(terminal_injection.triggered)
                store.complete_slack_delivery(
                    root_report.deduplication_key,
                    root_receipt,
                )
                running, turn = store.begin_turn(
                    ready.work_item_id,
                    issue_revision="revision-1",
                    prompt_sha256="d" * 64,
                    input_head_sha=BASE_SHA,
                    turn_id="turn_" + "7" * 32,
                )
                store.bind_codex_session(
                    running.work_item_id,
                    "123e4567-e89b-12d3-a456-426614174000",
                )
                store.update_turn_state(turn.turn_id, TurnState.STARTING)
                store.record_turn_result(
                    turn.turn_id,
                    output_sha256="e" * 64,
                    output_head_sha=HEAD_SHA,
                    result_status="completed",
                    result_summary="Fixture completed",
                )
                review, finished = store.finalize_turn(
                    turn.turn_id,
                    turn_state=TurnState.FINISHED,
                    work_item_state=WorkItemState.REVIEW,
                )
                store.record_published_sha(
                    review.work_item_id,
                    previous_sha=BASE_SHA,
                    head_sha=HEAD_SHA,
                )
                review = store.bind_draft_pr(review.work_item_id, 17)
                result_report = build_slack_report(
                    work_item_id=review.work_item_id,
                    turn_id=finished.turn_id,
                    kind=SlackReportKind.RESULT,
                    channel_id=FIXTURE_SLACK_CHANNEL_ID,
                    thread_ts=root_receipt.thread_ts,
                    text="Fixture result",
                )
                store.prepare_slack_delivery(result_report)

                with self.assertRaisesRegex(
                    FixtureReceiptLost,
                    "slack-terminal-receipt",
                ):
                    terminal_publisher.publish(result_report)

                self.assertTrue(terminal_injection.triggered)
                self.assertEqual(
                    SlackDeliveryState.PREPARED,
                    store.get_slack_delivery(result_report.deduplication_key).state,
                )
                self.assertEqual(
                    [SlackReportKind.ROOT, SlackReportKind.RESULT],
                    [report.kind for report in terminal_calls],
                )

    def test_publisher_discards_only_a_successful_exact_fixture_receipt(self) -> None:
        item = _running_item()
        plan = _plan(item)
        receipt = PublicationReceipt(
            item.work_item_id,
            plan.target_ref,
            HEAD_SHA,
            False,
        )
        delegate = SimpleNamespace(publish=lambda *args, **kwargs: receipt)
        injection = FixtureFaultInjection(
            FixtureFaultPoint.PUBLISHER_RECEIPT,
            ISSUE,
        )
        publisher = injection.wrap_publisher(delegate)

        with self.assertRaisesRegex(GitPublicationInterrupted, "discarded"):
            publisher.publish(b"bundle", plan=plan, work_item=item)

        self.assertTrue(injection.triggered)
        with self.assertRaisesRegex(FixtureFaultRejected, "escaped"):
            publisher.publish(
                b"bundle",
                plan=replace(plan, repository="owner/production"),
                work_item=item,
            )

        reused = SimpleNamespace(
            publish=lambda *args, **kwargs: replace(receipt, reused=True)
        )
        reused_injection = FixtureFaultInjection(
            FixtureFaultPoint.PUBLISHER_RECEIPT,
            ISSUE,
        )
        with self.assertRaisesRegex(FixtureFaultRejected, "newly written"):
            reused_injection.wrap_publisher(reused).publish(
                b"bundle",
                plan=plan,
                work_item=item,
            )
        self.assertFalse(reused_injection.triggered)

    def test_tracker_discards_pr_and_comment_receipts_after_delegate_write(self) -> None:
        item = _running_item()
        request = DraftPullRequestRequest(
            repository=FIXTURE_REPOSITORY,
            branch_name=item.task_branch,
            base_branch="main",
            title=f"Codex work for Issue #{ISSUE}",
            body="fixture",
        )
        pr_delegate = FakeTracker()
        pr_injection = FixtureFaultInjection(
            FixtureFaultPoint.DRAFT_PR_RECEIPT,
            ISSUE,
        )

        with self.assertRaisesRegex(FixtureReceiptLost, "draft-pr-receipt"):
            pr_injection.wrap_tracker(pr_delegate).create_draft_pr(request)

        self.assertTrue(pr_injection.triggered)
        self.assertIn((FIXTURE_REPOSITORY, item.task_branch), pr_delegate.pull_requests)

        comment_delegate = FakeTracker()
        comment_delegate.tasks[str(ISSUE)] = _task(TaskState.RUNNING)
        comment_injection = FixtureFaultInjection(
            FixtureFaultPoint.ISSUE_COMMENT_RECEIPT,
            ISSUE,
        )
        with self.assertRaisesRegex(FixtureReceiptLost, "issue-comment-receipt"):
            comment_injection.wrap_tracker(comment_delegate).upsert_run_comment(
                FIXTURE_REPOSITORY,
                str(ISSUE),
                f"work-item:{item.work_item_id}:status",
                "fixture status",
            )

        self.assertTrue(comment_injection.triggered)
        self.assertEqual("upsert_run_comment", comment_delegate.calls[-1].method)

    def test_completion_receipts_require_merged_identity_and_ordered_projection(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with StateStore(Path(root) / "state.db") as store:
                store.migrate()
                item = WorkItem.new(
                    repository=FIXTURE_REPOSITORY,
                    issue_number=ISSUE,
                    issue_node_id="I_fixture_fault_7",
                    base_branch="main",
                    base_sha=BASE_SHA,
                    at="2026-08-19T00:00:00Z",
                )
                store.create_work_item(item)
                for state in (
                    WorkItemState.PREPARING,
                    WorkItemState.READY,
                    WorkItemState.RUNNING,
                ):
                    store.update_work_item_state(item.work_item_id, state)
                store.record_published_sha(
                    item.work_item_id,
                    previous_sha=BASE_SHA,
                    head_sha=HEAD_SHA,
                )
                review = store.update_work_item_state(
                    item.work_item_id,
                    WorkItemState.REVIEW,
                )
                review = store.bind_draft_pr(review.work_item_id, 19)
                pull_request = PullRequest(
                    19,
                    f"https://github.com/{FIXTURE_REPOSITORY}/pull/19",
                    review.task_branch,
                    "Fixture completion",
                    False,
                    "main",
                    PullRequestState.MERGED,
                    False,
                    HEAD_SHA,
                )
                delegate = FakeTracker()
                delegate.tasks[str(ISSUE)] = _task(TaskState.REVIEW)
                delegate.pull_requests[(FIXTURE_REPOSITORY, review.task_branch)] = (
                    pull_request
                )
                comment_injection = FixtureFaultInjection(
                    FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT,
                    ISSUE,
                )
                comment_tracker = comment_injection.wrap_tracker(
                    delegate,
                    store=store,
                )
                prior_branch = "codex/issue-14-prior"
                prior_pull_request = replace(
                    pull_request,
                    number=15,
                    url=f"https://github.com/{FIXTURE_REPOSITORY}/pull/15",
                    branch_name=prior_branch,
                    head_sha="c" * 40,
                )
                delegate.pull_requests[(FIXTURE_REPOSITORY, prior_branch)] = (
                    prior_pull_request
                )

                self.assertEqual(
                    prior_pull_request,
                    comment_tracker.find_pr_by_branch(
                        FIXTURE_REPOSITORY,
                        prior_branch,
                    ),
                )
                self.assertFalse(comment_injection.completion_identity_validated)

                self.assertEqual(
                    pull_request,
                    comment_tracker.find_pr_by_branch(
                        FIXTURE_REPOSITORY,
                        review.task_branch,
                    ),
                )
                comment_injection.before_completion_candidate(
                    delegate.tasks[str(ISSUE)],
                    review,
                    pull_request,
                )
                store.update_work_item_state(
                    review.work_item_id,
                    WorkItemState.COMPLETED,
                )
                completed = store.get_work_item(review.work_item_id)
                assert completed is not None
                calls_before_rejection = tuple(delegate.calls)
                with self.assertRaisesRegex(
                    FixtureFaultRejected,
                    "exact projection identity",
                ):
                    comment_tracker.upsert_run_comment(
                        FIXTURE_REPOSITORY,
                        str(ISSUE),
                        f"work-item:{review.work_item_id}:status",
                        "Dispatcher state: agent:completed",
                    )
                self.assertEqual(calls_before_rejection, tuple(delegate.calls))
                with self.assertRaisesRegex(
                    FixtureReceiptLost,
                    "completion-comment-receipt",
                ):
                    comment_tracker.upsert_run_comment(
                        FIXTURE_REPOSITORY,
                        str(ISSUE),
                        f"work-item:{review.work_item_id}:status",
                        _completion_comment(completed),
                    )

                self.assertTrue(comment_injection.completion_identity_validated)
                self.assertTrue(comment_injection.completion_comment_projected)
                self.assertFalse(comment_injection.completion_label_projected)
                self.assertEqual(TaskState.REVIEW, delegate.tasks[str(ISSUE)].state)

                label_injection = FixtureFaultInjection(
                    FixtureFaultPoint.COMPLETION_LABEL_RECEIPT,
                    ISSUE,
                )
                label_tracker = label_injection.wrap_tracker(delegate, store=store)
                label_tracker.find_pr_by_branch(
                    FIXTURE_REPOSITORY,
                    review.task_branch,
                )
                label_tracker.upsert_run_comment(
                    FIXTURE_REPOSITORY,
                    str(ISSUE),
                    f"work-item:{review.work_item_id}:status",
                    _completion_comment(completed),
                )
                with self.assertRaisesRegex(
                    FixtureReceiptLost,
                    "completion-label-receipt",
                ):
                    label_tracker.set_state(
                        FIXTURE_REPOSITORY,
                        str(ISSUE),
                        TaskState.COMPLETED,
                    )

                self.assertTrue(label_injection.completion_identity_validated)
                self.assertTrue(label_injection.completion_comment_projected)
                self.assertTrue(label_injection.completion_label_projected)
                self.assertEqual(
                    TaskState.COMPLETED,
                    delegate.tasks[str(ISSUE)].state,
                )
                with self.assertRaisesRegex(
                    FixtureFaultRejected,
                    "unexpectedly invoked Slack",
                ):
                    label_injection.wrap_slack_publisher(
                        SimpleNamespace(publish=lambda report: None),
                        store=store,
                    ).publish(
                        build_slack_report(
                            work_item_id=review.work_item_id,
                            kind=SlackReportKind.ROOT,
                            channel_id=FIXTURE_SLACK_CHANNEL_ID,
                            text="must not publish",
                        )
                    )

    def test_tracker_rejects_wrong_issue_and_stage_before_delegate_write(self) -> None:
        delegate = FakeTracker()
        injection = FixtureFaultInjection(FixtureFaultPoint.DRAFT_PR_RECEIPT, ISSUE)
        tracker = injection.wrap_tracker(delegate)

        with self.assertRaisesRegex(FixtureFaultRejected, "Issue"):
            tracker.claim(
                FIXTURE_REPOSITORY,
                str(ISSUE + 1),
                "dispatcher",
                approved_by=("longwdl",),
            )
        with self.assertRaisesRegex(FixtureFaultRejected, "unexpected Issue comment"):
            tracker.upsert_run_comment(
                FIXTURE_REPOSITORY,
                str(ISSUE),
                "work-item:wi_000000000000000000000000:status",
                "fixture",
            )

        self.assertEqual([], delegate.calls)

    def test_terminal_branch_receipt_fault_requires_exact_prepared_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root, StateStore(
            Path(raw_root) / "terminal-branch.db"
        ) as store:
            store.migrate()
            completed = _running_item().transition_to(
                WorkItemState.REVIEW,
                at="2026-08-19T00:00:00Z",
            )
            completed = replace(
                completed,
                last_published_sha=HEAD_SHA,
                pr_number=9,
            ).transition_to(
                WorkItemState.COMPLETED,
                at="2026-08-19T00:00:00Z",
            )
            store.create_work_item(completed)
            archive_request = RunnerRequest(
                RunnerOperation.ARCHIVE,
                completed.work_item_id,
                version=NEXT_PROTOCOL_VERSION,
                expected_head_sha=HEAD_SHA,
            )
            store.prepare_work_item_archive(
                completed.work_item_id,
                expected_head_sha=HEAD_SHA,
                eligible_at="2026-08-19T00:00:00+00:00",
                request_sha256=sha256(
                    archive_request.to_json().encode("utf-8")
                ).hexdigest(),
            )
            store.record_work_item_absence_reconciliation(
                completed.work_item_id,
                expected_head_sha=HEAD_SHA,
                evidence_sha256="e" * 64,
                observed_by="operator",
                observed_at="2026-08-19T00:00:01+00:00",
            )
            request_sha256 = terminal_branch_request_sha256(
                work_item_id=completed.work_item_id,
                repository=completed.repository,
                branch_name=completed.task_branch,
                expected_head_sha=HEAD_SHA,
            )
            store.prepare_terminal_branch_cleanup(
                completed.work_item_id,
                expected_head_sha=HEAD_SHA,
                eligible_at="2026-08-19T00:00:01+00:00",
                request_sha256=request_sha256,
            )
            delegate = FakeTracker()
            delegate.branches[(completed.repository, completed.task_branch)] = HEAD_SHA
            injection = FixtureFaultInjection(
                FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECEIPT,
                ISSUE,
                expected_work_item_id=completed.work_item_id,
                expected_head_sha=HEAD_SHA,
            )
            tracker = injection.wrap_tracker(delegate, store=store)

            with self.assertRaises(FixtureReceiptLost):
                tracker.delete_branch(
                    completed.repository,
                    completed.task_branch,
                    HEAD_SHA,
                )

            cleanup = store.get_terminal_branch_cleanup(completed.work_item_id)
            self.assertTrue(injection.triggered)
            self.assertIsNotNone(cleanup)
            self.assertIs(TerminalBranchCleanupState.PREPARED, cleanup.state)
            self.assertIsNone(
                delegate.get_branch_head(
                    completed.repository,
                    completed.task_branch,
                )
            )
            self.assertEqual(
                1,
                sum(call.method == "delete_branch" for call in delegate.calls),
            )
            recovery = FixtureFaultInjection(
                FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECOVERY,
                ISSUE,
                expected_work_item_id=completed.work_item_id,
                expected_head_sha=HEAD_SHA,
            ).wrap_tracker(delegate, store=store)
            self.assertIsNone(
                recovery.get_branch_head(
                    completed.repository,
                    completed.task_branch,
                )
            )
            with self.assertRaisesRegex(FixtureFaultRejected, "second delete"):
                recovery.delete_branch(
                    completed.repository,
                    completed.task_branch,
                    HEAD_SHA,
                )
            self.assertEqual(
                1,
                sum(call.method == "delete_branch" for call in delegate.calls),
            )

    def test_fixture_fault_allows_completion_reads_but_rejects_before_local_write(self) -> None:
        delegate = FakeTracker()
        other_task = replace(
            _task(TaskState.REVIEW),
            task_id=str(ISSUE + 1),
            issue_number=ISSUE + 1,
            issue_node_id="I_fixture_fault_8",
        )
        delegate.tasks[other_task.task_id] = other_task
        injection = FixtureFaultInjection(FixtureFaultPoint.START_RECEIPT, ISSUE)
        tracker = injection.wrap_tracker(delegate)

        self.assertEqual(
            other_task,
            tracker.get_task(FIXTURE_REPOSITORY, other_task.task_id),
        )
        work_item = WorkItem.new(
            repository=FIXTURE_REPOSITORY,
            issue_number=ISSUE + 1,
            issue_node_id=other_task.issue_node_id,
            base_branch="main",
            base_sha=BASE_SHA,
            at="2026-08-19T00:00:00Z",
        )
        for state in (
            WorkItemState.PREPARING,
            WorkItemState.READY,
            WorkItemState.RUNNING,
            WorkItemState.REVIEW,
        ):
            work_item = work_item.transition_to(
                state,
                at="2026-08-19T00:00:00Z",
            )
        work_item = replace(
            work_item,
            pr_number=9,
            last_published_sha=HEAD_SHA,
        )
        pull_request = PullRequest(
            9,
            f"https://github.com/{FIXTURE_REPOSITORY}/pull/9",
            work_item.task_branch,
            "Other completed Fixture",
            False,
            "main",
            PullRequestState.MERGED,
            False,
            HEAD_SHA,
        )
        with self.assertRaisesRegex(FixtureFaultRejected, "cannot complete"):
            injection.before_completion_candidate(
                other_task,
                work_item,
                pull_request,
            )

    def test_publication_recorded_hook_and_recovery_io_guards_are_exact(self) -> None:
        recorded = replace(_running_item(), last_published_sha=HEAD_SHA)
        turn = _checkpoint_turn(recorded)
        injection = FixtureFaultInjection(
            FixtureFaultPoint.PUBLICATION_RECORDED,
            ISSUE,
        )

        with self.assertRaisesRegex(FixtureProcessInterrupted, "recording"):
            injection.after_publication_recorded(recorded, turn)

        self.assertTrue(injection.triggered)
        recovery = FixtureFaultInjection(
            FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY,
            ISSUE,
        )
        guarded_transport = recovery.wrap_transport(
            SimpleNamespace(invoke=lambda *args, **kwargs: None)
        )
        with self.assertRaisesRegex(FixtureFaultRejected, "Runner"):
            guarded_transport.invoke(SimpleNamespace())

        receipt = PublicationReceipt(
            recorded.work_item_id,
            _plan(recorded).target_ref,
            HEAD_SHA,
            True,
        )
        guarded_publisher = recovery.wrap_publisher(
            SimpleNamespace(publish=lambda *args, **kwargs: receipt)
        )
        with self.assertRaisesRegex(FixtureFaultRejected, "Publisher"):
            guarded_publisher.publish(
                b"bundle",
                plan=_plan(recorded),
                work_item=recorded,
            )

    def test_claim_acquired_hook_stops_before_any_later_port(self) -> None:
        delegate = FakeTracker()
        delegate.ready_tasks = (_task(TaskState.READY),)
        delegate.tasks[str(ISSUE)] = _task(TaskState.READY)
        observed: list[str] = []
        injection = FixtureFaultInjection(
            FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL,
            ISSUE,
            claim_acquired_callback=lambda task: observed.append(task.issue_node_id),
        )
        tracker = injection.wrap_tracker(delegate)

        claimed = tracker.claim(
            FIXTURE_REPOSITORY,
            str(ISSUE),
            "codex-dispatcher",
            approved_by=("longwdl",),
        )
        assert claimed.task is not None
        with self.assertRaisesRegex(FixtureFaultRejected, "unexpectedly returned"):
            injection.after_claim_acquired(claimed.task)

        self.assertTrue(injection.triggered)
        self.assertEqual(["I_fixture_fault_7"], observed)
        with self.assertRaisesRegex(FixtureFaultRejected, "Runner"):
            injection.wrap_transport(
                SimpleNamespace(invoke=lambda *args, **kwargs: None)
            ).invoke(SimpleNamespace())
        with self.assertRaisesRegex(FixtureFaultRejected, "state write"):
            tracker.set_state(
                FIXTURE_REPOSITORY,
                str(ISSUE),
                TaskState.RUNNING,
            )
        with self.assertRaisesRegex(FixtureFaultRejected, "claim callback"):
            FixtureFaultInjection(
                FixtureFaultPoint.PUBLISHER_RECEIPT,
                ISSUE,
                claim_acquired_callback=lambda task: None,
            )

    def test_process_kill_source_uses_only_independently_pinned_cached_base(self) -> None:
        observed: list[tuple[str, str]] = []
        marker = object()
        delegate = SimpleNamespace(
            current=lambda *args: self.fail("network refresh must not run"),
            exact=lambda repository, base_sha: (
                observed.append((repository, base_sha)),
                marker,
            )[1],
        )
        injection = FixtureFaultInjection(
            FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL,
            ISSUE,
            pinned_base_sha=BASE_SHA,
        )

        source = injection.wrap_source(delegate)

        self.assertIs(marker, source.current(FIXTURE_REPOSITORY, "main"))
        self.assertEqual([(FIXTURE_REPOSITORY, BASE_SHA)], observed)
        with self.assertRaisesRegex(FixtureFaultRejected, "different base"):
            source.exact(FIXTURE_REPOSITORY, HEAD_SHA)
        with self.assertRaisesRegex(FixtureFaultRejected, "requires one"):
            FixtureFaultInjection(
                FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL,
                ISSUE,
            ).wrap_source(delegate)

    def test_start_receipt_loss_and_status_recovery_never_replay_execution(self) -> None:
        item = _running_item()
        turn = _reconciling_turn(item)
        start = RunnerRequest(
            RunnerOperation.START,
            item.work_item_id,
            turn_id=turn.turn_id,
            prompt_sha256=turn.prompt_sha256,
            input_head_sha=turn.input_head_sha,
        )
        start_reply = RunnerTurnReply(
            RunnerOperation.START,
            item.work_item_id,
            turn.turn_id,
            RunnerTurnRemoteState.RUNNING,
            session_id="123e4567-e89b-12d3-a456-426614174000",
        )
        injection = FixtureFaultInjection(FixtureFaultPoint.START_RECEIPT, ISSUE)
        transport = injection.wrap_transport(
            SimpleNamespace(
                invoke=lambda *args, **kwargs: RunnerWireOutput(
                    start_reply.to_json().encode("utf-8")
                )
            )
        )

        with self.assertRaisesRegex(RunnerTransportInterrupted, "START response"):
            transport.invoke(start, stdin=b"fixture prompt")

        self.assertTrue(injection.triggered)
        recovery = FixtureFaultInjection(
            FixtureFaultPoint.START_STATUS_RECOVERY,
            ISSUE,
        )
        guarded = recovery.wrap_transport(
            SimpleNamespace(
                invoke=lambda *args, **kwargs: RunnerWireOutput(b"{}")
            )
        )
        with self.assertRaisesRegex(FixtureFaultRejected, "replay"):
            guarded.invoke(start, stdin=b"must not replay")
        guarded.invoke(
            RunnerRequest(
                RunnerOperation.STATUS,
                item.work_item_id,
                turn_id=turn.turn_id,
            )
        )
        guarded.invoke(
            RunnerRequest(
                RunnerOperation.EXPORT,
                item.work_item_id,
                expected_head_sha=HEAD_SHA,
            )
        )
        self.assertEqual(
            [RunnerOperation.STATUS, RunnerOperation.EXPORT],
            recovery.recovery_operations,
        )

    def test_ssh_process_kill_requires_local_start_and_second_status_proof(self) -> None:
        class ExactProcess:
            argv = ("/usr/bin/ssh", "fixture")
            pid = 321
            process_group_id = 321
            session_id = 321
            termination_requested = False

            def kill_exact_process_group(self, *, expected_argv, expected_pid):
                self_test.assertEqual(self.argv, tuple(expected_argv))
                self_test.assertEqual(self.pid, expected_pid)
                self.termination_requested = True

        self_test = self
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.db"
            with StateStore(database) as store:
                store.migrate()
                ready = WorkItem.new(
                    repository=FIXTURE_REPOSITORY,
                    issue_number=ISSUE,
                    issue_node_id="I_fixture_fault_7",
                    base_branch="main",
                    base_sha=BASE_SHA,
                    at="2026-08-19T00:00:00Z",
                )
                ready = ready.transition_to(
                    WorkItemState.PREPARING,
                    at="2026-08-19T00:00:00Z",
                ).transition_to(
                    WorkItemState.READY,
                    at="2026-08-19T00:00:00Z",
                )
                store.create_work_item(ready)
                running, turn = store.begin_turn(
                    ready.work_item_id,
                    issue_revision="revision-1",
                    prompt_sha256="d" * 64,
                    input_head_sha=BASE_SHA,
                    issue_allowed_paths=("README.md",),
                    turn_id="turn_" + "4" * 32,
                )
                turn = store.update_turn_state(turn.turn_id, TurnState.STARTING)
                request = RunnerRequest(
                    RunnerOperation.START,
                    running.work_item_id,
                    turn_id=turn.turn_id,
                    prompt_sha256=turn.prompt_sha256,
                    input_head_sha=turn.input_head_sha,
                )
                proof = RunnerTurnReply(
                    RunnerOperation.STATUS,
                    running.work_item_id,
                    turn.turn_id,
                    RunnerTurnRemoteState.UNKNOWN,
                    error_code="turn_outcome_unresolved",
                )
                observed = []
                status_transport = SimpleNamespace(
                    invoke=lambda status, **kwargs: (
                        observed.append((status, kwargs)),
                        RunnerWireOutput(proof.to_json().encode("utf-8")),
                    )[1]
                )
                injection = FixtureFaultInjection(
                    FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL,
                    ISSUE,
                )
                hook = injection.ssh_process_started_hook(
                    store=store,
                    status_transport=status_transport,
                )
                assert hook is not None
                process = ExactProcess()
                hook(
                    request,
                    SshInvocationPlan(process.argv, {}),
                    process,  # type: ignore[arg-type]
                )

                missing = RunnerTurnReply(
                    RunnerOperation.STATUS,
                    running.work_item_id,
                    turn.turn_id,
                    RunnerTurnRemoteState.UNKNOWN,
                    error_code="turn_not_found",
                )
                rejected_process = SimpleNamespace(
                    argv=("/usr/bin/ssh", "fixture"),
                    pid=322,
                    process_group_id=322,
                    session_id=322,
                    termination_requested=False,
                    kill_exact_process_group=lambda **kwargs: self.fail("must not kill"),
                )
                rejected = FixtureFaultInjection(
                    FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL,
                    ISSUE,
                )
                rejected_hook = rejected.ssh_process_started_hook(
                    store=store,
                    status_transport=SimpleNamespace(
                        invoke=lambda *args, **kwargs: RunnerWireOutput(
                            missing.to_json().encode("utf-8")
                        )
                    ),
                )
                assert rejected_hook is not None
                with patch("codex_dispatcher.fixture_faults.time.sleep"):
                    rejected_hook(
                        request,
                        SshInvocationPlan(rejected_process.argv, {}),
                        rejected_process,  # type: ignore[arg-type]
                    )

        self.assertTrue(injection.triggered)
        self.assertTrue(process.termination_requested)
        self.assertEqual(321, injection.ssh_process_pid)
        self.assertEqual(RunnerTurnRemoteState.UNKNOWN, injection.ssh_status_state)
        self.assertEqual("turn_outcome_unresolved", injection.ssh_status_error_code)
        self.assertEqual(1, injection.ssh_status_attempts)
        self.assertEqual(1, len(observed))
        self.assertEqual(RunnerOperation.STATUS, observed[0][0].operation)
        self.assertEqual({}, observed[0][1])
        self.assertFalse(rejected.triggered)
        self.assertFalse(rejected_process.termination_requested)
        self.assertEqual(5, rejected.ssh_status_attempts)
        self.assertIn("could not prove", rejected.ssh_interrupt_rejection)
        self.assertFalse(
            _is_durable_ssh_start_proof(
                RunnerTurnReply(
                    RunnerOperation.STATUS,
                    _running_item().work_item_id,
                    _reconciling_turn(_running_item()).turn_id,
                    RunnerTurnRemoteState.RUNNING,
                    session_id="123e4567-e89b-12d3-a456-426614174000",
                )
            )
        )
        guarded = FixtureFaultInjection(
            FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL,
            ISSUE,
        ).wrap_transport(
            SimpleNamespace(invoke=lambda *args, **kwargs: self.fail("must not delegate"))
        )
        with self.assertRaisesRegex(FixtureFaultRejected, "unexpected Runner operation"):
            guarded.invoke(
                RunnerRequest(
                    RunnerOperation.STATUS,
                    _running_item().work_item_id,
                    turn_id=_reconciling_turn(_running_item()).turn_id,
                )
            )

    def test_preflight_requires_the_exact_fault_sequences(self) -> None:
        ready = SshPreflightPlan(
            SshPreflightStatus.READY_CANDIDATE,
            SshRecoveryAction.IDLE,
            task=_task(TaskState.READY),
        )
        validate_fixture_preflight(
            ready,
            fault=FixtureFaultPoint.PUBLISHER_RECEIPT,
            issue_number=ISSUE,
        )
        validate_fixture_preflight(
            ready,
            fault=FixtureFaultPoint.PUBLICATION_RECORDED,
            issue_number=ISSUE,
        )
        validate_fixture_preflight(
            ready,
            fault=FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL,
            issue_number=ISSUE,
        )
        validate_fixture_preflight(
            ready,
            fault=FixtureFaultPoint.START_RECEIPT,
            issue_number=ISSUE,
        )
        validate_fixture_preflight(
            ready,
            fault=FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL,
            issue_number=ISSUE,
        )
        validate_fixture_preflight(
            ready,
            fault=FixtureFaultPoint.SLACK_ROOT_RECEIPT,
            issue_number=ISSUE,
        )

        ambiguous_item = _running_item()
        ambiguous = SshPreflightPlan(
            SshPreflightStatus.READY_RECOVERY,
            SshRecoveryAction.RECONCILE_ACTIVE_TURN,
            task=_task(TaskState.RUNNING),
            work_item=ambiguous_item,
            turn=_reconciling_turn(ambiguous_item),
        )
        validate_fixture_preflight(
            ambiguous,
            fault=FixtureFaultPoint.START_STATUS_RECOVERY,
            issue_number=ISSUE,
        )
        with self.assertRaisesRegex(FixtureFaultRejected, "ambiguous active Turn"):
            validate_fixture_preflight(
                replace(ambiguous, turn=None),
                fault=FixtureFaultPoint.START_STATUS_RECOVERY,
                issue_number=ISSUE,
            )

        running = _running_item()
        publication = SshPreflightPlan(
            SshPreflightStatus.READY_RECOVERY,
            SshRecoveryAction.RESUME_PUBLICATION,
            task=_task(TaskState.RUNNING),
            work_item=running,
        )
        validate_fixture_preflight(
            publication,
            fault=FixtureFaultPoint.DRAFT_PR_RECEIPT,
            issue_number=ISSUE,
        )

        recorded = replace(running, last_published_sha=HEAD_SHA)
        recorded_recovery = SshPreflightPlan(
            SshPreflightStatus.READY_RECOVERY,
            SshRecoveryAction.RESUME_PUBLICATION,
            task=_task(TaskState.DISPATCHING),
            work_item=recorded,
            turn=_checkpoint_turn(recorded),
        )
        validate_fixture_preflight(
            recorded_recovery,
            fault=FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY,
            issue_number=ISSUE,
        )
        with self.assertRaisesRegex(FixtureFaultRejected, "durable checkpoint"):
            validate_fixture_preflight(
                replace(recorded_recovery, turn=None),
                fault=FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY,
                issue_number=ISSUE,
            )

        review = replace(
            running.transition_to(
                WorkItemState.REVIEW,
                at="2026-08-19T00:00:00Z",
            ),
            last_published_sha=HEAD_SHA,
        )
        comment = SshPreflightPlan(
            SshPreflightStatus.READY_RECOVERY,
            SshRecoveryAction.SYNC_TRACKER_STATE,
            task=_task(TaskState.RUNNING),
            work_item=review,
        )
        validate_fixture_preflight(
            comment,
            fault=FixtureFaultPoint.ISSUE_COMMENT_RECEIPT,
            issue_number=ISSUE,
        )

        with self.assertRaisesRegex(FixtureFaultRejected, "unbound"):
            validate_fixture_preflight(
                replace(comment, work_item=replace(review, pr_number=3)),
                fault=FixtureFaultPoint.ISSUE_COMMENT_RECEIPT,
                issue_number=ISSUE,
            )

        completion_review = replace(review, pr_number=19)
        merged = PullRequest(
            19,
            f"https://github.com/{FIXTURE_REPOSITORY}/pull/19",
            completion_review.task_branch,
            "Fixture completion",
            False,
            "main",
            PullRequestState.MERGED,
            False,
            HEAD_SHA,
        )
        completion_comment = SshPreflightPlan(
            SshPreflightStatus.READY_RECOVERY,
            SshRecoveryAction.COMPLETE_MERGED_WORK_ITEM,
            task=_task(TaskState.REVIEW),
            work_item=completion_review,
            pull_request=merged,
        )
        validate_fixture_preflight(
            completion_comment,
            fault=FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT,
            issue_number=ISSUE,
        )
        completion_label = replace(
            completion_comment,
            recovery_action=SshRecoveryAction.SYNC_TRACKER_STATE,
            work_item=completion_review.transition_to(
                WorkItemState.COMPLETED,
                at="2026-08-19T00:00:01Z",
            ),
        )
        validate_fixture_preflight(
            completion_label,
            fault=FixtureFaultPoint.COMPLETION_LABEL_RECEIPT,
            issue_number=ISSUE,
        )
        with self.assertRaisesRegex(FixtureFaultRejected, "merged Fixture PR"):
            validate_fixture_preflight(
                replace(
                    completion_comment,
                    pull_request=replace(merged, is_draft=True),
                ),
                fault=FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT,
                issue_number=ISSUE,
            )

    def test_slack_terminal_preflight_requires_one_prepared_unbound_root(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            with StateStore(Path(root) / "state.db") as store:
                store.migrate()
                item = WorkItem.new(
                    repository=FIXTURE_REPOSITORY,
                    issue_number=ISSUE,
                    issue_node_id="I_fixture_fault_7",
                    base_branch="main",
                    base_sha=BASE_SHA,
                    at="2026-08-19T00:00:00Z",
                )
                store.create_work_item(item)
                store.update_work_item_state(
                    item.work_item_id,
                    WorkItemState.PREPARING,
                )
                ready = store.update_work_item_state(
                    item.work_item_id,
                    WorkItemState.READY,
                )
                report = build_slack_report(
                    work_item_id=ready.work_item_id,
                    kind=SlackReportKind.ROOT,
                    channel_id=FIXTURE_SLACK_CHANNEL_ID,
                    text="Fixture root",
                )
                store.prepare_slack_delivery(report)
                plan = SshPreflightPlan(
                    SshPreflightStatus.READY_RECOVERY,
                    SshRecoveryAction.START_CLAIMED_TURN,
                    task=_task(TaskState.DISPATCHING),
                    work_item=ready,
                )

                validate_fixture_preflight(
                    plan,
                    fault=FixtureFaultPoint.SLACK_TERMINAL_RECEIPT,
                    issue_number=ISSUE,
                    store=store,
                )
                with self.assertRaisesRegex(FixtureFaultRejected, "SQLite proof"):
                    validate_fixture_preflight(
                        plan,
                        fault=FixtureFaultPoint.SLACK_TERMINAL_RECEIPT,
                        issue_number=ISSUE,
                    )

        config = make_config(global_max_active=1, repository_max_active=1)
        fixture_config = replace(
            config,
            repositories=(
                replace(
                    config.repositories[0],
                    slug=FIXTURE_REPOSITORY,
                    allowed_paths=("README.md",),
                    denied_paths=(),
                    maintainers=("longwdl",),
                    required_checks=("fixture",),
                ),
            ),
            slack_runtime=SlackRuntimeConfig(
                issue_channel_id=FIXTURE_SLACK_CHANNEL_ID,
                system_channel_id="C0BS3LPG43G",
                request_timeout_seconds=10,
                idempotency_contract="client_msg_id-live-fixture-verified-v1",
            ),
        )
        validate_fixture_slack_config(fixture_config)
        with self.assertRaisesRegex(FixtureFaultRejected, "Slack receipt"):
            validate_fixture_slack_config(
                replace(
                    fixture_config,
                    slack_runtime=replace(
                        fixture_config.slack_runtime,
                        issue_channel_id="C0000000000",
                    ),
                )
            )

    def test_cli_guards_fail_before_loading_live_config(self) -> None:
        environments = (
            {},
            {
                "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS": "1",
                "GITHUB_TOKEN": "github_pat_fixture_test",
            },
        )
        for environment in environments:
            with (
                self.subTest(environment=environment),
                patch.dict("os.environ", environment, clear=True),
                patch(
                    "codex_dispatcher.fixture_fault_cli.load_protected_ssh_config"
                ) as load,
            ):
                code, payload = _run(
                    Path("/protected/config.toml"),
                    issue_number=ISSUE,
                    fault=FixtureFaultPoint.PUBLISHER_RECEIPT,
                )

                self.assertEqual(1, code)
                self.assertFalse(payload["ok"])
                load.assert_not_called()
        with (
            patch.dict(
                "os.environ",
                {
                    "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                    "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS": FIXTURE_REPOSITORY,
                    "GITHUB_TOKEN": "github_pat_fixture_test",
                },
                clear=True,
            ),
            patch(
                "codex_dispatcher.fixture_fault_cli.load_protected_ssh_config"
            ) as load,
        ):
            code, payload = _run(
                Path("/protected/config.toml"),
                issue_number=ISSUE,
                fault=FixtureFaultPoint.TERMINAL_BRANCH_DELETE_RECEIPT,
            )

            self.assertEqual(1, code)
            self.assertIn("--work-item-id", payload["error"])
            load.assert_not_called()

    def test_cli_slack_fault_requires_separate_gate_and_bot_token(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.db"
            with StateStore(database) as store:
                store.migrate()
            base = make_config(global_max_active=1, repository_max_active=1)
            config = replace(
                base,
                scheduler=replace(base.scheduler, database_path=database),
                repositories=(
                    replace(
                        base.repositories[0],
                        slug=FIXTURE_REPOSITORY,
                        allowed_paths=("README.md",),
                        denied_paths=(),
                        maintainers=("longwdl",),
                        required_checks=("fixture",),
                    ),
                ),
                slack_runtime=SlackRuntimeConfig(
                    issue_channel_id=FIXTURE_SLACK_CHANNEL_ID,
                    system_channel_id="C0BS3LPG43G",
                    request_timeout_seconds=10,
                    idempotency_contract="client_msg_id-live-fixture-verified-v1",
                ),
            )
            common = {
                "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS": FIXTURE_REPOSITORY,
                "GITHUB_TOKEN": "github_pat_fixture_test",
            }
            for environment, message in (
                (common, "ENABLE_SLACK_WRITES"),
                ({**common, "CODEX_DISPATCHER_ENABLE_SLACK_WRITES": "1"}, "bot token"),
            ):
                with (
                    self.subTest(message=message),
                    patch.dict("os.environ", environment, clear=True),
                    patch(
                        "codex_dispatcher.fixture_fault_cli.load_protected_ssh_config",
                        return_value=config,
                    ),
                    patch(
                        "codex_dispatcher.fixture_fault_cli.validate_runtime_state_path"
                    ),
                    patch(
                        "codex_dispatcher.fixture_fault_cli.run_ssh_preflight"
                    ) as preflight,
                ):
                    code, payload = _run(
                        Path(root) / "config.toml",
                        issue_number=ISSUE,
                        fault=FixtureFaultPoint.SLACK_ROOT_RECEIPT,
                    )

                self.assertEqual(1, code)
                self.assertIn(message, payload["error"])
                preflight.assert_not_called()

    def test_cli_reports_slack_root_receipt_loss_as_unstarted_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.db"
            with StateStore(database) as store:
                store.migrate()
            base = make_config(global_max_active=1, repository_max_active=1)
            config = replace(
                base,
                scheduler=replace(base.scheduler, database_path=database),
                repositories=(
                    replace(
                        base.repositories[0],
                        slug=FIXTURE_REPOSITORY,
                        allowed_paths=("README.md",),
                        denied_paths=(),
                        maintainers=("longwdl",),
                        required_checks=("fixture",),
                    ),
                ),
                slack_runtime=SlackRuntimeConfig(
                    issue_channel_id=FIXTURE_SLACK_CHANNEL_ID,
                    system_channel_id="C0BS3LPG43G",
                    request_timeout_seconds=10,
                    idempotency_contract="client_msg_id-live-fixture-verified-v1",
                ),
            )
            preflight = SshPreflightPlan(
                SshPreflightStatus.READY_CANDIDATE,
                SshRecoveryAction.IDLE,
                task=_task(TaskState.READY),
            )

            def build_fixture_sweep(**kwargs):
                injection = kwargs["injection"]
                store = kwargs["store"]

                def run_once():
                    item = WorkItem.new(
                        repository=FIXTURE_REPOSITORY,
                        issue_number=ISSUE,
                        issue_node_id="I_fixture_fault_7",
                        base_branch="main",
                        base_sha=BASE_SHA,
                        at="2026-08-19T00:00:00Z",
                    )
                    store.create_work_item(item)
                    store.update_work_item_state(
                        item.work_item_id,
                        WorkItemState.PREPARING,
                    )
                    item = store.update_work_item_state(
                        item.work_item_id,
                        WorkItemState.READY,
                    )
                    report = build_slack_report(
                        work_item_id=item.work_item_id,
                        kind=SlackReportKind.ROOT,
                        channel_id=FIXTURE_SLACK_CHANNEL_ID,
                        text="Fixture root",
                    )
                    store.prepare_slack_delivery(report)
                    injection.slack_receipt = SlackDeliveryReceipt(
                        report.deduplication_key,
                        FIXTURE_SLACK_CHANNEL_ID,
                        "1700000000.000001",
                        "1700000000.000001",
                        (
                            "https://fixture.slack.com/archives/"
                            f"{FIXTURE_SLACK_CHANNEL_ID}/p1700000000000001"
                        ),
                    )
                    injection.triggered = True
                    raise FixtureReceiptLost("fixture Slack root receipt lost")

                return SimpleNamespace(run_once=run_once)

            with (
                patch.dict(
                    "os.environ",
                    {
                        "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                        "CODEX_DISPATCHER_ENABLE_SLACK_WRITES": "1",
                        "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS": FIXTURE_REPOSITORY,
                        "GITHUB_TOKEN": "github_pat_fixture_test",
                        "SLACK_BOT_TOKEN": "xoxb-1234567890-fixture",
                    },
                    clear=True,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.load_protected_ssh_config",
                    return_value=config,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.validate_runtime_state_path"
                ),
                patch("codex_dispatcher.fixture_fault_cli.verify_slack_workspace"),
                patch(
                    "codex_dispatcher.fixture_fault_cli.run_ssh_preflight",
                    return_value=SshPreflightInspection(preflight, (), True),
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.build_ssh_fixture_fault_sweep",
                    side_effect=build_fixture_sweep,
                ),
            ):
                code, payload = _run(
                    Path(root) / "config.toml",
                    issue_number=ISSUE,
                    fault=FixtureFaultPoint.SLACK_ROOT_RECEIPT,
                )

        self.assertEqual(0, code)
        self.assertTrue(payload["fault_triggered"])
        self.assertEqual("receipt_lost", payload["status"])
        self.assertEqual("prepared", payload["slack_outbox_state"])
        self.assertTrue(payload["slack_receipt_discarded"])

    def test_cli_reports_slack_terminal_receipt_loss_as_projection_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.db"
            with StateStore(database) as store:
                store.migrate()
                item = WorkItem.new(
                    repository=FIXTURE_REPOSITORY,
                    issue_number=ISSUE,
                    issue_node_id="I_fixture_fault_7",
                    base_branch="main",
                    base_sha=BASE_SHA,
                    at="2026-08-19T00:00:00Z",
                )
                store.create_work_item(item)
                store.update_work_item_state(item.work_item_id, WorkItemState.PREPARING)
                ready = store.update_work_item_state(
                    item.work_item_id,
                    WorkItemState.READY,
                )
                root_report = build_slack_report(
                    work_item_id=ready.work_item_id,
                    kind=SlackReportKind.ROOT,
                    channel_id=FIXTURE_SLACK_CHANNEL_ID,
                    text="Fixture root",
                )
                store.prepare_slack_delivery(root_report)
            base = make_config(global_max_active=1, repository_max_active=1)
            config = replace(
                base,
                scheduler=replace(base.scheduler, database_path=database),
                repositories=(
                    replace(
                        base.repositories[0],
                        slug=FIXTURE_REPOSITORY,
                        allowed_paths=("README.md",),
                        denied_paths=(),
                        maintainers=("longwdl",),
                        required_checks=("fixture",),
                    ),
                ),
                slack_runtime=SlackRuntimeConfig(
                    issue_channel_id=FIXTURE_SLACK_CHANNEL_ID,
                    system_channel_id="C0BS3LPG43G",
                    request_timeout_seconds=10,
                    idempotency_contract="client_msg_id-live-fixture-verified-v1",
                ),
            )
            preflight = SshPreflightPlan(
                SshPreflightStatus.READY_RECOVERY,
                SshRecoveryAction.START_CLAIMED_TURN,
                task=_task(TaskState.DISPATCHING),
                work_item=ready,
            )

            def build_fixture_sweep(**kwargs):
                injection = kwargs["injection"]
                store = kwargs["store"]

                def run_once():
                    root_receipt = SlackDeliveryReceipt(
                        root_report.deduplication_key,
                        FIXTURE_SLACK_CHANNEL_ID,
                        "1700000000.000001",
                        "1700000000.000001",
                        (
                            "https://fixture.slack.com/archives/"
                            f"{FIXTURE_SLACK_CHANNEL_ID}/p1700000000000001"
                        ),
                    )
                    store.complete_slack_delivery(
                        root_report.deduplication_key,
                        root_receipt,
                    )
                    running, turn = store.begin_turn(
                        ready.work_item_id,
                        issue_revision="revision-1",
                        prompt_sha256="d" * 64,
                        input_head_sha=BASE_SHA,
                        turn_id="turn_" + "8" * 32,
                    )
                    store.bind_codex_session(
                        running.work_item_id,
                        "123e4567-e89b-12d3-a456-426614174000",
                    )
                    store.update_turn_state(turn.turn_id, TurnState.STARTING)
                    store.record_turn_result(
                        turn.turn_id,
                        output_sha256="e" * 64,
                        output_head_sha=HEAD_SHA,
                        result_status="completed",
                        result_summary="Fixture completed",
                    )
                    review, finished = store.finalize_turn(
                        turn.turn_id,
                        turn_state=TurnState.FINISHED,
                        work_item_state=WorkItemState.REVIEW,
                    )
                    store.record_published_sha(
                        review.work_item_id,
                        previous_sha=BASE_SHA,
                        head_sha=HEAD_SHA,
                    )
                    review = store.bind_draft_pr(review.work_item_id, 17)
                    report = build_slack_report(
                        work_item_id=review.work_item_id,
                        turn_id=finished.turn_id,
                        kind=SlackReportKind.RESULT,
                        channel_id=FIXTURE_SLACK_CHANNEL_ID,
                        thread_ts=root_receipt.thread_ts,
                        text="Fixture result",
                    )
                    store.prepare_slack_delivery(report)
                    injection.slack_receipt = SlackDeliveryReceipt(
                        report.deduplication_key,
                        FIXTURE_SLACK_CHANNEL_ID,
                        "1700000000.000002",
                        root_receipt.thread_ts,
                        (
                            "https://fixture.slack.com/archives/"
                            f"{FIXTURE_SLACK_CHANNEL_ID}/p1700000000000002"
                            "?thread_ts=1700000000.000001&"
                            f"cid={FIXTURE_SLACK_CHANNEL_ID}"
                        ),
                    )
                    injection.triggered = True
                    raise FixtureReceiptLost("fixture Slack terminal receipt lost")

                return SimpleNamespace(run_once=run_once)

            with (
                patch.dict(
                    "os.environ",
                    {
                        "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                        "CODEX_DISPATCHER_ENABLE_SLACK_WRITES": "1",
                        "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS": FIXTURE_REPOSITORY,
                        "GITHUB_TOKEN": "github_pat_fixture_test",
                        "SLACK_BOT_TOKEN": "xoxb-1234567890-fixture",
                    },
                    clear=True,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.load_protected_ssh_config",
                    return_value=config,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.validate_runtime_state_path"
                ),
                patch("codex_dispatcher.fixture_fault_cli.verify_slack_workspace"),
                patch(
                    "codex_dispatcher.fixture_fault_cli.run_ssh_preflight",
                    return_value=SshPreflightInspection(preflight, (), True),
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.build_ssh_fixture_fault_sweep",
                    side_effect=build_fixture_sweep,
                ),
            ):
                code, payload = _run(
                    Path(root) / "config.toml",
                    issue_number=ISSUE,
                    fault=FixtureFaultPoint.SLACK_TERMINAL_RECEIPT,
                )

        self.assertEqual(0, code)
        self.assertTrue(payload["fault_triggered"])
        self.assertEqual("receipt_lost", payload["status"])
        self.assertEqual("turn_" + "8" * 32, payload["turn_id"])
        self.assertEqual("prepared", payload["slack_outbox_state"])

    def test_cli_reports_ordered_completion_comment_and_label_receipt_loss(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.db"
            with StateStore(database) as store:
                store.migrate()
                item = WorkItem.new(
                    repository=FIXTURE_REPOSITORY,
                    issue_number=ISSUE,
                    issue_node_id="I_fixture_fault_7",
                    base_branch="main",
                    base_sha=BASE_SHA,
                    at="2026-08-19T00:00:00Z",
                )
                store.create_work_item(item)
                store.update_work_item_state(item.work_item_id, WorkItemState.PREPARING)
                ready = store.update_work_item_state(
                    item.work_item_id,
                    WorkItemState.READY,
                )
                running, turn = store.begin_turn(
                    ready.work_item_id,
                    issue_revision="revision-1",
                    prompt_sha256="d" * 64,
                    input_head_sha=BASE_SHA,
                    turn_id="turn_" + "9" * 32,
                )
                store.bind_codex_session(
                    running.work_item_id,
                    "123e4567-e89b-12d3-a456-426614174000",
                )
                store.update_turn_state(turn.turn_id, TurnState.STARTING)
                store.record_turn_result(
                    turn.turn_id,
                    output_sha256="e" * 64,
                    output_head_sha=HEAD_SHA,
                    result_status="completed",
                    result_summary="Fixture completed",
                )
                review, finished = store.finalize_turn(
                    turn.turn_id,
                    turn_state=TurnState.FINISHED,
                    work_item_state=WorkItemState.REVIEW,
                )
                store.record_published_sha(
                    review.work_item_id,
                    previous_sha=BASE_SHA,
                    head_sha=HEAD_SHA,
                )
                review = store.bind_draft_pr(review.work_item_id, 19)
            base = make_config(global_max_active=1, repository_max_active=1)
            config = replace(
                base,
                scheduler=replace(base.scheduler, database_path=database),
                repositories=(
                    replace(
                        base.repositories[0],
                        slug=FIXTURE_REPOSITORY,
                        allowed_paths=("README.md",),
                        denied_paths=(),
                        maintainers=("longwdl",),
                        required_checks=("fixture",),
                    ),
                ),
            )
            merged = PullRequest(
                19,
                f"https://github.com/{FIXTURE_REPOSITORY}/pull/19",
                review.task_branch,
                "Fixture completion",
                False,
                "main",
                PullRequestState.MERGED,
                False,
                HEAD_SHA,
            )
            comment_plan = SshPreflightPlan(
                SshPreflightStatus.READY_RECOVERY,
                SshRecoveryAction.COMPLETE_MERGED_WORK_ITEM,
                task=_task(TaskState.REVIEW),
                work_item=review,
                pull_request=merged,
            )

            def build_comment_sweep(**kwargs):
                injection = kwargs["injection"]
                store = kwargs["store"]

                def run_once():
                    store.update_work_item_state(
                        review.work_item_id,
                        WorkItemState.COMPLETED,
                    )
                    injection.completion_identity_validated = True
                    injection.completion_comment_projected = True
                    injection.triggered = True
                    raise FixtureReceiptLost("completion comment receipt lost")

                return SimpleNamespace(run_once=run_once)

            environment = {
                "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS": FIXTURE_REPOSITORY,
                "GITHUB_TOKEN": "github_pat_fixture_test",
            }
            common_patches = (
                patch(
                    "codex_dispatcher.fixture_fault_cli.load_protected_ssh_config",
                    return_value=config,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.validate_runtime_state_path"
                ),
            )
            with (
                patch.dict("os.environ", environment, clear=True),
                common_patches[0],
                common_patches[1],
                patch(
                    "codex_dispatcher.fixture_fault_cli.run_ssh_preflight",
                    return_value=SshPreflightInspection(comment_plan, (), True),
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.build_ssh_fixture_fault_sweep",
                    side_effect=build_comment_sweep,
                ),
            ):
                code, comment = _run(
                    Path(root) / "config.toml",
                    issue_number=ISSUE,
                    fault=FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT,
                )

            self.assertEqual(0, code)
            self.assertTrue(comment["completion_comment_projected"])
            self.assertFalse(comment["completion_label_projected"])
            with StateStore(database, read_only=True) as store:
                completed = store.get_work_item(review.work_item_id)
            assert completed is not None
            label_plan = replace(
                comment_plan,
                recovery_action=SshRecoveryAction.SYNC_TRACKER_STATE,
                work_item=completed,
            )

            def build_label_sweep(**kwargs):
                injection = kwargs["injection"]

                def run_once():
                    injection.completion_identity_validated = True
                    injection.completion_comment_projected = True
                    injection.completion_label_projected = True
                    injection.triggered = True
                    raise FixtureReceiptLost("completion label receipt lost")

                return SimpleNamespace(run_once=run_once)

            with (
                patch.dict("os.environ", environment, clear=True),
                patch(
                    "codex_dispatcher.fixture_fault_cli.load_protected_ssh_config",
                    return_value=config,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.validate_runtime_state_path"
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.run_ssh_preflight",
                    return_value=SshPreflightInspection(label_plan, (), True),
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.build_ssh_fixture_fault_sweep",
                    side_effect=build_label_sweep,
                ),
            ):
                code, label = _run(
                    Path(root) / "config.toml",
                    issue_number=ISSUE,
                    fault=FixtureFaultPoint.COMPLETION_LABEL_RECEIPT,
                )

        self.assertEqual(0, code)
        self.assertTrue(label["completion_identity_validated"])
        self.assertTrue(label["completion_comment_projected"])
        self.assertTrue(label["completion_label_projected"])
        self.assertEqual(finished.turn_id, label["turn_id"])

    def test_cli_reports_an_expected_publisher_receipt_loss_as_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.db"
            with StateStore(database) as store:
                store.migrate()
            config = make_config(global_max_active=1, repository_max_active=1)
            config = replace(
                config,
                scheduler=replace(config.scheduler, database_path=database),
                repositories=(
                    replace(
                        config.repositories[0],
                        slug=FIXTURE_REPOSITORY,
                        allowed_paths=("README.md",),
                        denied_paths=(),
                        maintainers=("longwdl",),
                        required_checks=("fixture",),
                    ),
                ),
            )
            plan = SshPreflightPlan(
                SshPreflightStatus.READY_CANDIDATE,
                SshRecoveryAction.IDLE,
                task=_task(TaskState.READY),
            )

            def build_fixture_sweep(**kwargs):
                injection = kwargs["injection"]

                def run_once():
                    injection.triggered = True
                    return ControlSweepResult(
                        ControlSweepStatus.AWAITING_PUBLICATION,
                        FIXTURE_REPOSITORY,
                        ISSUE,
                        "wi_000000000000000000000000",
                        "turn_00000000000000000000000000000000",
                        "publication_outcome_ambiguous",
                    )

                return SimpleNamespace(run_once=run_once)

            with (
                patch.dict(
                    "os.environ",
                    {
                        "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                        "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS": FIXTURE_REPOSITORY,
                        "GITHUB_TOKEN": "github_pat_fixture_test",
                    },
                    clear=True,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.load_protected_ssh_config",
                    return_value=config,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.validate_runtime_state_path"
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.run_ssh_preflight",
                    return_value=SshPreflightInspection(plan, (), True),
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.build_ssh_fixture_fault_sweep",
                    side_effect=build_fixture_sweep,
                ),
            ):
                code, payload = _run(
                    Path(root) / "config.toml",
                    issue_number=ISSUE,
                    fault=FixtureFaultPoint.PUBLISHER_RECEIPT,
                )

        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["fault_triggered"])
        self.assertTrue(payload["recovery_required"])
        self.assertEqual("awaiting_publication", payload["status"])

    def test_cli_reports_guarded_recorded_publication_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.db"
            with StateStore(database) as store:
                store.migrate()
            config = make_config(global_max_active=1, repository_max_active=1)
            config = replace(
                config,
                scheduler=replace(config.scheduler, database_path=database),
                repositories=(
                    replace(
                        config.repositories[0],
                        slug=FIXTURE_REPOSITORY,
                        allowed_paths=("README.md",),
                        denied_paths=(),
                        maintainers=("longwdl",),
                        required_checks=("fixture",),
                    ),
                ),
            )
            recorded = replace(_running_item(), last_published_sha=HEAD_SHA)
            plan = SshPreflightPlan(
                SshPreflightStatus.READY_RECOVERY,
                SshRecoveryAction.RESUME_PUBLICATION,
                task=_task(TaskState.DISPATCHING),
                work_item=recorded,
                turn=_checkpoint_turn(recorded),
            )

            def build_fixture_sweep(**kwargs):
                def run_once():
                    return ControlSweepResult(
                        ControlSweepStatus.REVIEW,
                        FIXTURE_REPOSITORY,
                        ISSUE,
                        recorded.work_item_id,
                        plan.turn.turn_id,
                    )

                return SimpleNamespace(run_once=run_once)

            with (
                patch.dict(
                    "os.environ",
                    {
                        "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                        "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS": FIXTURE_REPOSITORY,
                        "GITHUB_TOKEN": "github_pat_fixture_test",
                    },
                    clear=True,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.load_protected_ssh_config",
                    return_value=config,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.validate_runtime_state_path"
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.run_ssh_preflight",
                    return_value=SshPreflightInspection(plan, (), True),
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.build_ssh_fixture_fault_sweep",
                    side_effect=build_fixture_sweep,
                ),
            ):
                code, payload = _run(
                    Path(root) / "config.toml",
                    issue_number=ISSUE,
                    fault=FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY,
                )

        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["fault_triggered"])
        self.assertTrue(payload["recovery_guarded"])
        self.assertFalse(payload["recovery_required"])
        self.assertEqual("review", payload["status"])

    def test_cli_reports_start_receipt_loss_then_status_only_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.db"
            with StateStore(database) as store:
                store.migrate()
            config = make_config(global_max_active=1, repository_max_active=1)
            config = replace(
                config,
                scheduler=replace(config.scheduler, database_path=database),
                repositories=(
                    replace(
                        config.repositories[0],
                        slug=FIXTURE_REPOSITORY,
                        allowed_paths=("README.md",),
                        denied_paths=(),
                        maintainers=("longwdl",),
                        required_checks=("fixture",),
                    ),
                ),
            )
            ready_plan = SshPreflightPlan(
                SshPreflightStatus.READY_CANDIDATE,
                SshRecoveryAction.IDLE,
                task=_task(TaskState.READY),
            )

            def build_start_sweep(**kwargs):
                injection = kwargs["injection"]
                store = kwargs["store"]

                def run_once():
                    ready = WorkItem.new(
                        repository=FIXTURE_REPOSITORY,
                        issue_number=ISSUE,
                        issue_node_id="I_fixture_fault_7",
                        base_branch="main",
                        base_sha=BASE_SHA,
                        at="2026-08-19T00:00:00Z",
                    )
                    ready = ready.transition_to(
                        WorkItemState.PREPARING,
                        at="2026-08-19T00:00:00Z",
                    ).transition_to(
                        WorkItemState.READY,
                        at="2026-08-19T00:00:00Z",
                    )
                    store.create_work_item(ready)
                    running, turn = store.begin_turn(
                        ready.work_item_id,
                        issue_revision="revision-1",
                        prompt_sha256="d" * 64,
                        input_head_sha=BASE_SHA,
                        issue_allowed_paths=("README.md",),
                        turn_id="turn_" + "3" * 32,
                    )
                    turn = store.update_turn_state(turn.turn_id, TurnState.STARTING)
                    turn = store.update_turn_state(turn.turn_id, TurnState.RECONCILING)
                    injection.triggered = True
                    return ControlSweepResult(
                        ControlSweepStatus.RUNNER_ACTIVE,
                        FIXTURE_REPOSITORY,
                        ISSUE,
                        running.work_item_id,
                        turn.turn_id,
                    )

                return SimpleNamespace(run_once=run_once)

            common_environment = {
                "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS": FIXTURE_REPOSITORY,
                "GITHUB_TOKEN": "github_pat_fixture_test",
            }
            with (
                patch.dict("os.environ", common_environment, clear=True),
                patch(
                    "codex_dispatcher.fixture_fault_cli.load_protected_ssh_config",
                    return_value=config,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.validate_runtime_state_path"
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.run_ssh_preflight",
                    return_value=SshPreflightInspection(ready_plan, (), True),
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.build_ssh_fixture_fault_sweep",
                    side_effect=build_start_sweep,
                ),
            ):
                code, first = _run(
                    Path(root) / "config.toml",
                    issue_number=ISSUE,
                    fault=FixtureFaultPoint.START_RECEIPT,
                )

            self.assertEqual(0, code)
            self.assertTrue(first["fault_triggered"])
            self.assertEqual("runner_active", first["status"])
            with StateStore(database, read_only=True) as store:
                running = store.get_work_item_by_issue(FIXTURE_REPOSITORY, ISSUE)
                active_turn = store.get_active_turn()
            assert running is not None and active_turn is not None
            recovery_plan = SshPreflightPlan(
                SshPreflightStatus.READY_RECOVERY,
                SshRecoveryAction.RECONCILE_ACTIVE_TURN,
                task=_task(TaskState.RUNNING),
                work_item=running,
                turn=active_turn,
            )

            def build_recovery_sweep(**kwargs):
                injection = kwargs["injection"]

                def run_once():
                    injection.recovery_operations.extend(
                        [RunnerOperation.STATUS, RunnerOperation.EXPORT]
                    )
                    return ControlSweepResult(
                        ControlSweepStatus.REVIEW,
                        FIXTURE_REPOSITORY,
                        ISSUE,
                        running.work_item_id,
                        active_turn.turn_id,
                    )

                return SimpleNamespace(run_once=run_once)

            with (
                patch.dict("os.environ", common_environment, clear=True),
                patch(
                    "codex_dispatcher.fixture_fault_cli.load_protected_ssh_config",
                    return_value=config,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.validate_runtime_state_path"
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.run_ssh_preflight",
                    return_value=SshPreflightInspection(recovery_plan, (), True),
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.build_ssh_fixture_fault_sweep",
                    side_effect=build_recovery_sweep,
                ),
            ):
                code, recovered = _run(
                    Path(root) / "config.toml",
                    issue_number=ISSUE,
                    fault=FixtureFaultPoint.START_STATUS_RECOVERY,
                )

        self.assertEqual(0, code)
        self.assertFalse(recovered["fault_triggered"])
        self.assertTrue(recovered["recovery_guarded"])
        self.assertEqual(["status", "export"], recovered["runner_operations"])
        self.assertEqual("review", recovered["status"])

    def test_cli_reports_exact_ssh_process_group_and_reconciling_turn(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.db"
            with StateStore(database) as store:
                store.migrate()
            config = make_config(global_max_active=1, repository_max_active=1)
            config = replace(
                config,
                scheduler=replace(config.scheduler, database_path=database),
                repositories=(
                    replace(
                        config.repositories[0],
                        slug=FIXTURE_REPOSITORY,
                        allowed_paths=("README.md",),
                        denied_paths=(),
                        maintainers=("longwdl",),
                        required_checks=("fixture",),
                    ),
                ),
            )
            ready_plan = SshPreflightPlan(
                SshPreflightStatus.READY_CANDIDATE,
                SshRecoveryAction.IDLE,
                task=_task(TaskState.READY),
            )

            def build_sweep(**kwargs):
                injection = kwargs["injection"]
                store = kwargs["store"]

                def run_once():
                    ready = WorkItem.new(
                        repository=FIXTURE_REPOSITORY,
                        issue_number=ISSUE,
                        issue_node_id="I_fixture_fault_7",
                        base_branch="main",
                        base_sha=BASE_SHA,
                        at="2026-08-19T00:00:00Z",
                    )
                    ready = ready.transition_to(
                        WorkItemState.PREPARING,
                        at="2026-08-19T00:00:00Z",
                    ).transition_to(
                        WorkItemState.READY,
                        at="2026-08-19T00:00:00Z",
                    )
                    store.create_work_item(ready)
                    running, turn = store.begin_turn(
                        ready.work_item_id,
                        issue_revision="revision-1",
                        prompt_sha256="d" * 64,
                        input_head_sha=BASE_SHA,
                        issue_allowed_paths=("README.md",),
                        turn_id="turn_" + "5" * 32,
                    )
                    turn = store.update_turn_state(turn.turn_id, TurnState.STARTING)
                    turn = store.update_turn_state(turn.turn_id, TurnState.RECONCILING)
                    injection.ssh_process_pid = 900
                    injection.ssh_process_group_id = 900
                    injection.ssh_session_id = 900
                    injection.ssh_status_state = RunnerTurnRemoteState.UNKNOWN
                    injection.ssh_status_error_code = "turn_outcome_unresolved"
                    injection.ssh_status_attempts = 1
                    injection.triggered = True
                    return ControlSweepResult(
                        ControlSweepStatus.RUNNER_ACTIVE,
                        FIXTURE_REPOSITORY,
                        ISSUE,
                        running.work_item_id,
                        turn.turn_id,
                    )

                return SimpleNamespace(run_once=run_once)

            with (
                patch.dict(
                    "os.environ",
                    {
                        "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                        "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS": FIXTURE_REPOSITORY,
                        "GITHUB_TOKEN": "github_pat_fixture_test",
                    },
                    clear=True,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.load_protected_ssh_config",
                    return_value=config,
                ),
                patch("codex_dispatcher.fixture_fault_cli.validate_runtime_state_path"),
                patch(
                    "codex_dispatcher.fixture_fault_cli.run_ssh_preflight",
                    return_value=SshPreflightInspection(ready_plan, (), True),
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.build_ssh_fixture_fault_sweep",
                    side_effect=build_sweep,
                ),
            ):
                code, payload = _run(
                    Path(root) / "config.toml",
                    issue_number=ISSUE,
                    fault=FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL,
                )

        self.assertEqual(0, code)
        self.assertTrue(payload["fault_triggered"])
        self.assertEqual("SIGKILL", payload["termination_signal"])
        self.assertEqual(900, payload["ssh_process_pid"])
        self.assertEqual(900, payload["ssh_process_group_id"])
        self.assertEqual(900, payload["ssh_session_id"])
        self.assertEqual("unknown", payload["status_proof_state"])
        self.assertEqual("turn_outcome_unresolved", payload["status_proof_error_code"])
        self.assertTrue(payload["local_work_item_persisted"])
        self.assertEqual("reconciling", payload["turn_state"])

    def test_backup_is_private_complete_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.db"
            with StateStore(database) as store:
                store.migrate()
            backup = _create_backup(database, FixtureFaultPoint.PUBLISHER_RECEIPT)

            self.assertEqual(0o600, backup.stat().st_mode & 0o777)
            with StateStore(backup, read_only=True) as store:
                self.assertEqual("ok", store.integrity_check())


if __name__ == "__main__":
    unittest.main()
