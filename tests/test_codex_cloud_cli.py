from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.command_runner import CommandResult
from codex_dispatcher.executors.base import RunStatus, SubmissionRequest
from codex_dispatcher.executors.codex_cloud_cli import (
    CodexCloudCliError,
    CodexCloudCliExecutor,
    CodexCloudCliUnsupportedReadError,
    CodexCloudCliWriteDisabledError,
)


CODEX = "/opt/homebrew/bin/codex"
ENVIRONMENT = "6a7d9f7b8784819183c5c2e3691466e7"


def result(payload: object) -> CommandResult:
    return CommandResult(0, json.dumps(payload), "")


class CodexCloudCliExecutorTests(unittest.TestCase):
    def test_empty_environment_preflight_uses_fixed_json_argv(self) -> None:
        with patch(
            "codex_dispatcher.executors.codex_cloud_cli.run_command",
            return_value=result({"tasks": [], "cursor": None}),
        ) as runner:
            preflight = CodexCloudCliExecutor(codex_path=CODEX).preflight(ENVIRONMENT)

        self.assertTrue(preflight.ok)
        self.assertEqual(
            (
                CODEX,
                "cloud",
                "list",
                "--env",
                ENVIRONMENT,
                "--limit",
                "1",
                "--json",
            ),
            runner.call_args.args[0],
        )

    def test_nonempty_task_schema_is_strict_and_unknown_status_is_safe(self) -> None:
        task = {
            "id": "task-1",
            "url": "https://chatgpt.com/codex/tasks/task-1",
            "title": "fixture",
            "status": "new-provider-state",
            "updated_at": "2026-08-13T00:00:00Z",
            "environment_id": ENVIRONMENT,
            "environment_label": "fixture",
            "summary": None,
            "is_review": False,
            "attempt_total": 1,
        }
        payload = {"tasks": [task], "cursor": None}
        with patch(
            "codex_dispatcher.executors.codex_cloud_cli.run_command",
            return_value=result(payload),
        ):
            preflight = CodexCloudCliExecutor(codex_path=CODEX).preflight(ENVIRONMENT)
            self.assertTrue(preflight.ok)
        with patch(
            "codex_dispatcher.executors.codex_cloud_cli.run_command",
            return_value=result(payload),
        ):
            runs = CodexCloudCliExecutor(codex_path=CODEX).list_runs(ENVIRONMENT)
        self.assertEqual(1, len(runs))
        self.assertEqual(RunStatus.UNKNOWN, runs[0].status)
        self.assertFalse(runs[0].status.is_success)

        malformed = {"tasks": [{**task, "new_field": True}], "cursor": None}
        with patch(
            "codex_dispatcher.executors.codex_cloud_cli.run_command",
            return_value=result(malformed),
        ):
            with self.assertRaisesRegex(CodexCloudCliError, "unexpected JSON fields"):
                CodexCloudCliExecutor(codex_path=CODEX).list_runs(ENVIRONMENT)

    def test_schema_and_command_failures_are_generic(self) -> None:
        with patch(
            "codex_dispatcher.executors.codex_cloud_cli.run_command",
            return_value=result({"tasks": [], "cursor": None, "new": True}),
        ):
            with self.assertRaisesRegex(CodexCloudCliError, "unexpected"):
                CodexCloudCliExecutor(codex_path=CODEX).preflight(ENVIRONMENT)
        with patch(
            "codex_dispatcher.executors.codex_cloud_cli.run_command",
            return_value=CommandResult(1, "", "private provider detail"),
        ):
            with self.assertRaises(CodexCloudCliError) as caught:
                CodexCloudCliExecutor(codex_path=CODEX).preflight(ENVIRONMENT)
        self.assertNotIn("private provider detail", str(caught.exception))

    def test_writes_raise_before_running_any_command(self) -> None:
        executor = CodexCloudCliExecutor(codex_path=Path(CODEX))
        request = SubmissionRequest("run-1", ENVIRONMENT, "secret prompt", "agent/1", "a" * 40)
        with patch("codex_dispatcher.executors.codex_cloud_cli.run_command") as runner:
            with self.assertRaises(CodexCloudCliWriteDisabledError):
                executor.submit(request)
            with self.assertRaises(CodexCloudCliWriteDisabledError):
                executor.apply("task-1", Path("/tmp/worktree"))
            with self.assertRaises(CodexCloudCliUnsupportedReadError):
                executor.reconcile("task-1")
            with self.assertRaises(CodexCloudCliUnsupportedReadError):
                executor.fetch_diff("task-1")
        runner.assert_not_called()

    def test_path_and_identifiers_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            CodexCloudCliExecutor(codex_path="codex")
        executor = CodexCloudCliExecutor(codex_path=CODEX)
        with self.assertRaises(ValueError):
            executor.preflight("env/unsafe")


if __name__ == "__main__":
    unittest.main()
