from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
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

    def test_branch_anchor_and_completion_are_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.db"
            with StateStore(path) as store:
                store.migrate()
                run = make_run(4, run_id="run-4")
                store.create_run(run)
                store.update_state(run.run_id, RunState.CLAIMED)
                anchored = store.record_branch_anchor(
                    run.run_id,
                    base_sha="b" * 40,
                    task_branch="codex/issue-4-abcdef012345",
                    updated_at="2026-01-01T00:00:00.000000Z",
                )
                self.assertEqual(RunState.CLAIMED, anchored.state)
                self.assertEqual("b" * 40, anchored.base_sha)
                self.assertEqual("codex/issue-4-abcdef012345", anchored.task_branch)
                self.assertEqual(
                    anchored,
                    store.record_branch_anchor(
                        run.run_id,
                        base_sha="b" * 40,
                        task_branch="codex/issue-4-abcdef012345",
                    ),
                )
                with self.assertRaisesRegex(ValueError, "different branch anchor"):
                    store.record_branch_anchor(
                        run.run_id,
                        base_sha="c" * 40,
                        task_branch="codex/issue-4-abcdef012345",
                    )

                prepared = store.mark_branch_prepared(
                    run.run_id,
                    head_sha="b" * 40,
                    remote_reused=False,
                    updated_at="2026-01-01T00:00:01.000000Z",
                )
                self.assertEqual(RunState.BRANCH_PREPARED, prepared.state)
                self.assertEqual("b" * 40, prepared.head_sha)
                self.assertEqual(
                    prepared,
                    store.mark_branch_prepared(
                        run.run_id, head_sha="b" * 40, remote_reused=True
                    ),
                )
            with closing(sqlite3.connect(path)) as connection:
                events = connection.execute(
                    "SELECT event_type FROM run_events WHERE run_id = ? ORDER BY event_id",
                    ("run-4",),
                ).fetchall()
            self.assertEqual(
                [("branch_anchor_recorded",), ("branch_prepared",)],
                events,
            )

    def test_branch_completion_rejects_missing_or_mismatched_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                run = make_run(5, run_id="run-5")
                store.create_run(run)
                store.update_state(run.run_id, RunState.CLAIMED)
                with self.assertRaisesRegex(ValueError, "anchor must be persisted"):
                    store.mark_branch_prepared(
                        run.run_id, head_sha="d" * 40, remote_reused=False
                    )
                store.record_branch_anchor(
                    run.run_id,
                    base_sha="d" * 40,
                    task_branch="codex/issue-5-abcdef012345",
                )
                with self.assertRaisesRegex(ValueError, "must equal"):
                    store.mark_branch_prepared(
                        run.run_id, head_sha="e" * 40, remote_reused=False
                    )

    def test_cloud_dispatch_boundary_is_atomic_and_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.db"
            with StateStore(path) as store:
                store.migrate()
                run = make_run(6, run_id="run-6")
                store.create_run(run)
                store.update_state(run.run_id, RunState.CLAIMED)
                store.record_branch_anchor(
                    run.run_id,
                    base_sha="f" * 40,
                    task_branch="codex/issue-6-abcdef012345",
                )
                store.mark_branch_prepared(
                    run.run_id,
                    head_sha="f" * 40,
                    remote_reused=False,
                )
                dispatching = store.begin_cloud_dispatch(
                    run.run_id,
                    known_task_ids=("task-b", "task-a"),
                    updated_at="2026-01-01T00:00:02.000000Z",
                )
                self.assertEqual(RunState.DISPATCHING, dispatching.state)
                with self.assertRaisesRegex(ValueError, "branch-prepared"):
                    store.begin_cloud_dispatch(run.run_id, known_task_ids=())
            with closing(sqlite3.connect(path)) as connection:
                event_type, payload_json = connection.execute(
                    "SELECT event_type, payload_json FROM run_events "
                    "WHERE run_id = ? ORDER BY event_id DESC LIMIT 1",
                    ("run-6",),
                ).fetchone()
            self.assertEqual("cloud_dispatch_started", event_type)
            self.assertIn('"known_task_ids":["task-a","task-b"]', payload_json)
            self.assertNotIn("prompt\"", payload_json)


if __name__ == "__main__":
    unittest.main()
