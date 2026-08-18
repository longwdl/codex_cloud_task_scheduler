from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from codex_dispatcher.prompt_builder import PromptSnapshot
from codex_dispatcher.publisher import VerifiedBundle
from codex_dispatcher.runner_protocol import RunnerOperation, parse_agent_result
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.testing.fake_runner import (
    FakeBundleVerifier,
    FakeSshRunnerTransport,
    FakeTurnFixture,
)
from codex_dispatcher.turn_orchestration import OfflineTurnOrchestrator
from codex_dispatcher.work_items import TurnState, WorkItem, WorkItemState


SESSION = "123e4567-e89b-12d3-a456-426614174000"


def prompt(text: str) -> PromptSnapshot:
    return PromptSnapshot(text, sha256(text.encode()).hexdigest(), ())


def result(status: str, *, path: str, summary: str):
    questions = ["Which format should be used?"] if status == "needs_input" else []
    return parse_agent_result(
        json.dumps(
            {
                "status": status,
                "summary": summary,
                "needs_input": questions,
                "tests": [{"name": "unit", "status": "passed"}],
                "changed_paths": [path],
                "next_step": "Continue from the Issue",
            }
        )
    )


def work_item(issue_number: int = 42) -> WorkItem:
    return WorkItem.new(
        repository="owner/repo",
        issue_number=issue_number,
        issue_node_id=f"I_kwDOFixture{issue_number}",
        base_branch="main",
        base_sha="a" * 40,
        at="2026-01-01T00:00:00.000000Z",
    )


class TurnOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "state.db")
        self.store.migrate()
        self.transport = FakeSshRunnerTransport()
        self.verifier = FakeBundleVerifier()
        self.service = OfflineTurnOrchestrator(
            store=self.store,
            transport=self.transport,
            bundle_verifier=self.verifier,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_first_turn_and_followup_reuse_session_branch_and_runner_identity(self) -> None:
        item = work_item()
        self.store.create_work_item(item)
        ready = self.service.prepare_work_item(
            item.work_item_id, source_bundle=b"fixture-base-bundle"
        )
        self.assertEqual(WorkItemState.READY, ready.state)

        first_artifact = b"fake-bundle-one"
        first_bundle = VerifiedBundle(
            bundle_sha256=sha256(first_artifact).hexdigest(),
            head_sha="b" * 40,
            parent_anchor_sha="a" * 40,
            changed_paths=("src/first.py",),
            commit_count=1,
            size_bytes=len(first_artifact),
        )
        self.verifier.register(first_bundle)
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(
                SESSION,
                "b" * 40,
                result("completed", path="src/first.py", summary="First checkpoint"),
                first_artifact,
            ),
        )
        first = self.service.run_turn(
            item.work_item_id,
            issue_revision="revision-1",
            prompt=prompt("implement first checkpoint\n"),
            turn_id="turn_" + "1" * 32,
        )
        self.assertTrue(first.checkpoint_ready)
        self.assertEqual(SESSION, first.work_item.codex_session_id)
        first_plan = self.service.prepare_publication(
            first.turn.turn_id,
            issue_allowed_paths=("src",),
            repository_allowed_paths=("src", "tests"),
        )
        self.assertIsNone(first_plan.expected_remote_sha)
        first = self.service.complete_publication(
            first.turn.turn_id,
            plan=first_plan,
            observed_remote_sha="b" * 40,
        )
        self.assertEqual(TurnState.FINISHED, first.turn.state)
        self.assertEqual(WorkItemState.REVIEW, first.work_item.state)
        self.assertEqual("b" * 40, first.work_item.last_published_sha)
        self.assertEqual(
            first,
            self.service.complete_publication(
                first.turn.turn_id,
                plan=first_plan,
                observed_remote_sha="b" * 40,
            ),
        )

        ready_again = self.store.update_work_item_state(
            item.work_item_id, WorkItemState.READY
        )
        second_artifact = b"fake-bundle-two"
        second_bundle = VerifiedBundle(
            bundle_sha256=sha256(second_artifact).hexdigest(),
            head_sha="c" * 40,
            parent_anchor_sha="b" * 40,
            changed_paths=("src/second.py",),
            commit_count=1,
            size_bytes=len(second_artifact),
        )
        self.verifier.register(second_bundle)
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(
                SESSION,
                "c" * 40,
                result("needs_input", path="src/second.py", summary="Need decision"),
                second_artifact,
            ),
        )
        second = self.service.run_turn(
            item.work_item_id,
            issue_revision="revision-2",
            prompt=prompt("continue after clarification\n"),
            turn_id="turn_" + "2" * 32,
        )
        second_plan = self.service.prepare_publication(
            second.turn.turn_id,
            issue_allowed_paths=("src",),
            repository_allowed_paths=("src",),
        )
        self.assertEqual("b" * 40, second_plan.expected_remote_sha)
        second = self.service.complete_publication(
            second.turn.turn_id,
            plan=second_plan,
            observed_remote_sha="c" * 40,
        )
        self.assertEqual(TurnState.NEEDS_INPUT, second.turn.state)
        self.assertEqual(WorkItemState.WAITING_INPUT, second.work_item.state)
        self.assertEqual(SESSION, second.work_item.codex_session_id)
        self.assertEqual(ready.task_branch, ready_again.task_branch)
        self.assertEqual(ready.runner_directory, ready_again.runner_directory)
        self.assertEqual(
            [1, 2],
            [turn.turn_number for turn in self.store.list_turns(item.work_item_id)],
        )
        operations = [call.operation for call in self.transport.calls]
        self.assertEqual(1, operations.count(RunnerOperation.START))
        self.assertEqual(1, operations.count(RunnerOperation.RESUME))
        self.assertTrue(all(not hasattr(call, "stdin") for call in self.transport.calls))

    def test_interrupted_start_is_reconciled_without_resending_prompt(self) -> None:
        item = work_item(43)
        self.store.create_work_item(item)
        self.service.prepare_work_item(
            item.work_item_id, source_bundle=b"fixture-base-bundle"
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(
                SESSION,
                "a" * 40,
                result("needs_input", path="src/question.py", summary="Need input"),
            ),
        )
        self.transport.interrupt_next(RunnerOperation.START)
        progress = self.service.run_turn(
            item.work_item_id,
            issue_revision="revision-1",
            prompt=prompt("ask for a decision\n"),
            turn_id="turn_" + "3" * 32,
        )
        self.assertEqual(TurnState.RECONCILING, progress.turn.state)
        self.assertEqual(WorkItemState.RUNNING, progress.work_item.state)

        second = work_item(44)
        self.store.create_work_item(second)
        self.service.prepare_work_item(
            second.work_item_id, source_bundle=b"fixture-base-bundle"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.run_turn(
                second.work_item_id,
                issue_revision="revision-1",
                prompt=prompt("must not run concurrently\n"),
                turn_id="turn_" + "4" * 32,
            )

        reconciled = self.service.reconcile_turn(progress.turn.turn_id)
        self.assertEqual(TurnState.NEEDS_INPUT, reconciled.turn.state)
        self.assertEqual(WorkItemState.WAITING_INPUT, reconciled.work_item.state)
        self.assertEqual(SESSION, reconciled.work_item.codex_session_id)
        operations = [call.operation for call in self.transport.calls]
        self.assertEqual(1, operations.count(RunnerOperation.START))
        self.assertEqual(0, operations.count(RunnerOperation.RESUME))
        self.assertEqual(1, operations.count(RunnerOperation.STATUS))


if __name__ == "__main__":
    unittest.main()
