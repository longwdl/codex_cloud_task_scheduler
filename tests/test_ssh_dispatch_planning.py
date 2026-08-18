from __future__ import annotations

import unittest
from dataclasses import replace

from codex_dispatcher.ssh_dispatch_planning import (
    SshDispatchPlanningError,
    WorkItemAction,
    build_ssh_turn_plan,
    resolve_ssh_work_item,
)
from codex_dispatcher.trackers.base import TaskState, TrackerTask
from codex_dispatcher.work_items import WorkItem, WorkItemState
from tests.test_scheduler import make_config
from tests.test_task_spec import BODY


BASE_SHA = "a" * 40


def claimed_task(issue_number: int = 42) -> TrackerTask:
    return TrackerTask(
        repository="owner/repo",
        task_id=str(issue_number),
        issue_number=issue_number,
        title="Implement fixture",
        body=BODY,
        state=TaskState.DISPATCHING,
        labels=("agent:dispatching", "exec:ssh-cli", "priority:p1"),
        created_at="2026-08-13T00:00:00Z",
        ready_approved_by="alice",
        issue_node_id=f"I_kwDOFixture{issue_number}",
        updated_at="2026-08-13T01:00:00Z",
    )


def ready_work_item(issue_number: int = 42) -> WorkItem:
    item = WorkItem.new(
        repository="owner/repo",
        issue_number=issue_number,
        issue_node_id=f"I_kwDOFixture{issue_number}",
        base_branch="main",
        base_sha=BASE_SHA,
        at="2026-08-13T01:00:00Z",
    )
    return item.transition_to(
        WorkItemState.PREPARING, at="2026-08-13T01:01:00Z"
    ).transition_to(WorkItemState.READY, at="2026-08-13T01:02:00Z")


class SshDispatchPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = make_config().repositories[0]

    def test_new_issue_creates_deterministic_work_item_for_prepare(self) -> None:
        first = resolve_ssh_work_item(
            task=claimed_task(),
            repository=self.repository,
            base_sha=BASE_SHA,
            created_at="2026-08-13T02:00:00Z",
        )
        second = resolve_ssh_work_item(
            task=claimed_task(),
            repository=self.repository,
            base_sha=BASE_SHA,
            created_at="2026-08-13T03:00:00Z",
        )

        self.assertTrue(first.is_new)
        self.assertEqual(WorkItemAction.PREPARE, first.action)
        self.assertEqual(first.work_item.work_item_id, second.work_item.work_item_id)
        self.assertEqual(first.work_item.task_branch, second.work_item.task_branch)
        self.assertEqual(first.work_item.runner_directory, second.work_item.runner_directory)

    def test_existing_issue_reuses_binding_and_original_base_anchor(self) -> None:
        item = ready_work_item()

        resolution = resolve_ssh_work_item(
            task=claimed_task(),
            repository=self.repository,
            base_sha="b" * 40,
            existing_work_item=item,
        )

        self.assertFalse(resolution.is_new)
        self.assertIs(item, resolution.work_item)
        self.assertEqual(BASE_SHA, resolution.work_item.base_sha)
        self.assertEqual(WorkItemAction.START_TURN, resolution.action)

    def test_recoverable_states_reuse_the_same_work_item(self) -> None:
        discovered = WorkItem.new(
            repository="owner/repo",
            issue_number=42,
            issue_node_id="I_kwDOFixture42",
            base_branch="main",
            base_sha=BASE_SHA,
        )
        preparing = discovered.transition_to(WorkItemState.PREPARING)
        ready = preparing.transition_to(WorkItemState.READY)
        running = ready.transition_to(WorkItemState.RUNNING)
        recoverable = (
            (discovered, WorkItemAction.PREPARE),
            (preparing, WorkItemAction.RETRY_PREPARE),
            (running.transition_to(WorkItemState.WAITING_INPUT), WorkItemAction.REACTIVATE),
            (running.transition_to(WorkItemState.REVIEW), WorkItemAction.REACTIVATE),
            (running.transition_to(WorkItemState.BLOCKED), WorkItemAction.REACTIVATE),
            (discovered.transition_to(WorkItemState.PAUSED), WorkItemAction.REACTIVATE),
        )

        for item, action in recoverable:
            with self.subTest(state=item.state):
                resolution = resolve_ssh_work_item(
                    task=claimed_task(),
                    repository=self.repository,
                    base_sha="b" * 40,
                    existing_work_item=item,
                )
                self.assertIs(item, resolution.work_item)
                self.assertEqual(action, resolution.action)

    def test_running_and_completed_work_items_fail_closed(self) -> None:
        running = ready_work_item().transition_to(WorkItemState.RUNNING)
        completed = running.transition_to(WorkItemState.REVIEW).transition_to(
            WorkItemState.COMPLETED
        )
        for item, message in (
            (running, "reconciled"),
            (completed, "new Issue"),
        ):
            with self.subTest(state=item.state):
                with self.assertRaisesRegex(SshDispatchPlanningError, message):
                    resolve_ssh_work_item(
                        task=claimed_task(),
                        repository=self.repository,
                        base_sha=BASE_SHA,
                        existing_work_item=item,
                    )

    def test_immutable_issue_or_branch_conflicts_fail_closed(self) -> None:
        item = ready_work_item()
        replacements = (
            {"issue_node_id": "I_kwDOReplacement42"},
            {"branch_name": "codex/issue-42-other"},
        )
        for replacement in replacements:
            with self.subTest(replacement=replacement):
                with self.assertRaises(SshDispatchPlanningError):
                    resolve_ssh_work_item(
                        task=replace(claimed_task(), **replacement),
                        repository=self.repository,
                        base_sha=BASE_SHA,
                        existing_work_item=item,
                    )

    def test_turn_plan_freezes_revision_head_number_and_approved_context(self) -> None:
        item = ready_work_item()
        comments = (
            {"id": 2, "author": {"login": "alice"}, "body": "/codex-context\nUse A"},
            {"id": 1, "author": {"login": "mallory"}, "body": "/codex-context\nIgnore"},
        )

        plan = build_ssh_turn_plan(
            task=claimed_task(),
            repository=self.repository,
            work_item=item,
            turn_number=3,
            comments=comments,
        )

        self.assertEqual("2026-08-13T01:00:00Z", plan.issue_revision)
        self.assertEqual(3, plan.turn_number)
        self.assertEqual(BASE_SHA, plan.input_head_sha)
        self.assertEqual(("2",), plan.prompt.included_comment_ids)
        self.assertIn("Turn: 3", plan.prompt.content)
        self.assertIn("Issue revision: 2026-08-13T01:00:00Z", plan.prompt.content)

    def test_unclaimed_wrong_executor_or_missing_revision_is_rejected(self) -> None:
        invalid_tasks = (
            replace(
                claimed_task(),
                state=TaskState.READY,
                labels=("agent:ready", "exec:ssh-cli"),
            ),
            replace(claimed_task(), labels=("agent:dispatching", "exec:cloud")),
            replace(claimed_task(), updated_at=None),
        )
        for task in invalid_tasks:
            with self.subTest(labels=task.labels, updated_at=task.updated_at):
                with self.assertRaises(SshDispatchPlanningError):
                    resolve_ssh_work_item(
                        task=task,
                        repository=self.repository,
                        base_sha=BASE_SHA,
                    )


if __name__ == "__main__":
    unittest.main()
