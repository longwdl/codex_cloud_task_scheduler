from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from codex_dispatcher.cli import main, run_once_dry_run
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


if __name__ == "__main__":
    unittest.main()
