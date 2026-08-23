from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_dispatcher.prompt_builder import PromptSnapshot
from codex_dispatcher.publisher import VerifiedBundle
from codex_dispatcher.runner_protocol import (
    NEXT_PROTOCOL_VERSION,
    RunnerOperation,
    parse_agent_result,
)
from codex_dispatcher.runner_transport import (
    RunnerInactiveContainerState,
    RunnerTurnRemoteState,
    RunnerTurnReply,
    RunnerTransportInterrupted,
    RunnerWireOutput,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.testing.fake_runner import (
    FakeBundleVerifier,
    FakeSshRunnerTransport,
    FakeTurnFixture,
)
from codex_dispatcher.turn_orchestration import OfflineTurnOrchestrator
from codex_dispatcher.turn_orchestration import TurnOrchestrationError
from codex_dispatcher.work_items import (
    PromptKind,
    SessionGenerationRole,
    SessionGenerationState,
    TurnState,
    WorkItem,
    WorkItemState,
)


SESSION = "123e4567-e89b-12d3-a456-426614174000"


def prompt(text: str) -> PromptSnapshot:
    return PromptSnapshot(text, sha256(text.encode()).hexdigest(), ())


def result(status: str, *, path: str, summary: str):
    questions = ["Which format should be used?"] if status == "needs_input" else []
    return parse_agent_result(
        json.dumps(
            {
                "schema_version": 2,
                "status": status,
                "summary": summary,
                "acceptance": [],
                "remaining_work": [],
                "needs_input": questions,
                "tests": [{"name": "unit", "status": "passed"}],
                "changed_paths": [path],
                "blocker_code": "test_blocked" if status == "blocked" else None,
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
            issue_allowed_paths=("src",),
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
        with self.assertRaisesRegex(TurnOrchestrationError, "frozen Turn policy"):
            self.service.prepare_publication(
                first.turn.turn_id,
                issue_allowed_paths=("docs",),
                repository_allowed_paths=("src", "docs"),
            )
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
            issue_allowed_paths=("src",),
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

    def test_recorded_publication_hook_recovers_without_runner_or_publisher(self) -> None:
        item = work_item()
        self.store.create_work_item(item)
        hook_calls: list[tuple[WorkItem, object]] = []

        def interrupt_after_record(work_item: WorkItem, turn) -> None:
            hook_calls.append((work_item, turn))
            raise RuntimeError("fixture stopped after publication record")

        service = OfflineTurnOrchestrator(
            store=self.store,
            transport=self.transport,
            bundle_verifier=self.verifier,
            publication_recorded_hook=interrupt_after_record,
        )
        service.prepare_work_item(item.work_item_id, source_bundle=b"fixture-base-bundle")
        artifact = b"fixture-recorded-publication"
        head_sha = "b" * 40
        self.verifier.register(
            VerifiedBundle(
                bundle_sha256=sha256(artifact).hexdigest(),
                head_sha=head_sha,
                parent_anchor_sha="a" * 40,
                changed_paths=("src/recorded.py",),
                commit_count=1,
                size_bytes=len(artifact),
            )
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(
                SESSION,
                head_sha,
                result(
                    "completed",
                    path="src/recorded.py",
                    summary="Recorded checkpoint",
                ),
                artifact,
            ),
        )
        progress = service.run_turn(
            item.work_item_id,
            issue_revision="revision-recorded",
            prompt=prompt("record one checkpoint\n"),
            issue_allowed_paths=("src",),
            turn_id="turn_" + "9" * 32,
        )
        publisher_calls: list[object] = []

        def publish(artifact, *, plan, work_item):
            publisher_calls.append(plan)
            return SimpleNamespace(observed_remote_sha=plan.source_sha)

        with self.assertRaisesRegex(RuntimeError, "after publication record"):
            service.publish_checkpoint(
                progress.turn.turn_id,
                publisher=SimpleNamespace(publish=publish),
                repository_allowed_paths=("src",),
            )

        recorded_item = self.store.get_work_item(item.work_item_id)
        recorded_turn = self.store.get_turn(progress.turn.turn_id)
        assert recorded_item is not None and recorded_turn is not None
        self.assertEqual(head_sha, recorded_item.last_published_sha)
        self.assertEqual(WorkItemState.RUNNING, recorded_item.state)
        self.assertEqual(TurnState.CHECKPOINTING, recorded_turn.state)
        self.assertEqual(1, len(hook_calls))
        self.assertEqual(1, len(publisher_calls))
        runner_calls = tuple(self.transport.calls)

        recovery = OfflineTurnOrchestrator(
            store=self.store,
            transport=self.transport,
            bundle_verifier=self.verifier,
        )

        def reject_publish(*args, **kwargs):
            raise AssertionError("recorded recovery must not invoke Publisher")

        completed = recovery.publish_checkpoint(
            progress.turn.turn_id,
            publisher=SimpleNamespace(publish=reject_publish),
            repository_allowed_paths=("src",),
        )

        self.assertEqual(TurnState.FINISHED, completed.turn.state)
        self.assertEqual(WorkItemState.REVIEW, completed.work_item.state)
        self.assertEqual(runner_calls, tuple(self.transport.calls))
        self.assertEqual(1, len(publisher_calls))

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
            issue_allowed_paths=("src",),
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
                issue_allowed_paths=("src",),
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

    def test_explicit_inactive_abandonment_requires_plan_then_stop_receipt(self) -> None:
        item = work_item(45)
        self.store.create_work_item(item)
        self.store.update_work_item_state(item.work_item_id, WorkItemState.PREPARING)
        self.store.update_work_item_state(item.work_item_id, WorkItemState.READY)
        policy = "c" * 64
        generation = self.store.plan_session_generation(
            item.work_item_id,
            role=SessionGenerationRole.IMPLEMENTATION,
            policy_sha256=policy,
        )
        _, generation, turn, _ = self.store.begin_session_generation_turn(
            item.work_item_id,
            session_generation_id=generation.session_generation_id,
            generation_number=1,
            policy_sha256=policy,
            prompt_kind=PromptKind.FULL,
            issue_revision="revision",
            issue_content_sha256="d" * 64,
            task_spec_sha256="e" * 64,
            prompt_sha256="f" * 64,
            approved_comment_ids=(),
            approved_context_sha256="1" * 64,
            issue_allowed_paths=("src",),
            input_head_sha="a" * 40,
        )
        self.store.record_generation_turn_unknown(
            turn.turn_id,
            session_generation_id=generation.session_generation_id,
            generation_number=1,
            policy_sha256=policy,
        )

        class InactiveTransport:
            def __init__(self):
                self.operations = []
                self.committed = False
                self.interrupt_stop_once = True

            def invoke(self, request, *, stdin=b"", source_artifact=None):
                self.operations.append(request.operation)
                if request.operation is RunnerOperation.STATUS:
                    state = (
                        RunnerTurnRemoteState.FAILED
                        if self.committed
                        else RunnerTurnRemoteState.UNKNOWN
                    )
                    error_code = (
                        "turn_abandoned_inactive"
                        if self.committed
                        else "turn_container_inactive"
                    )
                elif request.operation is RunnerOperation.STOP:
                    state = RunnerTurnRemoteState.FAILED
                    error_code = "turn_abandoned_inactive"
                    self.committed = True
                else:
                    raise AssertionError("unexpected operation")
                reply = RunnerTurnReply(
                    operation=request.operation,
                    work_item_id=request.work_item_id,
                    turn_id=request.turn_id,
                    state=state,
                    error_code=error_code,
                    inactive_container_state=RunnerInactiveContainerState.ABSENT,
                    inactive_observed_at="2026-08-23T00:00:00+00:00",
                    session_generation_id=request.session_generation_id,
                    session_generation=request.session_generation,
                    agent_policy_digest=request.agent_policy_digest,
                    version=NEXT_PROTOCOL_VERSION,
                )
                output = RunnerWireOutput(reply.to_json().encode("utf-8"))
                if (
                    request.operation is RunnerOperation.STOP
                    and self.interrupt_stop_once
                ):
                    self.interrupt_stop_once = False
                    raise RunnerTransportInterrupted(
                        "fixture lost committed STOP response"
                    )
                return output

        transport = InactiveTransport()
        service = OfflineTurnOrchestrator(
            store=self.store,
            transport=transport,
            bundle_verifier=self.verifier,
        )
        inspection = service.inspect_turn_abandonment(turn.turn_id)
        self.assertEqual("turn_container_inactive", inspection.error_code)
        self.assertEqual(
            TurnState.RECONCILING, self.store.get_turn(turn.turn_id).state
        )
        with self.assertRaisesRegex(
            TurnOrchestrationError, "did not commit"
        ):
            service.abandon_unknown_turn(turn.turn_id)
        self.assertEqual(
            TurnState.RECONCILING, self.store.get_turn(turn.turn_id).state
        )
        recovered = service.inspect_turn_abandonment(turn.turn_id)
        self.assertEqual("turn_abandoned_inactive", recovered.error_code)
        progress, receipt = service.abandon_unknown_turn(turn.turn_id)
        self.assertEqual(WorkItemState.BLOCKED, progress.work_item.state)
        self.assertEqual(TurnState.BLOCKED, progress.turn.state)
        self.assertEqual("absent", receipt.inactive_container_state.value)
        self.assertEqual(
            [
                RunnerOperation.STATUS,
                RunnerOperation.STATUS,
                RunnerOperation.STOP,
                RunnerOperation.STATUS,
                RunnerOperation.STATUS,
                RunnerOperation.STOP,
            ],
            transport.operations,
        )
        self.assertEqual(
            SessionGenerationState.FAILED,
            self.store.get_session_generation(
                generation.session_generation_id
            ).state,
        )
        repeated_progress, repeated_receipt = service.abandon_unknown_turn(
            turn.turn_id
        )
        self.assertEqual(progress, repeated_progress)
        self.assertEqual(receipt, repeated_receipt)
        self.assertEqual(
            [RunnerOperation.STATUS, RunnerOperation.STOP],
            transport.operations[-2:],
        )

    def test_migrated_legacy_generation_reconciles_with_v1_status(self) -> None:
        item = work_item(47)
        self.store.create_work_item(item)
        self.service.prepare_work_item(
            item.work_item_id, source_bundle=b"fixture-base-bundle"
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(
                SESSION,
                "a" * 40,
                result("needs_input", path="src/legacy.py", summary="Need input"),
            ),
        )
        self.transport.interrupt_next(RunnerOperation.START)
        progress = self.service.run_turn(
            item.work_item_id,
            issue_revision="legacy-revision",
            prompt=prompt("recover migrated legacy turn\n"),
            issue_allowed_paths=("src",),
            turn_id="turn_" + "7" * 32,
        )
        generation_id = "sg_" + "7" * 32
        now = "2026-01-01T01:00:00.000000Z"
        with self.store._transaction() as connection:
            connection.execute(
                "UPDATE work_items SET codex_session_id = ?, updated_at = ? "
                "WHERE work_item_id = ?",
                (SESSION, now, item.work_item_id),
            )
            connection.execute(
                "INSERT INTO session_generations ("
                "session_generation_id, work_item_id, generation_number, state, role, "
                "codex_session_id, start_head_sha, policy_sha256, rotation_reason, "
                "created_at, started_at, updated_at"
                ") VALUES (?, ?, 1, 'active', 'implementation', ?, ?, NULL, "
                "'legacy_migration', ?, ?, ?)",
                (generation_id, item.work_item_id, SESSION, "a" * 40, now, now, now),
            )
            connection.execute(
                "INSERT INTO turn_session_generations (turn_id, session_generation_id) "
                "VALUES (?, ?)",
                (progress.turn.turn_id, generation_id),
            )

        reconciled = self.service.reconcile_turn(progress.turn.turn_id)

        self.assertEqual(TurnState.NEEDS_INPUT, reconciled.turn.state)
        status = self.transport.calls[-1]
        self.assertEqual(RunnerOperation.STATUS, status.operation)
        self.assertEqual(1, status.version)
        self.assertIsNone(status.session_generation_id)

    def test_remote_failure_binds_session_and_persists_machine_error(self) -> None:
        item = work_item(45)
        self.store.create_work_item(item)
        self.service.prepare_work_item(
            item.work_item_id, source_bundle=b"fixture-base-bundle"
        )
        original_invoke = self.transport.invoke

        def invoke(request, *, stdin=b"", source_artifact=None):
            if request.operation is RunnerOperation.START:
                assert request.turn_id is not None
                reply = RunnerTurnReply(
                    RunnerOperation.START,
                    request.work_item_id,
                    request.turn_id,
                    RunnerTurnRemoteState.FAILED,
                    session_id=SESSION,
                    error_code="agent_result_invalid",
                )
                return RunnerWireOutput(reply.to_json().encode("utf-8"))
            return original_invoke(
                request, stdin=stdin, source_artifact=source_artifact
            )

        with patch.object(self.transport, "invoke", side_effect=invoke):
            progress = self.service.run_turn(
                item.work_item_id,
                issue_revision="revision-invalid-result",
                prompt=prompt("return a structured result\n"),
                issue_allowed_paths=("src",),
                turn_id="turn_" + "5" * 32,
            )

        self.assertEqual(WorkItemState.BLOCKED, progress.work_item.state)
        self.assertEqual(TurnState.BLOCKED, progress.turn.state)
        self.assertEqual(SESSION, progress.work_item.codex_session_id)
        self.assertEqual("agent_result_invalid", progress.turn.error_code)

    def test_stale_prompt_turn_number_fails_before_runner_invocation(self) -> None:
        item = work_item(46)
        self.store.create_work_item(item)
        self.service.prepare_work_item(
            item.work_item_id, source_bundle=b"fixture-base-bundle"
        )

        with self.assertRaisesRegex(RuntimeError, "Prompt snapshot"):
            self.service.run_turn(
                item.work_item_id,
                issue_revision="revision-1",
                prompt=prompt("stale turn number\n"),
                issue_allowed_paths=("src",),
                expected_turn_number=2,
            )

        self.assertEqual(1, len(self.transport.calls))
        self.assertEqual(RunnerOperation.PREPARE, self.transport.calls[0].operation)
        self.assertEqual((), self.store.list_turns(item.work_item_id))
        self.assertEqual(WorkItemState.READY, self.store.get_work_item(item.work_item_id).state)

    def test_turn_persists_only_included_context_ids_not_prompt_content(self) -> None:
        item = work_item(47)
        self.store.create_work_item(item)
        self.service.prepare_work_item(
            item.work_item_id, source_bundle=b"fixture-base-bundle"
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(
                SESSION,
                "a" * 40,
                result("blocked", path="src/none.py", summary="Blocked safely"),
            ),
        )
        content = "private context must not be persisted\n"
        snapshot = PromptSnapshot(content, sha256(content.encode()).hexdigest(), ("IC_1",))

        progress = self.service.run_turn(
            item.work_item_id,
            issue_revision="revision-context",
            prompt=snapshot,
            issue_allowed_paths=("src",),
            turn_id="turn_" + "7" * 32,
        )

        persisted = self.store.get_turn(progress.turn.turn_id)
        self.assertEqual(("IC_1",), persisted.included_comment_ids)
        self.assertNotIn(content.strip(), repr(persisted))


if __name__ == "__main__":
    unittest.main()
