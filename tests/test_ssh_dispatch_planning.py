from __future__ import annotations

import unittest
from dataclasses import replace

from codex_dispatcher.prompt_builder import build_canonical_input_snapshot
from codex_dispatcher.ssh_dispatch_planning import (
    SshDispatchPlanningError,
    WorkItemAction,
    build_ssh_generation_turn_plan,
    build_ssh_turn_plan,
    resolve_ssh_work_item,
)
from codex_dispatcher.task_spec import parse_task_spec
from codex_dispatcher.trackers.base import TaskState, TrackerTask
from codex_dispatcher.work_items import (
    PromptKind,
    SessionGeneration,
    SessionGenerationRole,
    SessionGenerationState,
    WorkItem,
    WorkItemState,
)
from tests.test_scheduler import make_config
from tests.test_task_spec import BODY


BASE_SHA = "a" * 40
POLICY_DIGEST = "d" * 64
SESSION = "123e4567-e89b-12d3-a456-426614174000"


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


def active_generation(
    item: WorkItem,
    *,
    comments: tuple[object, ...],
) -> SessionGeneration:
    inputs = build_canonical_input_snapshot(
        issue_title=claimed_task().title,
        task_spec=parse_task_spec(BODY),
        comments=comments,
        maintainers=("alice",),
    )
    return (
        SessionGeneration.new(
            work_item_id=item.work_item_id,
            generation_number=1,
            role=SessionGenerationRole.IMPLEMENTATION,
            start_head_sha=BASE_SHA,
            policy_sha256=POLICY_DIGEST,
            session_generation_id="sg_" + "1" * 32,
        )
        .record_baseline(
            issue_revision="2026-08-13T01:00:00Z",
            issue_content_sha256=inputs.issue_content_sha256,
            task_spec_sha256=inputs.task_spec_sha256,
            prompt_sha256="e" * 64,
            approved_comment_ids=inputs.approved_comment_ids,
            approved_context_sha256=inputs.approved_context_sha256,
        )
        .transition_to(SessionGenerationState.STARTING)
        .bind_session(SESSION)
        .transition_to(SessionGenerationState.ACTIVE)
    )


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

    def test_generation_plan_starts_with_full_canonical_prompt(self) -> None:
        item = ready_work_item()
        generation = SessionGeneration.new(
            work_item_id=item.work_item_id,
            generation_number=1,
            role=SessionGenerationRole.IMPLEMENTATION,
            start_head_sha=BASE_SHA,
            policy_sha256=POLICY_DIGEST,
            session_generation_id="sg_" + "1" * 32,
        )
        plan = build_ssh_generation_turn_plan(
            task=claimed_task(),
            repository=self.repository,
            work_item=item,
            session_generation=generation,
            turn_number=1,
            agent_policy_digest=POLICY_DIGEST,
            comments=(
                {"id": "C1", "author": "alice", "body": "/codex-context initial"},
            ),
        )

        self.assertEqual(PromptKind.FULL, plan.prompt_kind)
        self.assertEqual(("C1",), plan.prompt.included_comment_ids)
        self.assertIn("Prompt kind: full", plan.prompt.content)

    def test_generation_resume_is_delta_and_rejects_context_drift(self) -> None:
        item = ready_work_item()
        initial = (
            {"id": "C1", "author": "alice", "body": "/codex-context initial"},
        )
        generation = active_generation(item, comments=initial)
        current = initial + (
            {"id": "C2", "author": "alice", "body": "/codex-context answer"},
        )
        plan = build_ssh_generation_turn_plan(
            task=claimed_task(),
            repository=self.repository,
            work_item=item,
            session_generation=generation,
            turn_number=2,
            agent_policy_digest=POLICY_DIGEST,
            comments=current,
            delivered_comment_ids=("C1",),
            delivered_context_sha256=generation.baseline_approved_context_sha256,
            prior_status="needs_input",
            prior_summary="Need one decision",
        )

        self.assertEqual(PromptKind.DELTA, plan.prompt_kind)
        self.assertEqual(("C2",), plan.prompt.included_comment_ids)
        self.assertIn("/codex-context answer", plan.prompt.content)
        self.assertNotIn("/codex-context initial", plan.prompt.content)

        for comments, message in (
            (initial, "requires new approved context"),
            (
                (
                    {
                        "id": "C1",
                        "author": "alice",
                        "body": "/codex-context edited",
                    },
                    current[1],
                ),
                "was edited",
            ),
            (current[1:], "was removed"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(SshDispatchPlanningError, message):
                    build_ssh_generation_turn_plan(
                        task=claimed_task(),
                        repository=self.repository,
                        work_item=item,
                        session_generation=generation,
                        turn_number=2,
                        agent_policy_digest=POLICY_DIGEST,
                        comments=comments,
                        delivered_comment_ids=("C1",),
                        delivered_context_sha256=(
                            generation.baseline_approved_context_sha256
                        ),
                        prior_status="needs_input",
                        prior_summary="Need one decision",
                    )

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
