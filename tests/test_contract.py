from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.command_runner import CommandResult
from codex_dispatcher.config import ToolPins
from codex_dispatcher.contract import run_control_host_contract_checks


class ContractTests(unittest.TestCase):
    def test_control_host_contract_checks_only_runtime_tools(self) -> None:
        outputs = {
            "/usr/bin/git": "git version 2.55.0\n",
            "/usr/bin/gh": "gh version 2.97.0 (date)\n",
            "/usr/bin/ssh": "OpenSSH_9.6p1 Linux fixture\n",
        }

        gh_config_directories: list[str] = []

        def fake_run(argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            if argv[0] == "/usr/bin/gh":
                environment = kwargs["env"]
                assert isinstance(environment, dict)
                config_directory = environment["GH_CONFIG_DIR"]
                assert isinstance(config_directory, str)
                self.assertTrue(Path(config_directory).is_dir())
                self.assertEqual(0o700, os.stat(config_directory).st_mode & 0o777)
                gh_config_directories.append(config_directory)
            output = outputs[argv[0]]
            return (
                CommandResult(0, "", output)
                if argv[0] == "/usr/bin/ssh"
                else CommandResult(0, output, "")
            )

        with patch("codex_dispatcher.command_runner.run_command", side_effect=fake_run):
            checks = run_control_host_contract_checks(
                pins=ToolPins("2.55.0", "2.97.0", "0.147.0", "9.6"),
                git_path=Path("/usr/bin/git"),
                gh_path=Path("/usr/bin/gh"),
                ssh_path=Path("/usr/bin/ssh"),
            )

        self.assertTrue(all(check.ok for check in checks))
        self.assertEqual(["git", "gh", "ssh"], [check.name for check in checks])
        self.assertEqual(1, len(gh_config_directories))
        self.assertFalse(Path(gh_config_directories[0]).exists())


if __name__ == "__main__":
    unittest.main()
