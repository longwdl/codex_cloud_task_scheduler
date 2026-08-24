from __future__ import annotations

import unittest

from codex_dispatcher.testing.fakes import Call, FakeTracker
from codex_dispatcher.trackers.base import (
    ClaimResult,
    TaskState,
    TrackerComment,
    TrackerTask,
)


class FakeTrackerTests(unittest.TestCase):
    def test_default_reads_are_empty_and_do_not_call_writes(self) -> None:
        tracker = FakeTracker()

        self.assertEqual(tracker.list_ready_tasks("owner/repo"), ())
        self.assertEqual(
            tracker.list_open_tasks("owner/repo", TaskState.DISPATCHING), ()
        )
        self.assertIsNone(tracker.get_task("owner/repo", "42"))
        self.assertEqual(tracker.list_comments("owner/repo", "42"), ())
        self.assertIsNone(tracker.find_pr_by_branch("owner/repo", "codex/42"))
        self.assertEqual(
            tracker.calls,
            [
                Call("list_ready_tasks", ("owner/repo",)),
                Call("list_open_tasks", ("owner/repo", TaskState.DISPATCHING)),
                Call("get_task", ("owner/repo", "42")),
                Call("list_comments", ("owner/repo", "42")),
                Call("find_pr_by_branch", ("owner/repo", "codex/42")),
            ],
        )

    def test_comment_snapshots_are_returned_only_for_the_requested_repository(self) -> None:
        tracker = FakeTracker()
        task = TrackerTask(
            "owner/repo", "42", 42, "Title", "Body", TaskState.READY,
            ("agent:ready", "exec:ssh-cli"), "2026-01-01T00:00:00Z", "alice",
        )
        comment = TrackerComment(
            "IC_fixture", "alice", "/codex-context\nUse fixture",
            "2026-01-01T01:00:00Z", "2026-01-01T01:00:00Z",
        )
        tracker.tasks["42"] = task
        tracker.comments["42"] = (comment,)

        self.assertEqual((comment,), tracker.list_comments("owner/repo", "42"))
        self.assertEqual((), tracker.list_comments("other/repo", "42"))

    def test_configured_claim_is_idempotent_and_records_sequence(self) -> None:
        tracker = FakeTracker()
        task = TrackerTask(
            "owner/repo",
            "42",
            42,
            "Title",
            "Body",
            TaskState.READY,
            ("agent:ready", "exec:ssh-cli"),
            "2026-01-01T00:00:00Z",
            "alice",
        )
        result = ClaimResult(True, task)
        tracker.set_result("claim", result)

        self.assertIs(
            tracker.claim("owner/repo", "42", "worker", approved_by=("alice",)), result
        )
        self.assertIs(
            tracker.claim("owner/repo", "42", "worker", approved_by=("alice",)), result
        )
        self.assertEqual(
            tracker.calls,
            [
                Call("claim", ("owner/repo", "42", "worker", ("alice",))),
                Call("claim", ("owner/repo", "42", "worker", ("alice",))),
            ],
        )

    def test_configured_exception_is_recorded_then_raised(self) -> None:
        tracker = FakeTracker()
        tracker.set_exception("upsert_run_comment", RuntimeError("offline"))

        with self.assertRaisesRegex(RuntimeError, "offline"):
            tracker.upsert_run_comment("owner/repo", "42", "run:started", "run started")
        self.assertEqual(
            tracker.calls,
            [Call("upsert_run_comment", ("owner/repo", "42", "run:started", "run started"))],
        )
if __name__ == "__main__":
    unittest.main()
