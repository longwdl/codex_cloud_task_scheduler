from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codex_dispatcher.github_delivery import (
    GitHubDeliveryCoordinator,
    GitHubDeliveryRejected,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.testing.fakes import FakeTracker
from codex_dispatcher.trackers.base import PullRequest, PullRequestState, TaskState
from codex_dispatcher.work_items import WorkItem, WorkItemState
from tests.test_ssh_dispatch_planning import BASE_SHA, claimed_task


HEAD_SHA = "b" * 40


class _LostReceiptTracker(FakeTracker):
    def __init__(self, *, fail_comment_once: bool = False) -> None:
        super().__init__()
        self.fail_create_once = True
        self.fail_comment_once = fail_comment_once

    def create_draft_pr(self, request):
        created = super().create_draft_pr(request)
        if self.fail_create_once:
            self.fail_create_once = False
            raise RuntimeError("fixture lost create receipt")
        return created

    def upsert_run_comment(self, repository, task_id, marker, body):
        super().upsert_run_comment(repository, task_id, marker, body)
        if self.fail_comment_once:
            self.fail_comment_once = False
            raise RuntimeError("fixture lost comment receipt")


class GitHubDeliveryCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "state.db")
        self.store.migrate()
        item = WorkItem.new(
            repository="owner/repo",
            issue_number=42,
            issue_node_id="I_kwDOFixture42",
            base_branch="main",
            base_sha=BASE_SHA,
            at="2026-08-13T00:00:00Z",
        )
        self.store.create_work_item(item)
        self.store.update_work_item_state(item.work_item_id, WorkItemState.PREPARING)
        self.store.update_work_item_state(item.work_item_id, WorkItemState.READY)
        self.store.update_work_item_state(item.work_item_id, WorkItemState.RUNNING)
        self.store.record_published_sha(
            item.work_item_id,
            previous_sha=BASE_SHA,
            head_sha=HEAD_SHA,
        )
        self.item = self.store.update_work_item_state(
            item.work_item_id,
            WorkItemState.REVIEW,
        )
        self.task = replace(
            claimed_task(),
            state=TaskState.RUNNING,
            labels=("agent:running", "exec:ssh-cli", "priority:p1"),
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_creates_reads_back_binds_and_reuses_one_draft_pr(self) -> None:
        tracker = FakeTracker()
        coordinator = GitHubDeliveryCoordinator(store=self.store, tracker=tracker)

        first = coordinator.reconcile(
            self.task,
            work_item=self.item,
            desired_task_state=TaskState.REVIEW,
        )
        assert first.pull_request is not None
        tracker.pull_requests[(self.item.repository, self.item.task_branch)] = replace(
            first.pull_request,
            is_draft=False,
        )
        second = coordinator.reconcile(
            self.task,
            work_item=first.work_item,
            desired_task_state=TaskState.REVIEW,
        )

        self.assertEqual(1, first.work_item.pr_number)
        self.assertEqual(1, second.work_item.pr_number)
        self.assertFalse(second.pull_request.is_draft)
        methods = [call.method for call in tracker.calls]
        self.assertEqual(1, methods.count("create_draft_pr"))
        self.assertEqual(3, methods.count("find_pr_by_branch"))
        self.assertEqual(2, methods.count("upsert_run_comment"))
        comment = next(
            call for call in tracker.calls if call.method == "upsert_run_comment"
        )
        self.assertIn(HEAD_SHA, comment.args[-1])

    def test_lost_create_receipt_is_recovered_by_branch_without_second_pr(self) -> None:
        tracker = _LostReceiptTracker()
        coordinator = GitHubDeliveryCoordinator(store=self.store, tracker=tracker)

        with self.assertRaisesRegex(RuntimeError, "lost create receipt"):
            coordinator.reconcile(
                self.task,
                work_item=self.item,
                desired_task_state=TaskState.REVIEW,
            )
        interrupted = self.store.get_work_item(self.item.work_item_id)
        assert interrupted is not None
        self.assertIsNone(interrupted.pr_number)

        recovered = coordinator.reconcile(
            self.task,
            work_item=interrupted,
            desired_task_state=TaskState.REVIEW,
        )

        self.assertEqual(1, recovered.work_item.pr_number)
        self.assertEqual(
            1,
            len([call for call in tracker.calls if call.method == "create_draft_pr"]),
        )

    def test_comment_failure_after_binding_reuses_the_bound_pr(self) -> None:
        tracker = _LostReceiptTracker(fail_comment_once=True)
        tracker.fail_create_once = False
        coordinator = GitHubDeliveryCoordinator(store=self.store, tracker=tracker)

        with self.assertRaisesRegex(RuntimeError, "lost comment receipt"):
            coordinator.reconcile(
                self.task,
                work_item=self.item,
                desired_task_state=TaskState.REVIEW,
            )
        bound = self.store.get_work_item(self.item.work_item_id)
        assert bound is not None
        self.assertEqual(1, bound.pr_number)

        coordinator.reconcile(
            self.task,
            work_item=bound,
            desired_task_state=TaskState.REVIEW,
        )
        self.assertEqual(
            1,
            len([call for call in tracker.calls if call.method == "create_draft_pr"]),
        )

    def test_running_execution_link_is_fixed_and_does_not_touch_pull_requests(self) -> None:
        tracker = FakeTracker()
        coordinator = GitHubDeliveryCoordinator(store=self.store, tracker=tracker)
        bound = self.store.bind_slack_thread(
            self.item.work_item_id,
            channel_id="C0BR2D0MS8Y",
            thread_ts="1700000000.000001",
        )
        permalink = (
            "https://fixture.slack.com/archives/C0BR2D0MS8Y/"
            "p1700000000000001"
        )

        coordinator.reconcile_execution_link(
            self.task,
            work_item=bound,
            slack_permalink=permalink,
        )

        self.assertEqual(["upsert_run_comment"], [call.method for call in tracker.calls])
        self.assertIn(permalink, tracker.calls[0].args[-1])

        tracker.calls.clear()
        with self.assertRaisesRegex(GitHubDeliveryRejected, "permalink"):
            coordinator.reconcile_execution_link(
                self.task,
                work_item=bound,
                slack_permalink="https://attacker.example/thread",
            )
        with self.assertRaisesRegex(GitHubDeliveryRejected, "permalink"):
            coordinator.reconcile_execution_link(
                self.task,
                work_item=bound,
                slack_permalink=(
                    "https://fixture.slack.com/archives/C0BR2D0MS8Y/"
                    "p1700000000000002"
                ),
            )
        self.assertEqual([], tracker.calls)

    def test_closed_or_wrong_branch_pr_is_rejected_without_rebinding(self) -> None:
        existing = PullRequest(
            number=9,
            url="https://github.com/owner/repo/pull/9",
            branch_name=self.item.task_branch,
            title="Existing",
            is_draft=True,
            base_branch=self.item.base_branch,
            state=PullRequestState.OPEN,
        )
        for unsafe in (
            replace(existing, state=PullRequestState.CLOSED),
            replace(existing, branch_name=f"{self.item.task_branch}-other"),
        ):
            with self.subTest(state=unsafe.state, branch=unsafe.branch_name):
                tracker = FakeTracker()
                tracker.pull_requests[
                    (self.item.repository, self.item.task_branch)
                ] = unsafe
                coordinator = GitHubDeliveryCoordinator(
                    store=self.store,
                    tracker=tracker,
                )

                with self.assertRaisesRegex(
                    GitHubDeliveryRejected,
                    "publication binding",
                ):
                    coordinator.reconcile(
                        self.task,
                        work_item=self.item,
                        desired_task_state=TaskState.REVIEW,
                    )

                persisted = self.store.get_work_item(self.item.work_item_id)
                assert persisted is not None
                self.assertIsNone(persisted.pr_number)
                self.assertFalse(
                    any(
                        call.method == "create_draft_pr"
                        for call in tracker.calls
                    )
                )


if __name__ == "__main__":
    unittest.main()
