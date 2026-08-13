from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from codex_dispatcher.cloud_dispatch import CloudDispatchPreparationService
from codex_dispatcher.domain import Run, RunState
from codex_dispatcher.executors.base import RemoteRun, RunStatus
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.testing.fakes import FakeExecutor


PROMPT = "deterministic prompt\n"


def prepared_run(store: StateStore) -> Run:
    store.migrate()
    run = Run.new(
        run_id="dispatch-run",
        repository="owner/repo",
        issue_number=10,
        prompt_sha256=hashlib.sha256(PROMPT.encode("utf-8")).hexdigest(),
        base_branch="main",
        cloud_environment_id="env-1",
    )
    store.create_run(run)
    store.update_state(run.run_id, RunState.CLAIMED)
    store.record_branch_anchor(
        run.run_id,
        base_sha="a" * 40,
        task_branch="codex/issue-10-abcdef012345",
    )
    return store.mark_branch_prepared(
        run.run_id,
        head_sha="a" * 40,
        remote_reused=False,
    )


class CloudDispatchPreparationTests(unittest.TestCase):
    def test_snapshots_remote_ids_before_persisting_dispatch_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                run = prepared_run(store)
                executor = FakeExecutor()
                executor.runs = (
                    RemoteRun("task-b", None, "env-1", RunStatus.RUNNING, "other/b"),
                    RemoteRun("task-a", None, "env-1", RunStatus.QUEUED, "other/a"),
                )
                result = CloudDispatchPreparationService(
                    store=store, executor=executor
                ).prepare(run.run_id, prompt=PROMPT)

                self.assertEqual(RunState.DISPATCHING, result.run.state)
                self.assertEqual(("task-a", "task-b"), result.known_task_ids)
                self.assertEqual(PROMPT, result.request.prompt)
                self.assertEqual("list_runs", executor.calls[0].method)
                self.assertFalse(any(call.method == "submit" for call in executor.calls))

    def test_prompt_mismatch_fails_before_executor_read_or_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                run = prepared_run(store)
                executor = FakeExecutor()
                service = CloudDispatchPreparationService(store=store, executor=executor)
                with self.assertRaisesRegex(ValueError, "immutable snapshot hash"):
                    service.prepare(run.run_id, prompt="different")
                self.assertEqual([], executor.calls)
                loaded = store.get_run(run.run_id)
                assert loaded is not None
                self.assertEqual(RunState.BRANCH_PREPARED, loaded.state)

    def test_invalid_remote_snapshot_fails_before_dispatch_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                run = prepared_run(store)
                executor = FakeExecutor()
                executor.set_result(
                    "list_runs",
                    (
                        RemoteRun(
                            "task-a", None, "other-env", RunStatus.QUEUED, "other/a"
                        ),
                    ),
                )
                with self.assertRaisesRegex(ValueError, "invalid pre-submit"):
                    CloudDispatchPreparationService(
                        store=store, executor=executor
                    ).prepare(run.run_id, prompt=PROMPT)
                loaded = store.get_run(run.run_id)
                assert loaded is not None
                self.assertEqual(RunState.BRANCH_PREPARED, loaded.state)


if __name__ == "__main__":
    unittest.main()
