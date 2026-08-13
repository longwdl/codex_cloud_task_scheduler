from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.command_runner import CommandResult
from codex_dispatcher.config import ToolPins
from codex_dispatcher.contract import run_contract_checks
from codex_dispatcher.executors.base import PreflightResult


class ContractTests(unittest.TestCase):
    def test_versions_and_environment_are_read_only_and_exact(self) -> None:
        outputs = {
            "/usr/bin/git": "git version 2.55.0\n",
            "/usr/bin/gh": "gh version 2.97.0 (date)\n",
            "/usr/bin/codex": "codex-cli 0.147.0\n",
        }

        def fake_run(argv: tuple[str, ...], **_: object) -> CommandResult:
            return CommandResult(0, outputs[argv[0]], "")

        with (
            patch("codex_dispatcher.command_runner.run_command", side_effect=fake_run),
            patch(
                "codex_dispatcher.contract.CodexCloudCliExecutor.preflight",
                return_value=PreflightResult(True, "visible"),
            ) as preflight,
        ):
            checks = run_contract_checks(
                pins=ToolPins("2.55.0", "2.97.0", "0.147.0"),
                git_path=Path("/usr/bin/git"),
                gh_path=Path("/usr/bin/gh"),
                codex_path=Path("/usr/bin/codex"),
                cloud_environment_ids=("env-1",),
            )

        self.assertTrue(all(check.ok for check in checks))
        preflight.assert_called_once_with("env-1")

    def test_version_drift_and_cloud_schema_are_failures(self) -> None:
        with (
            patch(
                "codex_dispatcher.command_runner.run_command",
                return_value=CommandResult(0, "tool 9.9.9\n", ""),
            ),
            patch(
                "codex_dispatcher.contract.CodexCloudCliExecutor.preflight",
                return_value=PreflightResult(False, "schema drift"),
            ) as preflight,
        ):
            checks = run_contract_checks(
                pins=ToolPins("1", "2", "3"),
                git_path=Path("/usr/bin/git"),
                gh_path=Path("/usr/bin/gh"),
                codex_path=Path("/usr/bin/codex"),
                cloud_environment_ids=("env-1",),
            )
        self.assertFalse(any(check.ok for check in checks))
        preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
