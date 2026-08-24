from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from codex_dispatcher.runner_protocol import (
    NEXT_PROTOCOL_VERSION,
    RunnerOperation,
    RunnerRequest,
)
from codex_dispatcher.ssh_recovery import (
    SshRecoveryAction,
    TerminalBranchCleanupFixtureTarget,
    plan_ssh_recovery,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.testing.fakes import FakeTracker
from codex_dispatcher.trackers.base import (
    PullRequest,
    PullRequestState,
    TaskState,
)
from codex_dispatcher.work_items import TurnState, WorkItem, WorkItemState
from codex_dispatcher.work_item_lifecycle import (
    TerminalGithubClosureKind,
    TerminalGithubClosureOutcome,
    WorkItemDispositionKind,
)
from tests.test_scheduler import make_config
from tests.test_ssh_dispatch_planning import BASE_SHA, claimed_task


def item(issue_number: int = 42) -> WorkItem:
    return WorkItem.new(
        repository="owner/repo",
        issue_number=issue_number,
        issue_node_id=f"I_kwDOFixture{issue_number}",
        base_branch="main",
        base_sha=BASE_SHA,
        at=f"2026-01-{min(issue_number, 28):02d}T00:00:00Z",
    )


def task_in(state: TaskState, issue_number: int = 42):
    return replace(
        claimed_task(issue_number),
        state=state,
        labels=(f"agent:{state.value}", "exec:ssh-cli"),
    )


def review_item(issue_number: int = 42) -> WorkItem:
    work_item = item(issue_number)
    for state in (
        WorkItemState.PREPARING,
        WorkItemState.READY,
        WorkItemState.RUNNING,
        WorkItemState.REVIEW,
    ):
        work_item = work_item.transition_to(state)
    return replace(
        work_item,
        last_published_sha="d" * 40,
        pr_number=7,
    )


def pull_request_for(
    work_item: WorkItem,
    state: PullRequestState,
    *,
    head_sha: str = "d" * 40,
) -> PullRequest:
    return PullRequest(
        7,
        f"https://github.com/{work_item.repository}/pull/7",
        work_item.task_branch,
        "Codex work",
        state is PullRequestState.OPEN,
        work_item.base_branch,
        state,
        False,
        head_sha,
    )


def complete_issue_closure(
    store: StateStore,
    tracker: FakeTracker,
    work_item: WorkItem,
    kind: TerminalGithubClosureKind,
) -> None:
    store.prepare_terminal_github_closure(work_item.work_item_id, kind=kind)
    store.complete_terminal_github_closure(
        work_item.work_item_id,
        kind=kind,
        outcome=TerminalGithubClosureOutcome.CLOSED,
    )
    task = tracker.tasks[str(work_item.issue_number)]
    tracker.tasks[str(work_item.issue_number)] = replace(
        task,
        is_open=False,
        state_reason=(
            "completed"
            if kind is TerminalGithubClosureKind.COMPLETED_ISSUE
            else "not_planned"
        ),
    )


def prepare_discard_request(
    store: StateStore,
    work_item: WorkItem,
    *,
    pr_number: int | None,
    event_id: str,
    requested_at: str = "2026-08-23T01:00:00Z",
) -> None:
    store.prepare_work_item_discard_request(
        work_item.work_item_id,
        expected_head_sha=work_item.last_published_sha or work_item.base_sha,
        pr_number=pr_number,
        requested_by="alice",
        request_event_id=event_id,
        requested_at=requested_at,
    )


def complete_discard_pr_closure(store: StateStore, work_item: WorkItem) -> None:
    store.prepare_terminal_github_closure(
        work_item.work_item_id,
        kind=TerminalGithubClosureKind.DISCARDED_PULL_REQUEST,
    )
    store.complete_terminal_github_closure(
        work_item.work_item_id,
        kind=TerminalGithubClosureKind.DISCARDED_PULL_REQUEST,
        outcome=TerminalGithubClosureOutcome.CLOSED,
    )


class SshRecoveryTests(unittest.TestCase):
    def test_discard_freezes_an_active_turn_and_rejects_its_checkpoint(self) -> None:
        work_item = item(19)
        self.store.create_work_item(work_item)
        self.store.update_work_item_state(
            work_item.work_item_id, WorkItemState.PREPARING
        )
        self.store.update_work_item_state(work_item.work_item_id, WorkItemState.READY)
        _, turn = self.store.begin_turn(
            work_item.work_item_id,
            issue_revision="revision-1",
            prompt_sha256="b" * 64,
            input_head_sha=work_item.base_sha,
        )
        turn = self.store.update_turn_state(turn.turn_id, TurnState.STARTING)
        discarded = replace(
            task_in(TaskState.DISCARD, 19),
            state_approved_by="alice",
            state_approval_event_id="1901",
            state_approved_at="2026-08-23T01:00:00Z",
        )
        self.tracker.tasks["19"] = discarded

        planned = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertIs(SshRecoveryAction.PREPARE_WORK_ITEM_DISCARD, planned.action)
        prepare_discard_request(
            self.store,
            work_item,
            pr_number=None,
            event_id="1901",
        )
        draining = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertIs(SshRecoveryAction.RECONCILE_ACTIVE_TURN, draining.action)

        self.store.record_turn_result(
            turn.turn_id,
            output_sha256="c" * 64,
            output_head_sha="d" * 40,
            result_status="completed",
            result_summary="must not publish",
        )
        self.store.update_turn_state(turn.turn_id, TurnState.CHECKPOINTING)
        reject = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertIs(SshRecoveryAction.REJECT_DISCARDED_TURN_RESULT, reject.action)

    def test_trusted_discard_records_terminal_intent_then_archives(self) -> None:
        work_item = item(20)
        self.store.create_work_item(work_item)
        self.store.update_work_item_state(
            work_item.work_item_id, WorkItemState.PREPARING
        )
        work_item = self.store.update_work_item_state(
            work_item.work_item_id, WorkItemState.BLOCKED
        )
        discarded = replace(
            task_in(TaskState.DISCARD, 20),
            state_approved_by="alice",
            state_approval_event_id="2001",
            state_approved_at="2026-08-23T01:00:00Z",
        )
        self.tracker.tasks["20"] = discarded

        planned = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertIs(SshRecoveryAction.PREPARE_WORK_ITEM_DISCARD, planned.action)
        self.assertIsNone(planned.disposition_pr_number)
        self.store.prepare_work_item_discard_request(
            work_item.work_item_id,
            expected_head_sha=work_item.base_sha,
            pr_number=planned.disposition_pr_number,
            requested_by=planned.disposition_requested_by or "",
            request_event_id=planned.disposition_request_event_id or "",
            requested_at=planned.disposition_requested_at or "",
        )
        finalize = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertIs(SshRecoveryAction.RECORD_WORK_ITEM_DISPOSITION, finalize.action)
        self.store.record_discarded_work_item(work_item.work_item_id)
        close_issue = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertIs(SshRecoveryAction.CLOSE_TERMINAL_ISSUE, close_issue.action)
        complete_issue_closure(
            self.store,
            self.tracker,
            work_item,
            TerminalGithubClosureKind.DISCARDED_ISSUE,
        )
        archive = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertIs(SshRecoveryAction.ARCHIVE_DISPOSED_WORK_ITEM, archive.action)
        self.assertIsNotNone(archive.archive_eligible_at)

    def test_discard_recovers_exact_unbound_pr_before_classification(self) -> None:
        work_item = replace(review_item(23), pr_number=None)
        self.store.create_work_item(work_item)
        self.tracker.tasks["23"] = replace(
            task_in(TaskState.DISCARD, 23),
            state_approved_by="alice",
            state_approval_event_id="2301",
            state_approved_at="2026-08-23T02:00:00Z",
        )
        pull_request = PullRequest(
            17,
            "https://github.com/owner/repo/pull/17",
            work_item.task_branch,
            "Superseded fixture",
            True,
            work_item.base_branch,
            PullRequestState.OPEN,
            False,
            work_item.last_published_sha,
        )
        self.tracker.pull_requests[(work_item.repository, work_item.task_branch)] = (
            pull_request
        )
        planned = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertIs(SshRecoveryAction.PREPARE_WORK_ITEM_DISCARD, planned.action)
        self.assertEqual(17, planned.disposition_pr_number)

    def test_disposition_revalidates_pr_state_before_archive(self) -> None:
        abandoned = item(20).transition_to(WorkItemState.PREPARING)
        self.store.create_work_item(abandoned)
        prepare_discard_request(
            self.store,
            abandoned,
            pr_number=None,
            event_id="2001",
        )
        self.store.record_discarded_work_item(abandoned.work_item_id)
        self.tracker.tasks["20"] = replace(
            task_in(TaskState.DISCARD, 20),
            state_approved_by="alice",
            state_approval_event_id="2001",
            state_approved_at="2026-08-23T01:00:00Z",
        )
        self.tracker.pull_requests[
            (abandoned.repository, abandoned.task_branch)
        ] = PullRequest(
            9,
            "https://github.com/owner/repo/pull/9",
            abandoned.task_branch,
            "Late PR",
            True,
            abandoned.base_branch,
            PullRequestState.OPEN,
            False,
            abandoned.base_sha,
        )
        appeared = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertEqual(SshRecoveryAction.BLOCK, appeared.action)
        self.assertEqual(
            "discarded_without_pr_pull_request_appeared", appeared.reason
        )

    def test_superseded_disposition_blocks_if_pr_is_later_merged(self) -> None:
        work_item = review_item(23)
        self.store.create_work_item(work_item)
        prepare_discard_request(
            self.store,
            work_item,
            pr_number=work_item.pr_number,
            event_id="2301",
        )
        complete_discard_pr_closure(self.store, work_item)
        self.store.record_discarded_work_item(work_item.work_item_id)
        self.tracker.tasks["23"] = replace(
            task_in(TaskState.DISCARD, 23),
            state_approved_by="alice",
            state_approval_event_id="2301",
            state_approved_at="2026-08-23T01:00:00Z",
        )
        self.tracker.pull_requests[
            (work_item.repository, work_item.task_branch)
        ] = pull_request_for(work_item, PullRequestState.MERGED)
        merged = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertEqual(SshRecoveryAction.BLOCK, merged.action)
        self.assertEqual(
            "merged_pull_request_requires_completion", merged.reason
        )

    def test_archived_or_absence_reconciled_dispositions_are_terminal_overlays(self) -> None:
        for issue_number, work_item in (
            (21, item(21).transition_to(WorkItemState.PREPARING).transition_to(WorkItemState.READY)),
            (23, review_item(23)),
        ):
            with self.subTest(issue_number=issue_number):
                self.store.create_work_item(work_item)
                pr_number = work_item.pr_number
                kind = (
                    WorkItemDispositionKind.SUPERSEDED
                    if pr_number is not None
                    else WorkItemDispositionKind.ABANDONED
                )
                expected_head_sha = work_item.last_published_sha or work_item.base_sha
                prepare_discard_request(
                    self.store,
                    work_item,
                    pr_number=pr_number,
                    event_id=str(2000 + issue_number),
                )
                if pr_number is not None:
                    complete_discard_pr_closure(self.store, work_item)
                disposition = self.store.record_discarded_work_item(
                    work_item.work_item_id
                )
                request = RunnerRequest(
                    RunnerOperation.ARCHIVE,
                    work_item.work_item_id,
                    version=NEXT_PROTOCOL_VERSION,
                    expected_head_sha=expected_head_sha,
                )
                self.store.prepare_work_item_archive(
                    work_item.work_item_id,
                    expected_head_sha=expected_head_sha,
                    eligible_at=disposition.eligible_at,
                    request_sha256=sha256(
                        request.to_json().encode("utf-8")
                    ).hexdigest(),
                )
                self.store.record_work_item_absence_reconciliation(
                    work_item.work_item_id,
                    expected_head_sha=expected_head_sha,
                    evidence_sha256=str(issue_number % 10) * 64,
                    observed_by="operator",
                    observed_at="2026-08-23T01:01:00Z",
                )
                self.tracker.tasks[str(issue_number)] = replace(
                    task_in(TaskState.DISCARD, issue_number),
                    state_approved_by="alice",
                    state_approval_event_id=str(2000 + issue_number),
                    state_approved_at="2026-08-23T01:00:00Z",
                )
                if pr_number is not None:
                    self.tracker.pull_requests[
                        (work_item.repository, work_item.task_branch)
                    ] = pull_request_for(work_item, PullRequestState.CLOSED)
                complete_issue_closure(
                    self.store,
                    self.tracker,
                    work_item,
                    TerminalGithubClosureKind.DISCARDED_ISSUE,
                )

        plan = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertEqual(SshRecoveryAction.IDLE, plan.action)

        self.tracker.calls.clear()
        incremental = plan_ssh_recovery(
            self.config,
            self.store,
            self.tracker,
            audit_terminal=False,
        )
        self.assertEqual(SshRecoveryAction.IDLE, incremental.action)
        self.assertFalse(
            any(
                call.method in {"get_task", "find_pr_by_branch"}
                for call in self.tracker.calls
            )
        )

    def test_completed_retention_plans_one_archive_and_reconciles_ambiguity(self) -> None:
        work_item = review_item()
        self.store.create_work_item(work_item)
        completed = self.store.update_work_item_state(
            work_item.work_item_id,
            WorkItemState.COMPLETED,
            updated_at="2026-01-10T00:00:00+00:00",
        )
        self.tracker.tasks["42"] = task_in(TaskState.COMPLETED)
        self.tracker.pull_requests[(completed.repository, completed.task_branch)] = (
            pull_request_for(completed, PullRequestState.MERGED)
        )
        configured = replace(
            self.config,
            ssh_runtime=SimpleNamespace(completed_retention_seconds=7 * 24 * 3600),
        )
        complete_issue_closure(
            self.store,
            self.tracker,
            completed,
            TerminalGithubClosureKind.COMPLETED_ISSUE,
        )

        retained = plan_ssh_recovery(
            configured,
            self.store,
            self.tracker,
            now=datetime(2026, 1, 16, tzinfo=timezone.utc),
        )
        self.assertEqual(SshRecoveryAction.IDLE, retained.action)
        planned = plan_ssh_recovery(
            configured,
            self.store,
            self.tracker,
            now=datetime(2026, 1, 17, tzinfo=timezone.utc),
        )
        self.assertEqual(SshRecoveryAction.ARCHIVE_COMPLETED_WORK_ITEM, planned.action)
        self.assertEqual("2026-01-17T00:00:00+00:00", planned.archive_eligible_at)

        request = RunnerRequest(
            RunnerOperation.ARCHIVE,
            completed.work_item_id,
            version=NEXT_PROTOCOL_VERSION,
            expected_head_sha=completed.last_published_sha,
        )
        self.store.prepare_work_item_archive(
            completed.work_item_id,
            expected_head_sha=completed.last_published_sha or "",
            eligible_at=planned.archive_eligible_at or "",
            request_sha256=sha256(request.to_json().encode("utf-8")).hexdigest(),
        )
        disabled = replace(configured, ssh_runtime=None)
        durable = plan_ssh_recovery(
            disabled,
            self.store,
            self.tracker,
            now=datetime(2026, 1, 11, tzinfo=timezone.utc),
        )
        self.assertEqual(
            SshRecoveryAction.ARCHIVE_COMPLETED_WORK_ITEM,
            durable.action,
        )
        self.assertEqual(planned.archive_eligible_at, durable.archive_eligible_at)

        self.store.mark_work_item_archive_ambiguous(completed.work_item_id)
        reconcile = plan_ssh_recovery(
            disabled,
            self.store,
            self.tracker,
            now=datetime(2026, 1, 11, tzinfo=timezone.utc),
        )
        self.assertEqual(
            SshRecoveryAction.RECONCILE_WORK_ITEM_ARCHIVE,
            reconcile.action,
        )

    def test_terminal_branch_cleanup_requires_runner_evidence_and_exact_head(self) -> None:
        work_item = review_item(22)
        self.store.create_work_item(work_item)
        completed = self.store.update_work_item_state(
            work_item.work_item_id,
            WorkItemState.COMPLETED,
            updated_at="2026-08-20T00:00:00+00:00",
        )
        task = task_in(TaskState.COMPLETED, 22)
        self.tracker.tasks["22"] = task
        self.tracker.pull_requests[(completed.repository, completed.task_branch)] = (
            pull_request_for(completed, PullRequestState.MERGED)
        )
        complete_issue_closure(
            self.store,
            self.tracker,
            completed,
            TerminalGithubClosureKind.COMPLETED_ISSUE,
        )
        request = RunnerRequest(
            RunnerOperation.ARCHIVE,
            completed.work_item_id,
            version=NEXT_PROTOCOL_VERSION,
            expected_head_sha=completed.last_published_sha,
        )
        self.store.prepare_work_item_archive(
            completed.work_item_id,
            expected_head_sha=completed.last_published_sha or "",
            eligible_at="2026-08-20T00:00:00+00:00",
            request_sha256=sha256(request.to_json().encode("utf-8")).hexdigest(),
        )
        self.store.record_work_item_absence_reconciliation(
            completed.work_item_id,
            expected_head_sha=completed.last_published_sha or "",
            evidence_sha256="e" * 64,
            observed_by="operator",
            observed_at="2026-08-20T00:00:01+00:00",
        )
        configured = replace(
            self.config,
            ssh_runtime=SimpleNamespace(
                completed_retention_seconds=None,
                terminal_branch_retention_seconds=3600,
                terminal_branch_retention_cutover_at=datetime(
                    2026, 8, 20, 0, 0, 2, tzinfo=timezone.utc
                ),
            ),
        )
        self.tracker.branches[(completed.repository, completed.task_branch)] = "d" * 40

        before_cutover = plan_ssh_recovery(
            configured,
            self.store,
            self.tracker,
            now=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )
        self.assertIs(SshRecoveryAction.IDLE, before_cutover.action)

        configured = replace(
            configured,
            ssh_runtime=SimpleNamespace(
                completed_retention_seconds=None,
                terminal_branch_retention_seconds=3600,
                terminal_branch_retention_cutover_at=datetime(
                    2026, 8, 20, 0, 0, 1, tzinfo=timezone.utc
                ),
            ),
        )
        planned = plan_ssh_recovery(
            configured,
            self.store,
            self.tracker,
            now=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )
        self.assertIs(SshRecoveryAction.DELETE_TERMINAL_BRANCH, planned.action)
        self.assertEqual(
            "2026-08-20T01:00:01+00:00", planned.branch_cleanup_eligible_at
        )

        self.tracker.branches[(completed.repository, completed.task_branch)] = "f" * 40
        blocked = plan_ssh_recovery(
            configured,
            self.store,
            self.tracker,
            now=datetime(2026, 8, 21, tzinfo=timezone.utc),
        )
        self.assertEqual("terminal_branch_head_conflict", blocked.reason)

    def test_fixture_target_selects_only_exact_terminal_branch_without_config_change(
        self,
    ) -> None:
        work_item = review_item(23)
        self.store.create_work_item(work_item)
        completed = self.store.update_work_item_state(
            work_item.work_item_id,
            WorkItemState.COMPLETED,
            updated_at="2026-08-20T00:00:00+00:00",
        )
        self.tracker.tasks["23"] = task_in(TaskState.COMPLETED, 23)
        self.tracker.pull_requests[(completed.repository, completed.task_branch)] = (
            pull_request_for(completed, PullRequestState.MERGED)
        )
        complete_issue_closure(
            self.store,
            self.tracker,
            completed,
            TerminalGithubClosureKind.COMPLETED_ISSUE,
        )
        self.tracker.branches[(completed.repository, completed.task_branch)] = "d" * 40
        archive_request = RunnerRequest(
            RunnerOperation.ARCHIVE,
            completed.work_item_id,
            version=NEXT_PROTOCOL_VERSION,
            expected_head_sha=completed.last_published_sha,
        )
        self.store.prepare_work_item_archive(
            completed.work_item_id,
            expected_head_sha=completed.last_published_sha or "",
            eligible_at="2026-08-20T00:00:00+00:00",
            request_sha256=sha256(
                archive_request.to_json().encode("utf-8")
            ).hexdigest(),
        )
        self.store.record_work_item_absence_reconciliation(
            completed.work_item_id,
            expected_head_sha=completed.last_published_sha or "",
            evidence_sha256="e" * 64,
            observed_by="operator",
            observed_at="2026-08-20T00:00:01+00:00",
        )
        configured = replace(
            self.config,
            ssh_runtime=SimpleNamespace(
                completed_retention_seconds=None,
                terminal_branch_retention_seconds=30 * 24 * 60 * 60,
            ),
        )
        observed_at = datetime(2026, 8, 20, 0, 1, tzinfo=timezone.utc)

        ordinary = plan_ssh_recovery(
            configured,
            self.store,
            self.tracker,
            now=observed_at,
        )
        exact = plan_ssh_recovery(
            configured,
            self.store,
            self.tracker,
            now=observed_at,
            terminal_branch_cleanup_fixture_target=(
                TerminalBranchCleanupFixtureTarget(completed.work_item_id)
            ),
        )

        self.assertIs(SshRecoveryAction.IDLE, ordinary.action)
        self.assertIs(SshRecoveryAction.DELETE_TERMINAL_BRANCH, exact.action)
        self.assertEqual(completed.work_item_id, exact.work_item.work_item_id)
        self.assertEqual(
            "2026-08-20T00:00:01+00:00",
            exact.branch_cleanup_eligible_at,
        )

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "state.db")
        self.store.migrate()
        self.tracker = FakeTracker()
        self.config = make_config()

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_idle_when_no_local_or_remote_claim_exists(self) -> None:
        plan = plan_ssh_recovery(self.config, self.store, self.tracker)

        self.assertEqual(SshRecoveryAction.IDLE, plan.action)
        self.assertEqual(2, len(self.tracker.calls))

    def test_active_turn_is_always_reconciled_before_any_remote_scan(self) -> None:
        work_item = item()
        self.store.create_work_item(work_item)
        self.store.update_work_item_state(work_item.work_item_id, WorkItemState.PREPARING)
        self.store.update_work_item_state(work_item.work_item_id, WorkItemState.READY)
        _, turn = self.store.begin_turn(
            work_item.work_item_id,
            issue_revision="revision-1",
            prompt_sha256="b" * 64,
            input_head_sha=BASE_SHA,
        )
        turn = self.store.update_turn_state(turn.turn_id, TurnState.STARTING)
        self.tracker.tasks["42"] = task_in(TaskState.RUNNING)

        plan = plan_ssh_recovery(self.config, self.store, self.tracker)

        self.assertEqual(SshRecoveryAction.RECONCILE_ACTIVE_TURN, plan.action)
        self.assertEqual(turn.turn_id, plan.turn.turn_id)
        self.assertIsNotNone(plan.repository_recovery_receipt)
        self.assertIsNotNone(plan.repository_target_readback_verdict)
        assert plan.repository_recovery_receipt is not None
        assert plan.repository_target_readback_verdict is not None
        self.assertEqual("legacy-unbound-recovery-v1", plan.repository_recovery_receipt.recovery_profile)
        self.assertEqual("allowed", plan.repository_recovery_receipt.decision)
        self.assertEqual("passed", plan.repository_target_readback_verdict.status)
        self.assertEqual("get_task", self.tracker.calls[0].method)
        self.assertEqual(1, len(self.tracker.calls))

    def test_planned_turn_blocks_and_checkpoint_resumes_publication(self) -> None:
        work_item = item()
        self.store.create_work_item(work_item)
        self.store.update_work_item_state(work_item.work_item_id, WorkItemState.PREPARING)
        self.store.update_work_item_state(work_item.work_item_id, WorkItemState.READY)
        _, turn = self.store.begin_turn(
            work_item.work_item_id,
            issue_revision="revision-1",
            prompt_sha256="b" * 64,
            input_head_sha=BASE_SHA,
        )
        self.tracker.tasks["42"] = task_in(TaskState.RUNNING)

        planned = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertEqual(SshRecoveryAction.BLOCK, planned.action)
        self.assertEqual("planned_turn_prompt_is_not_recoverable", planned.reason)

        self.store.update_turn_state(turn.turn_id, TurnState.STARTING)
        self.store.record_turn_result(
            turn.turn_id,
            output_sha256="c" * 64,
            output_head_sha="d" * 40,
            result_status="completed",
            result_summary="Checkpoint ready",
        )
        self.store.update_turn_state(turn.turn_id, TurnState.CHECKPOINTING)
        publication = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertEqual(SshRecoveryAction.RESUME_PUBLICATION, publication.action)

    def test_preparing_and_ready_work_items_resume_without_new_claim(self) -> None:
        for state, expected in (
            (WorkItemState.PREPARING, SshRecoveryAction.RESUME_PREPARATION),
            (WorkItemState.READY, SshRecoveryAction.START_CLAIMED_TURN),
        ):
            with self.subTest(state=state):
                work_item = item()
                if state is WorkItemState.PREPARING:
                    work_item = work_item.transition_to(state)
                else:
                    work_item = work_item.transition_to(
                        WorkItemState.PREPARING
                    ).transition_to(WorkItemState.READY)
                self.store.create_work_item(work_item)
                self.tracker.tasks["42"] = task_in(TaskState.DISPATCHING)

                plan = plan_ssh_recovery(self.config, self.store, self.tracker)
                self.assertEqual(expected, plan.action)

                self.store.close()
                self.temp_dir.cleanup()
                self.setUp()

    def test_orphan_dispatching_claim_can_be_recovered_but_running_cannot(self) -> None:
        dispatching = task_in(TaskState.DISPATCHING)
        self.tracker.tasks["42"] = dispatching
        plan = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertEqual(SshRecoveryAction.RECOVER_ORPHAN_CLAIM, plan.action)

        self.tracker.tasks["42"] = task_in(TaskState.RUNNING)
        plan = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertEqual(SshRecoveryAction.BLOCK, plan.action)
        self.assertEqual("orphan_running_issue", plan.reason)

    def test_identity_conflict_and_running_without_turn_block(self) -> None:
        work_item = item()
        self.store.create_work_item(work_item)
        self.store.update_work_item_state(work_item.work_item_id, WorkItemState.PREPARING)
        self.store.update_work_item_state(work_item.work_item_id, WorkItemState.READY)
        self.store.update_work_item_state(work_item.work_item_id, WorkItemState.RUNNING)
        self.tracker.tasks["42"] = task_in(TaskState.RUNNING)
        plan = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertEqual("running_work_item_has_no_active_turn", plan.reason)

        self.tracker.tasks["42"] = replace(
            task_in(TaskState.RUNNING), issue_node_id="I_kwDOReplacement42"
        )
        plan = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertEqual("persisted_issue_identity_conflict", plan.reason)

    def test_multiple_pending_or_remote_claims_block_globally(self) -> None:
        self.store.create_work_item(item(42))
        self.store.create_work_item(item(43))
        plan = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertEqual("multiple_pending_work_items", plan.reason)

        self.store.close()
        self.temp_dir.cleanup()
        self.setUp()
        self.tracker.tasks["42"] = task_in(TaskState.DISPATCHING, 42)
        self.tracker.tasks["43"] = task_in(TaskState.DISPATCHING, 43)
        plan = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertEqual("multiple_remote_claims", plan.reason)

    def test_lost_terminal_tracker_write_is_planned_for_idempotent_sync(self) -> None:
        for remote_state in (TaskState.DISPATCHING, TaskState.RUNNING):
            with self.subTest(remote_state=remote_state):
                work_item = item()
                work_item = work_item.transition_to(WorkItemState.BLOCKED)
                self.store.create_work_item(work_item)
                self.tracker.tasks["42"] = task_in(remote_state)

                plan = plan_ssh_recovery(self.config, self.store, self.tracker)

                self.assertEqual(SshRecoveryAction.SYNC_TRACKER_STATE, plan.action)
                self.assertEqual(TaskState.BLOCKED, plan.desired_task_state)
                self.assertEqual(work_item.work_item_id, plan.work_item.work_item_id)

                self.store.close()
                self.temp_dir.cleanup()
                self.setUp()

    def test_merged_bound_pr_plans_completion_only_for_the_exact_head(self) -> None:
        work_item = review_item()
        self.store.create_work_item(work_item)
        self.tracker.tasks["42"] = task_in(TaskState.REVIEW)
        self.tracker.pull_requests[(work_item.repository, work_item.task_branch)] = (
            pull_request_for(work_item, PullRequestState.OPEN)
        )

        open_plan = plan_ssh_recovery(self.config, self.store, self.tracker)

        self.assertEqual(SshRecoveryAction.IDLE, open_plan.action)
        self.tracker.pull_requests[(work_item.repository, work_item.task_branch)] = (
            pull_request_for(work_item, PullRequestState.MERGED)
        )
        merged = plan_ssh_recovery(self.config, self.store, self.tracker)

        self.assertEqual(SshRecoveryAction.COMPLETE_MERGED_WORK_ITEM, merged.action)
        self.assertEqual(work_item.work_item_id, merged.work_item.work_item_id)
        self.assertEqual(7, merged.pull_request.number)

        self.tracker.pull_requests[(work_item.repository, work_item.task_branch)] = (
            pull_request_for(
                work_item,
                PullRequestState.MERGED,
                head_sha="e" * 40,
            )
        )
        conflict = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertEqual(SshRecoveryAction.BLOCK, conflict.action)
        self.assertEqual("persisted_pull_request_head_conflict", conflict.reason)

    def test_completed_local_item_retries_only_issue_projection(self) -> None:
        work_item = review_item()
        work_item = work_item.transition_to(WorkItemState.COMPLETED)
        self.store.create_work_item(work_item)
        self.tracker.tasks["42"] = task_in(TaskState.REVIEW)
        self.tracker.pull_requests[(work_item.repository, work_item.task_branch)] = (
            pull_request_for(work_item, PullRequestState.MERGED)
        )

        plan = plan_ssh_recovery(self.config, self.store, self.tracker)

        self.assertEqual(SshRecoveryAction.SYNC_TRACKER_STATE, plan.action)
        self.assertEqual(TaskState.COMPLETED, plan.desired_task_state)
        self.assertEqual(7, plan.pull_request.number)

    def test_closed_unmerged_or_premature_completed_issue_blocks(self) -> None:
        work_item = review_item()
        self.store.create_work_item(work_item)
        self.tracker.tasks["42"] = task_in(TaskState.REVIEW)
        self.tracker.pull_requests[(work_item.repository, work_item.task_branch)] = (
            pull_request_for(work_item, PullRequestState.CLOSED)
        )

        closed = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertEqual("review_pull_request_closed_without_merge", closed.reason)

        self.tracker.tasks["42"] = task_in(TaskState.COMPLETED)
        self.tracker.pull_requests[(work_item.repository, work_item.task_branch)] = (
            pull_request_for(work_item, PullRequestState.OPEN)
        )
        premature = plan_ssh_recovery(self.config, self.store, self.tracker)
        self.assertEqual("completed_issue_pull_request_not_merged", premature.reason)


if __name__ == "__main__":
    unittest.main()
