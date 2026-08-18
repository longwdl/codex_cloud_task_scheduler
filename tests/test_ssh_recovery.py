from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codex_dispatcher.ssh_recovery import SshRecoveryAction, plan_ssh_recovery
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.testing.fakes import FakeTracker
from codex_dispatcher.trackers.base import (
    PullRequest,
    PullRequestState,
    TaskState,
)
from codex_dispatcher.work_items import TurnState, WorkItem, WorkItemState
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


class SshRecoveryTests(unittest.TestCase):
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
