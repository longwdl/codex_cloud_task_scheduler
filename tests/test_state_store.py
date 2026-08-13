from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_dispatcher.domain import Run, RunState
from codex_dispatcher.state_store import StateStore


def make_run(issue_number: int, *, run_id: str, attempt_no: int = 1) -> Run:
    return Run.new(
        run_id=run_id,
        repository="owner/repo",
        issue_number=issue_number,
        attempt_no=attempt_no,
        prompt_sha256="a" * 64,
        base_branch="main",
        cloud_environment_id="env-1",
    )


class StateStoreTests(unittest.TestCase):
    def test_migrate_persist_and_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.db"
            with StateStore(path) as store:
                store.migrate()
                run = make_run(1, run_id="run-1")
                store.create_run(run)
                updated = store.update_state(run.run_id, RunState.CLAIMED)
                self.assertEqual(RunState.CLAIMED, updated.state)
                self.assertEqual("ok", store.integrity_check())
            with StateStore(path) as reopened:
                reopened.migrate()
                loaded = reopened.get_run("run-1")
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertEqual(RunState.CLAIMED, loaded.state)

    def test_active_issue_and_cloud_task_constraints_are_database_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                first = make_run(1, run_id="run-1")
                store.create_run(first)
                with self.assertRaises(sqlite3.IntegrityError):
                    store.create_run(make_run(1, run_id="run-2", attempt_no=2))
                store.bind_cloud_task(first.run_id, "cloud-1")
                terminal = store.update_state(first.run_id, RunState.DISCARDED)
                self.assertTrue(terminal.is_terminal)
                second = make_run(1, run_id="run-2", attempt_no=2)
                store.create_run(second)
                with self.assertRaises(sqlite3.IntegrityError):
                    store.bind_cloud_task(second.run_id, "cloud-1")

    def test_events_backup_and_foreign_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "state.db"
            backup = root / "backup.db"
            with StateStore(database) as store:
                store.migrate()
                run = make_run(3, run_id="run-3")
                store.create_run(run)
                event_id = store.append_event(run.run_id, "created", {"source": "test"})
                self.assertGreater(event_id, 0)
                with self.assertRaises(sqlite3.IntegrityError):
                    store.append_event("missing", "created", {})
                store.backup(backup)
            with StateStore(backup) as restored:
                restored.migrate()
                self.assertIsNotNone(restored.get_run("run-3"))
                self.assertEqual("ok", restored.integrity_check())


if __name__ == "__main__":
    unittest.main()
