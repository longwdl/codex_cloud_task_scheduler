from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codex_dispatcher.repository_admission import RepositoryClass
from codex_dispatcher.ssh_preflight import (
    SshPreflightStatus,
    build_ssh_preflight_plan,
)
from codex_dispatcher.ssh_recovery import SshRecoveryAction
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.testing.fakes import FakeTracker
from codex_dispatcher.trackers.base import TaskState
from codex_dispatcher.work_items import TurnState, WorkItem, WorkItemState
from tests.test_scheduler import make_config, make_task
from tests.test_ssh_dispatch_planning import BASE_SHA, claimed_task


class SshPreflightPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "state.db")
        self.store.migrate()
        self.config = make_config(global_max_active=1)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_selects_one_ssh_candidate_after_recovery_is_idle(self) -> None:
        tracker = FakeTracker()
        tracker.ready_tasks = (
            make_task(1, executor_label="exec:cloud"),
            make_task(2, executor_label="exec:ssh-cli"),
        )

        plan = build_ssh_preflight_plan(self.config, self.store, tracker)

        self.assertIs(SshPreflightStatus.READY_CANDIDATE, plan.status)
        self.assertIs(SshRecoveryAction.IDLE, plan.recovery_action)
        assert plan.task is not None
        self.assertEqual(2, plan.task.issue_number)
        self.assertEqual(
            {1: "invalid_executor_labels"},
            {item.issue_number: item.code for item in plan.rejected},
        )
        self.assertFalse(
            any(
                call.method
                in {"claim", "set_state", "upsert_run_comment", "create_draft_pr"}
                for call in tracker.calls
            )
        )

    def test_recovery_is_reported_before_any_ready_candidate(self) -> None:
        tracker = FakeTracker()
        task = claimed_task()
        tracker.tasks[task.task_id] = task
        tracker.ready_tasks = (make_task(2, executor_label="exec:ssh-cli"),)
        item = WorkItem.new(
            repository=task.repository,
            issue_number=task.issue_number,
            issue_node_id=task.issue_node_id or "missing",
            base_branch="main",
            base_sha=BASE_SHA,
        )
        self.store.create_work_item(item)

        admission_disabled = replace(
            self.config,
            repositories=(
                replace(
                    self.config.repositories[0],
                    repository_class=RepositoryClass.UNCLASSIFIED,
                ),
            ),
            repository_admission=None,
        )
        plan = build_ssh_preflight_plan(admission_disabled, self.store, tracker)

        self.assertIs(SshPreflightStatus.READY_RECOVERY, plan.status)
        self.assertIs(SshRecoveryAction.RESUME_PREPARATION, plan.recovery_action)
        self.assertEqual(item, plan.work_item)
        self.assertIsNone(plan.turn)
        self.assertIsNotNone(plan.repository_recovery_receipt)
        self.assertIsNotNone(plan.repository_target_readback_verdict)
        self.assertFalse(any(call.method == "list_ready_tasks" for call in tracker.calls))

    def test_active_publication_recovery_preserves_the_exact_turn(self) -> None:
        tracker = FakeTracker()
        task = claimed_task()
        tracker.tasks[task.task_id] = task
        item = WorkItem.new(
            repository=task.repository,
            issue_number=task.issue_number,
            issue_node_id=task.issue_node_id or "missing",
            base_branch="main",
            base_sha=BASE_SHA,
        )
        self.store.create_work_item(item)
        self.store.update_work_item_state(item.work_item_id, WorkItemState.PREPARING)
        self.store.update_work_item_state(item.work_item_id, WorkItemState.READY)
        _, turn = self.store.begin_turn(
            item.work_item_id,
            issue_revision="revision-1",
            prompt_sha256="b" * 64,
            input_head_sha=BASE_SHA,
            issue_allowed_paths=("src",),
        )
        self.store.update_turn_state(turn.turn_id, TurnState.STARTING)
        turn = self.store.record_turn_result(
            turn.turn_id,
            output_sha256="c" * 64,
            output_head_sha="d" * 40,
            result_status="completed",
            result_summary="Fixture checkpoint",
        )
        turn = self.store.update_turn_state(turn.turn_id, TurnState.CHECKPOINTING)

        plan = build_ssh_preflight_plan(self.config, self.store, tracker)

        self.assertIs(SshPreflightStatus.READY_RECOVERY, plan.status)
        self.assertIs(SshRecoveryAction.RESUME_PUBLICATION, plan.recovery_action)
        self.assertEqual(turn, plan.turn)

    def test_ambiguous_remote_claims_fail_closed(self) -> None:
        tracker = FakeTracker()
        first = claimed_task(41)
        second = claimed_task(42)
        tracker.tasks = {first.task_id: first, second.task_id: second}

        plan = build_ssh_preflight_plan(self.config, self.store, tracker)

        self.assertIs(SshPreflightStatus.BLOCKED, plan.status)
        self.assertIs(SshRecoveryAction.BLOCK, plan.recovery_action)
        self.assertEqual("multiple_remote_claims", plan.reason)
        self.assertIsNone(plan.task)

    def test_terminal_work_item_remote_claim_plans_state_sync(self) -> None:
        tracker = FakeTracker()
        task = claimed_task()
        tracker.tasks[task.task_id] = task
        item = WorkItem.new(
            repository=task.repository,
            issue_number=task.issue_number,
            issue_node_id=task.issue_node_id or "missing",
            base_branch="main",
            base_sha=BASE_SHA,
        )
        self.store.create_work_item(item)
        self.store.update_work_item_state(item.work_item_id, WorkItemState.PREPARING)
        self.store.update_work_item_state(item.work_item_id, WorkItemState.READY)
        self.store.update_work_item_state(item.work_item_id, WorkItemState.RUNNING)
        terminal = self.store.update_work_item_state(
            item.work_item_id,
            WorkItemState.BLOCKED,
        )

        plan = build_ssh_preflight_plan(self.config, self.store, tracker)

        self.assertIs(SshPreflightStatus.READY_RECOVERY, plan.status)
        self.assertIs(SshRecoveryAction.SYNC_TRACKER_STATE, plan.recovery_action)
        self.assertEqual(terminal, plan.work_item)
        self.assertIs(TaskState.DISPATCHING, plan.task.state if plan.task else None)


if __name__ == "__main__":
    unittest.main()
