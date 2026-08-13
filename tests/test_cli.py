from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_dispatcher.cli import main, run_once_dry_run
from codex_dispatcher.contract import ContractCheck
from codex_dispatcher.domain import Run
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.testing.fakes import FakeTracker
from tests.test_scheduler import make_config


class CliTests(unittest.TestCase):
    def test_doctor_json_is_read_only_and_machine_readable(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(["doctor", "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("offline-core", payload["phase"])
        self.assertIn("tool:gh", {item["name"] for item in payload["checks"]})

    def test_doctor_contract_requires_config_and_reports_checks(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(["doctor", "--contract", "--json"])
        self.assertEqual(1, exit_code)
        self.assertFalse(json.loads(stdout.getvalue())["ok"])

        stdout = io.StringIO()
        with (
            patch("codex_dispatcher.cli.load_config", return_value=make_config()),
            patch(
                "codex_dispatcher.contract.run_contract_checks",
                return_value=(ContractCheck("git", True, "2.55.0"),),
            ),
            patch(
                "codex_dispatcher.cli.shutil.which",
                side_effect=lambda tool: f"/usr/bin/{tool}",
            ),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                ["doctor", "--config", "config.toml", "--contract", "--json"]
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertIn("contract:git", {item["name"] for item in payload["checks"]})

    def test_status_rejects_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "missing.db"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(["status", "--database", str(database), "--json"])

        self.assertEqual(1, exit_code)
        self.assertFalse(json.loads(stdout.getvalue())["ok"])

    def test_injected_dry_run_does_not_require_external_adapters(self) -> None:
        tracker = FakeTracker()
        plan = run_once_dry_run(make_config(), tracker)
        self.assertEqual((), plan.selected)
        self.assertEqual(1, len(tracker.calls))

    def test_status_reads_an_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "state.db"
            with StateStore(database) as store:
                store.migrate()
                store.create_run(
                    Run.new(
                        run_id="run-1",
                        repository="owner/repo",
                        issue_number=1,
                        prompt_sha256="a" * 64,
                        base_branch="main",
                        cloud_environment_id="env-1",
                    )
                )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["status", "--database", str(database), "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual("ok", payload["integrity"])
        self.assertEqual(["run-1"], payload["active_runs"])

    def test_run_once_requires_explicit_dry_run(self) -> None:
        with self.assertRaises(SystemExit):
            main(["run-once", "--config", "config.toml"])

    def test_run_once_reports_missing_gh_without_external_write(self) -> None:
        stdout = io.StringIO()
        with patch("codex_dispatcher.cli.shutil.which", return_value=None):
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    ["run-once", "--dry-run", "--config", "missing.toml", "--json"]
                )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, exit_code)
        self.assertEqual("gh executable not found", payload["error"])

    def test_run_once_passes_environment_token_without_printing_it(self) -> None:
        stdout = io.StringIO()
        token = "github_pat_test_fixture"
        with (
            patch("codex_dispatcher.cli.shutil.which", return_value="/usr/bin/gh"),
            patch("codex_dispatcher.cli.load_config", return_value=make_config()),
            patch("codex_dispatcher.cli.run_once_dry_run") as dry_run,
            patch(
                "codex_dispatcher.trackers.github_cli.GitHubCliTracker"
            ) as tracker_class,
            patch.dict("os.environ", {"GITHUB_TOKEN": token}, clear=True),
            contextlib.redirect_stdout(stdout),
        ):
            dry_run.return_value = SimpleNamespace(selected=(), rejected=())
            exit_code = main(
                ["run-once", "--dry-run", "--config", "config.toml", "--json"]
            )

        self.assertEqual(0, exit_code)
        tracker_class.assert_called_once_with(gh_path=Path("/usr/bin/gh"), token=token)
        self.assertNotIn(token, stdout.getvalue())

    def test_run_once_ignores_unrecognized_environment_token(self) -> None:
        stdout = io.StringIO()
        with (
            patch("codex_dispatcher.cli.shutil.which", return_value="/usr/bin/gh"),
            patch("codex_dispatcher.cli.load_config", return_value=make_config()),
            patch("codex_dispatcher.cli.run_once_dry_run") as dry_run,
            patch(
                "codex_dispatcher.trackers.github_cli.GitHubCliTracker"
            ) as tracker_class,
            patch.dict("os.environ", {"GITHUB_TOKEN": "unrecognized-token"}, clear=True),
            contextlib.redirect_stdout(stdout),
        ):
            dry_run.return_value = SimpleNamespace(selected=(), rejected=())
            exit_code = main(
                ["run-once", "--dry-run", "--config", "config.toml", "--json"]
            )

        self.assertEqual(0, exit_code)
        tracker_class.assert_called_once_with(gh_path=Path("/usr/bin/gh"), token=None)


if __name__ == "__main__":
    unittest.main()
