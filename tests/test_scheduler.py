from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from codex_dispatcher.config import (
    Config,
    RepositoryAdmissionConfig,
    RepositoryConfig,
    SchedulerConfig,
    ToolPins,
)
from codex_dispatcher.repository_admission import (
    HIGHER_VALUE_CANARY_REPOSITORY,
    HigherValueCanaryTarget,
    RepositoryClass,
    RepositoryRecoveryProfile,
    RepositoryTargetReadbackProfile,
)
from codex_dispatcher.scheduler import (
    build_ssh_dry_run_plan,
    build_ssh_higher_value_canary_plan,
)
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
                repository_max_active,
                ("src", "tests"),
                (),
                ("alice",),
                ("tests",),
                RepositoryClass.FIXTURE,
            ),
        ),
        repository_admission=RepositoryAdmissionConfig(
            frozenset({RepositoryRecoveryProfile.FIXTURE_LIVE_V1}),
            frozenset({RepositoryTargetReadbackProfile.FIXTURE_EXACT_V1}),
        ),
    )


def make_task(
    issue_number: int,
    *,
    priority: int | None = None,
    executor_label: str = "exec:ssh-cli",
) -> TrackerTask:
    labels = ["agent:ready", executor_label]
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
    def test_empty_ssh_plan_performs_only_tracker_reads(self) -> None:
        tracker = FakeTracker()
        plan = build_ssh_dry_run_plan(make_config(), tracker)
        self.assertEqual((), plan.selected)
        self.assertEqual((), plan.rejected)
        self.assertEqual([Call("list_ready_tasks", ("owner/repo",))], tracker.calls)

    def test_repository_admission_is_explicit_and_fail_closed(self) -> None:
        tracker = FakeTracker()
        tracker.ready_tasks = (make_task(1, executor_label="exec:ssh-cli"),)
        base = make_config()

        unclassified = replace(
            base,
            repositories=(
                replace(
                    base.repositories[0],
                    repository_class=RepositoryClass.UNCLASSIFIED,
                ),
            ),
        )
        mismatched = replace(
            base,
            repository_admission=RepositoryAdmissionConfig(
                frozenset({RepositoryRecoveryProfile.HIGHER_VALUE_LIVE_V1}),
                frozenset({RepositoryTargetReadbackProfile.FIXTURE_EXACT_V1}),
            ),
        )
        higher_value = replace(
            base,
            repositories=(
                replace(
                    base.repositories[0],
                    repository_class=RepositoryClass.HIGHER_VALUE,
                ),
            ),
            repository_admission=RepositoryAdmissionConfig(
                frozenset({RepositoryRecoveryProfile.HIGHER_VALUE_LIVE_V1}),
                frozenset({RepositoryTargetReadbackProfile.HIGHER_VALUE_EXACT_V1}),
            ),
        )

        decisions = (
            build_ssh_dry_run_plan(unclassified, tracker),
            build_ssh_dry_run_plan(mismatched, tracker),
            build_ssh_dry_run_plan(higher_value, tracker),
        )

        self.assertEqual(
            [
                "repository_class_unclassified",
                "repository_recovery_profile_mismatch",
                "repository_class_not_admitted",
            ],
            [decision.rejected[0].code for decision in decisions],
        )

    def test_priority_is_stable_and_one_task_per_repository_is_selected(self) -> None:
        tracker = FakeTracker()
        tracker.ready_tasks = (make_task(1, priority=2), make_task(2, priority=0))
        plan = build_ssh_dry_run_plan(make_config(repository_max_active=2), tracker)
        self.assertEqual([2], [task.issue_number for task in plan.selected])
        self.assertEqual("global_capacity", plan.rejected[0].code)

    def test_rejects_untrusted_approval_and_out_of_policy_path(self) -> None:
        tracker = FakeTracker()
        untrusted = replace(make_task(1), ready_approved_by="mallory")
        unsafe = replace(make_task(2), body=BODY.replace("- tests", "- docs"))
        tracker.ready_tasks = (untrusted, unsafe)
        plan = build_ssh_dry_run_plan(make_config(), tracker)
        self.assertEqual((), plan.selected)
        self.assertEqual(
            {"untrusted_ready_approval", "path_outside_policy"},
            {item.code for item in plan.rejected},
        )

    def test_ssh_plan_accepts_only_ssh_label_and_forces_one_global_candidate(self) -> None:
        tracker = FakeTracker()
        tracker.ready_tasks = (
            make_task(1, priority=0, executor_label="exec:other"),
            make_task(2, priority=1, executor_label="exec:ssh-cli"),
            make_task(3, priority=2, executor_label="exec:ssh-cli"),
        )

        plan = build_ssh_dry_run_plan(make_config(global_max_active=3), tracker)

        self.assertEqual([2], [task.issue_number for task in plan.selected])
        self.assertEqual(
            {1: "invalid_executor_labels", 3: "global_capacity"},
            {item.issue_number: item.code for item in plan.rejected},
        )

    def test_ssh_plan_selects_nothing_while_any_turn_is_active(self) -> None:
        tracker = FakeTracker()
        tracker.ready_tasks = (
            make_task(1, executor_label="exec:ssh-cli"),
            make_task(2, executor_label="exec:ssh-cli"),
        )

        plan = build_ssh_dry_run_plan(
            make_config(global_max_active=3), tracker, active_turn_exists=True
        )

        self.assertEqual((), plan.selected)
        self.assertEqual(
            ["global_capacity", "global_capacity"],
            [item.code for item in plan.rejected],
        )

    def test_manual_canary_planner_cannot_widen_normal_higher_value_admission(self) -> None:
        base = make_config(global_max_active=1)
        config = replace(
            base,
            repositories=(
                replace(
                    base.repositories[0],
                    slug=HIGHER_VALUE_CANARY_REPOSITORY,
                    allowed_paths=("canary/target.txt",),
                    repository_class=RepositoryClass.HIGHER_VALUE,
                ),
            ),
            repository_admission=RepositoryAdmissionConfig(
                frozenset({RepositoryRecoveryProfile.HIGHER_VALUE_LIVE_V1}),
                frozenset({RepositoryTargetReadbackProfile.HIGHER_VALUE_EXACT_V1}),
            ),
        )
        task = replace(
            make_task(7, executor_label="exec:ssh-cli"),
            repository=HIGHER_VALUE_CANARY_REPOSITORY,
            body=BODY.replace(
                "- src/codex_dispatcher\n- tests", "- canary/target.txt"
            ),
            issue_node_id="I_kwDOHigherValue7",
        )
        target = HigherValueCanaryTarget(
            task.repository,
            task.issue_number,
            task.issue_node_id or "missing",
            "a" * 40,
        )
        tracker = FakeTracker()
        tracker.ready_tasks = (task, replace(task, issue_number=8, task_id="8"))

        ordinary = build_ssh_dry_run_plan(config, tracker)
        canary = build_ssh_higher_value_canary_plan(
            config,
            tracker,
            target=target,
        )

        self.assertEqual((), ordinary.selected)
        self.assertTrue(
            all(item.code == "repository_class_not_admitted" for item in ordinary.rejected)
        )
        self.assertEqual((task,), canary.selected)
        self.assertEqual("higher_value_canary_target_mismatch", canary.rejected[0].code)


if __name__ == "__main__":
    unittest.main()
