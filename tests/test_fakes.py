from __future__ import annotations

import unittest
from pathlib import Path

from codex_dispatcher.executors.base import RemoteRun, RunStatus, SubmissionRequest
from codex_dispatcher.testing.fakes import Call, FakeExecutor, FakeTracker
from codex_dispatcher.trackers.base import ClaimResult, TaskState, TrackerTask


class FakeTrackerTests(unittest.TestCase):
    def test_default_reads_are_empty_and_do_not_call_writes(self) -> None:
        tracker = FakeTracker()

        self.assertEqual(tracker.list_ready_tasks("owner/repo"), ())
        self.assertIsNone(tracker.get_task("owner/repo", "42"))
        self.assertIsNone(tracker.find_pr_by_branch("owner/repo", "codex/42"))
        self.assertEqual(
            tracker.calls,
            [
                Call("list_ready_tasks", ("owner/repo",)),
                Call("get_task", ("owner/repo", "42")),
                Call("find_pr_by_branch", ("owner/repo", "codex/42")),
            ],
        )

    def test_configured_claim_is_idempotent_and_records_sequence(self) -> None:
        tracker = FakeTracker()
        task = TrackerTask(
            "owner/repo",
            "42",
            42,
            "Title",
            "Body",
            TaskState.READY,
            ("agent:ready", "exec:cloud"),
            "2026-01-01T00:00:00Z",
            "alice",
        )
        result = ClaimResult(True, task)
        tracker.set_result("claim", result)

        self.assertIs(tracker.claim("owner/repo", "42", "worker"), result)
        self.assertIs(tracker.claim("owner/repo", "42", "worker"), result)
        self.assertEqual(
            tracker.calls,
            [
                Call("claim", ("owner/repo", "42", "worker")),
                Call("claim", ("owner/repo", "42", "worker")),
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


class FakeExecutorTests(unittest.TestCase):
    def test_default_executor_is_fail_closed_for_unknown_submit_status(self) -> None:
        executor = FakeExecutor()
        request = SubmissionRequest("local-42", "env-1", "prompt", "codex/42", "a" * 40)
        result = executor.submit(request)

        self.assertEqual(result.status, RunStatus.UNKNOWN)
        self.assertFalse(result.status.is_success)
        self.assertEqual(executor.calls, [Call("submit", (request,))])

    def test_configured_results_and_exception(self) -> None:
        executor = FakeExecutor()
        run = RemoteRun("cloud-1", "local-42", "env-1", RunStatus.SUCCEEDED, "codex/42")
        executor.set_result("reconcile", run)
        executor.set_exception("apply", ConnectionError("unavailable"))

        self.assertIs(executor.reconcile("cloud-1"), run)
        with self.assertRaisesRegex(ConnectionError, "unavailable"):
            executor.apply("cloud-1", Path("/tmp/worktree"))
        self.assertEqual(
            executor.calls,
            [
                Call("reconcile", ("cloud-1",)),
                Call("apply", ("cloud-1", Path("/tmp/worktree"))),
            ],
        )

    def test_unknown_status_never_reports_success(self) -> None:
        self.assertFalse(RunStatus.UNKNOWN.is_success)
        self.assertTrue(RunStatus.SUCCEEDED.is_success)


if __name__ == "__main__":
    unittest.main()
