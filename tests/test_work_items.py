from __future__ import annotations

import unittest
from dataclasses import replace

from codex_dispatcher.domain import InvalidStateTransition
from codex_dispatcher.work_items import (
    TaskBranchSource,
    Turn,
    TurnState,
    WorkItem,
    WorkItemState,
    stable_work_item_identity,
)


SESSION = "123e4567-e89b-12d3-a456-426614174000"


def work_item() -> WorkItem:
    return WorkItem.new(
        repository="owner/repo",
        issue_number=42,
        issue_node_id="I_kwDOFixture42",
        base_branch="main",
        base_sha="a" * 40,
        at="2026-01-01T00:00:00.000000Z",
    )


class WorkItemDomainTests(unittest.TestCase):
    def test_identity_is_stable_and_does_not_contain_an_attempt(self) -> None:
        first = stable_work_item_identity(
            repository="owner/repo",
            issue_number=42,
            issue_node_id="I_kwDOFixture42",
        )
        second = stable_work_item_identity(
            repository="owner/repo",
            issue_number=42,
            issue_node_id="I_kwDOFixture42",
        )
        different = stable_work_item_identity(
            repository="owner/repo",
            issue_number=42,
            issue_node_id="I_kwDOFixtureReplacement",
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first.work_item_id, different.work_item_id)
        self.assertRegex(first.work_item_id, r"^wi_[0-9a-f]{24}$")
        self.assertRegex(first.task_branch, r"^codex/issue-42-[0-9a-f]{12}$")
        self.assertEqual(
            "/srv/codex-runner/work-items/owner__repo/issue-42", first.runner_directory
        )
        self.assertNotIn("attempt", first.task_branch)

    def test_state_changes_preserve_identity_and_completed_is_terminal(self) -> None:
        item = work_item()
        identity = (item.work_item_id, item.task_branch, item.runner_directory)
        for state in (
            WorkItemState.PREPARING,
            WorkItemState.READY,
            WorkItemState.RUNNING,
            WorkItemState.REVIEW,
            WorkItemState.COMPLETED,
        ):
            item = item.transition_to(state)
            self.assertEqual(identity, (item.work_item_id, item.task_branch, item.runner_directory))
        with self.assertRaises(InvalidStateTransition):
            item.transition_to(WorkItemState.READY)

    def test_verified_existing_branch_binding_is_preserved_across_migration(self) -> None:
        item = WorkItem.from_existing_branch_binding(
            repository="owner/repo",
            issue_number=42,
            issue_node_id="I_kwDOFixture42",
            base_branch="main",
            base_sha="a" * 40,
            task_branch="codex/issue-42-8e3775879000",
            at="2026-01-01T00:00:00.000000Z",
        )
        self.assertEqual(TaskBranchSource.MIGRATED, item.task_branch_source)
        self.assertEqual("codex/issue-42-8e3775879000", item.task_branch)
        self.assertEqual(
            item.task_branch,
            item.transition_to(WorkItemState.PREPARING).task_branch,
        )
        with self.assertRaisesRegex(ValueError, "legacy stable branch shape"):
            WorkItem.from_existing_branch_binding(
                repository="owner/repo",
                issue_number=42,
                issue_node_id="I_kwDOFixture42",
                base_branch="main",
                base_sha="a" * 40,
                task_branch="codex/issue-42-wrong",
            )

    def test_session_and_slack_bindings_are_idempotent_but_not_replaceable(self) -> None:
        item = work_item().bind_session(SESSION)
        self.assertIs(item, item.bind_session(SESSION))
        with self.assertRaisesRegex(ValueError, "different Codex session"):
            item.bind_session("223e4567-e89b-12d3-a456-426614174000")

        item = item.bind_slack_thread("C0BR2D0MS8Y", "1234567890.123456")
        self.assertIs(item, item.bind_slack_thread("C0BR2D0MS8Y", "1234567890.123456"))
        with self.assertRaisesRegex(ValueError, "different Slack thread"):
            item.bind_slack_thread("C0BR2D0MS8Y", "1234567890.999999")

    def test_turn_state_records_start_and_finish_without_changing_work_item(self) -> None:
        item = work_item()
        turn = Turn.new(
            turn_id="turn_" + "1" * 32,
            work_item_id=item.work_item_id,
            turn_number=1,
            issue_revision="revision-1",
            prompt_sha256="b" * 64,
            input_head_sha="a" * 40,
            at="2026-01-01T00:00:01.000000Z",
        )
        self.assertTrue(turn.is_active)
        turn = turn.transition_to(TurnState.STARTING, at="2026-01-01T00:00:02.000000Z")
        turn = turn.transition_to(TurnState.RUNNING, at="2026-01-01T00:00:03.000000Z")
        turn = turn.transition_to(TurnState.NEEDS_INPUT, at="2026-01-01T00:00:04.000000Z")
        self.assertFalse(turn.is_active)
        self.assertEqual("2026-01-01T00:00:02.000000Z", turn.started_at)
        self.assertEqual("2026-01-01T00:00:04.000000Z", turn.finished_at)
        self.assertEqual(item.work_item_id, turn.work_item_id)
        with self.assertRaises(InvalidStateTransition):
            turn.transition_to(TurnState.STARTING)

    def test_rejects_rebound_branch_directory_and_noncanonical_session(self) -> None:
        item = work_item()
        with self.assertRaisesRegex(ValueError, "immutable Issue identity"):
            replace(item, task_branch="codex/issue-43-aaaaaaaaaaaa")
        with self.assertRaisesRegex(ValueError, "absolute normalized"):
            replace(item, runner_directory="../runner")
        with self.assertRaisesRegex(ValueError, "canonical UUID"):
            item.bind_session("not-a-session")


if __name__ == "__main__":
    unittest.main()
