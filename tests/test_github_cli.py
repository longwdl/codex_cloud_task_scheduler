from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.command_runner import CommandResult
from codex_dispatcher.trackers.base import TaskState
from codex_dispatcher.trackers.github_cli import (
    GitHubCliReadOnlyError,
    GitHubCliTracker,
    GitHubCliTrackerError,
    GitHubCliUnsupportedReadError,
)


GH = "/opt/homebrew/bin/gh"
REPOSITORY = "owner/repo"


def result(payload: object) -> CommandResult:
    return CommandResult(0, json.dumps(payload), "")


def issue(*, number: int = 12, labels: object | None = None) -> dict[str, object]:
    return {
        "number": number,
        "title": "Safe task",
        "body": "Task body",
        "labels": labels if labels is not None else [label("agent:ready"), label("exec:cloud")],
        "createdAt": "2026-08-13T00:00:00Z",
        "state": "OPEN",
    }


def label(name: str) -> dict[str, object]:
    return {"id": f"id-{name}", "name": name, "description": None, "color": "abcdef"}


class GitHubCliTrackerTests(unittest.TestCase):
    def test_list_ready_tasks_reads_fixed_argv_and_audits_latest_label_actor(self) -> None:
        calls: list[tuple[str, ...]] = []
        keyword_arguments: list[dict[str, object]] = []

        def fake_run(argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            calls.append(argv)
            keyword_arguments.append(kwargs)
            if argv[1:3] == ("issue", "list"):
                return result([issue()])
            return result(
                [[
                    {
                        "event": "labeled",
                        "created_at": "2026-08-13T01:00:00Z",
                        "label": {"name": "agent:ready"},
                        "actor": {"login": "alice"},
                    },
                    {
                        "event": "labeled",
                        "created_at": "2026-08-13T02:00:00Z",
                        "label": {"name": "agent:ready"},
                        "actor": {"login": "bob"},
                    },
                ]]
            )

        with patch("codex_dispatcher.trackers.github_cli.run_command", side_effect=fake_run):
            tasks = GitHubCliTracker(gh_path=GH, token="test-token").list_ready_tasks(REPOSITORY)

        self.assertEqual(1, len(tasks))
        self.assertEqual(TaskState.READY, tasks[0].state)
        self.assertEqual("bob", tasks[0].ready_approved_by)
        self.assertEqual((GH, "issue", "list"), calls[0][:3])
        self.assertIn("--repo", calls[0])
        self.assertIn(REPOSITORY, calls[0])
        self.assertEqual("api", calls[1][1])
        self.assertEqual("GET", calls[1][3])
        self.assertIn("/repos/owner/repo/issues/12/timeline?per_page=100", calls[1])
        self.assertEqual(
            {"GH_PROMPT_DISABLED": "1", "GH_TOKEN": "test-token"},
            keyword_arguments[0]["env"],
        )
        self.assertEqual(("test-token",), keyword_arguments[0]["secrets"])

    def test_missing_or_malformed_audit_data_is_untrusted_not_inferred(self) -> None:
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[
                result([issue()]),
                result([[{
                    "event": "labeled",
                    "created_at": "2026-08-13T02:00:00Z",
                    "label": {"name": "agent:ready"},
                }]]),
            ],
        ):
            tasks = GitHubCliTracker(gh_path=GH).list_ready_tasks(REPOSITORY)
        self.assertIsNone(tasks[0].ready_approved_by)

    def test_more_than_one_agent_state_fails_closed(self) -> None:
        conflicting = issue(
            labels=[
                label("agent:ready"),
                label("agent:blocked"),
                label("exec:cloud"),
            ]
        )
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=result([conflicting]),
        ) as runner:
            with self.assertRaisesRegex(GitHubCliTrackerError, "agent state"):
                GitHubCliTracker(gh_path=GH).list_ready_tasks(REPOSITORY)
        runner.assert_called_once()

    def test_malformed_issue_data_fails_closed(self) -> None:
        malformed = issue(labels=[{"name": "agent:ready", "color": "red"}])
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=result([malformed]),
        ):
            with self.assertRaisesRegex(GitHubCliTrackerError, "label"):
                GitHubCliTracker(gh_path=GH).list_ready_tasks(REPOSITORY)

    def test_get_task_and_json_command_failure(self) -> None:
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[result(issue()), result([])],
        ) as runner:
            task = GitHubCliTracker(gh_path=GH).get_task(REPOSITORY, "12")
        self.assertIsNotNone(task)
        self.assertEqual("12", task.task_id)
        self.assertEqual("view", runner.call_args_list[0].args[0][2])

        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=CommandResult(1, "", "denied"),
        ):
            with self.assertRaisesRegex(GitHubCliTrackerError, "exit code 1"):
                GitHubCliTracker(gh_path=GH).list_ready_tasks(REPOSITORY)

    def test_command_failure_does_not_include_token_or_provider_stderr(self) -> None:
        token = "github_pat_test_fixture"
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=CommandResult(1, "", f"denied for {token}"),
        ):
            with self.assertRaises(GitHubCliTrackerError) as caught:
                GitHubCliTracker(gh_path=GH, token=token).list_ready_tasks(REPOSITORY)
        self.assertNotIn(token, str(caught.exception))
        self.assertNotIn("denied", str(caught.exception))

    def test_path_repository_and_issue_id_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            GitHubCliTracker(gh_path="gh")
        with self.assertRaises(ValueError):
            GitHubCliTracker(gh_path=GH, token="")
        tracker = GitHubCliTracker(gh_path=Path(GH))
        for repository in ("owner/repo/extra", "owner/repo?per_page=1", "owner/repo name"):
            with self.assertRaises(ValueError):
                tracker.list_ready_tasks(repository)
        with self.assertRaises(ValueError):
            tracker.get_task(REPOSITORY, "012")

    def test_writes_raise_before_running_a_command_and_optional_read_is_clear(self) -> None:
        tracker = GitHubCliTracker(gh_path=GH)
        with patch("codex_dispatcher.trackers.github_cli.run_command") as runner:
            for call in (
                lambda: tracker.claim(REPOSITORY, "12", "worker"),
                lambda: tracker.set_state(REPOSITORY, "12", TaskState.BLOCKED),
                lambda: tracker.upsert_run_comment(REPOSITORY, "12", "marker", "body"),
            ):
                with self.assertRaises(GitHubCliReadOnlyError):
                    call()
        runner.assert_not_called()
        with self.assertRaises(GitHubCliUnsupportedReadError):
            tracker.find_pr_by_branch(REPOSITORY, "branch")


if __name__ == "__main__":
    unittest.main()
