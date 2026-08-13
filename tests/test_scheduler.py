from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from codex_dispatcher.config import Config, RepositoryConfig, SchedulerConfig, ToolPins
from codex_dispatcher.domain import Run
from codex_dispatcher.scheduler import build_dry_run_plan
from codex_dispatcher.testing.fakes import Call, FakeTracker
from codex_dispatcher.trackers.base import TaskState, TrackerTask
from tests.test_task_spec import BODY


def make_config(*, global_max_active: int = 2, repository_max_active: int = 1) -> Config:
    return Config(
        scheduler=SchedulerConfig(Path("state.db"), Path("repos"), 60, global_max_active),
        tools=ToolPins("git", "gh", "codex"),
        repositories=(
            RepositoryConfig(
                "owner/repo",
                "main",
                "env-1",
                repository_max_active,
                ("src", "tests"),
                (),
                ("alice",),
                ("tests",),
            ),
        ),
    )


def make_task(issue_number: int, *, priority: int | None = None) -> TrackerTask:
    labels = ["agent:ready", "exec:cloud"]
    if priority is not None:
        labels.append(f"priority:p{priority}")
    return TrackerTask(
        "owner/repo",
        str(issue_number),
        issue_number,
        f"Issue {issue_number}",
        BODY,
        TaskState.READY,
        tuple(labels),
        f"2026-01-{issue_number:02d}T00:00:00Z",
        "alice",
    )


class SchedulerTests(unittest.TestCase):
    def test_empty_dry_run_performs_only_tracker_reads(self) -> None:
        tracker = FakeTracker()
        plan = build_dry_run_plan(make_config(), tracker)
        self.assertEqual((), plan.selected)
        self.assertEqual((), plan.rejected)
        self.assertEqual([Call("list_ready_tasks", ("owner/repo",))], tracker.calls)

    def test_priority_is_stable_and_one_task_per_repository_is_selected(self) -> None:
        tracker = FakeTracker()
        tracker.ready_tasks = (make_task(1, priority=2), make_task(2, priority=0))
        plan = build_dry_run_plan(make_config(repository_max_active=2), tracker)
        self.assertEqual([2], [task.issue_number for task in plan.selected])
        self.assertEqual("one_per_repository_sweep", plan.rejected[0].code)

    def test_rejects_untrusted_approval_and_out_of_policy_path(self) -> None:
        tracker = FakeTracker()
        untrusted = replace(make_task(1), ready_approved_by="mallory")
        unsafe = replace(make_task(2), body=BODY.replace("- tests", "- docs"))
        tracker.ready_tasks = (untrusted, unsafe)
        plan = build_dry_run_plan(make_config(), tracker)
        self.assertEqual((), plan.selected)
        self.assertEqual(
            {"untrusted_ready_approval", "path_outside_policy"},
            {item.code for item in plan.rejected},
        )

    def test_existing_active_run_consumes_repository_capacity(self) -> None:
        tracker = FakeTracker()
        tracker.ready_tasks = (make_task(2),)
        active = Run.new(
            repository="owner/repo",
            issue_number=1,
            prompt_sha256="a" * 64,
            base_branch="main",
            cloud_environment_id="env-1",
        )
        plan = build_dry_run_plan(make_config(), tracker, (active,))
        self.assertEqual((), plan.selected)
        self.assertEqual("repository_capacity", plan.rejected[0].code)

    def test_active_issue_is_never_selected_when_repository_has_capacity(self) -> None:
        tracker = FakeTracker()
        tracker.ready_tasks = (make_task(1, priority=0), make_task(2, priority=1))
        active = Run.new(
            repository="owner/repo",
            issue_number=1,
            prompt_sha256="a" * 64,
            base_branch="main",
            cloud_environment_id="env-1",
        )
        plan = build_dry_run_plan(
            make_config(repository_max_active=2), tracker, (active,)
        )
        self.assertEqual([2], [task.issue_number for task in plan.selected])
        self.assertEqual("issue_already_active", plan.rejected[0].code)


if __name__ == "__main__":
    unittest.main()
