from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from codex_dispatcher.ci_evidence import (
    ActionsEvidenceSnapshot,
    ActionsRunEvidence,
    CiEvidenceError,
    RequiredCheckEvidence,
)
from codex_dispatcher.codex_jsonl import CodexTurnUsage
from codex_dispatcher.config import SessionRuntimeConfig
from codex_dispatcher.publisher import VerifiedBundle
from codex_dispatcher.runner_protocol import RunnerOperation, parse_agent_result
from codex_dispatcher.source_bundle import SourceBundle
from codex_dispatcher.ssh_dispatch_planning import SshDispatchPlanningError
from codex_dispatcher.ssh_dispatch_service import OfflineSshDispatchService
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.testing.fake_runner import (
    FakeBundleVerifier,
    FakeSshRunnerTransport,
    FakeTurnFixture,
)
from codex_dispatcher.testing.fakes import FakeTracker
from codex_dispatcher.trackers.base import TaskState
from codex_dispatcher.turn_orchestration import OfflineTurnOrchestrator
from codex_dispatcher.work_items import TurnState, WorkItemState
from tests.test_scheduler import make_config
from tests.test_ssh_dispatch_planning import BASE_SHA, claimed_task


SESSION = "123e4567-e89b-12d3-a456-426614174000"
SESSION_2 = "223e4567-e89b-12d3-a456-426614174000"
POLICY_DIGEST = "d" * 64
POLICY_DIGEST_2 = "e" * 64


def source_bundle(base_sha: str = BASE_SHA) -> SourceBundle:
    artifact = b"fixture-source-bundle"
    return SourceBundle(artifact, base_sha, sha256(artifact).hexdigest(), len(artifact))


def blocked_result():
    return parse_agent_result(
        json.dumps(
            {
                "status": "blocked",
                "summary": "No repository changes were required",
                "needs_input": [],
                "tests": [{"name": "inspection", "status": "passed"}],
                "changed_paths": [],
                "next_step": "Record the blocker in the Issue",
            }
        )
    )


def session_runtime_config(**overrides: object) -> SessionRuntimeConfig:
    values: dict[str, object] = {
        "protocol_version": 2,
        "agent_policy_digest": POLICY_DIGEST,
        "max_turns_per_session": 4,
        "rotate_after_input_tokens": 120_000,
        "rotate_after_session_age_seconds": 14_400,
        "rotate_before_final_audit": False,
        "use_incremental_resume_prompts": True,
        "max_session_generations": 3,
        "max_total_turns": 10,
        "max_no_progress_turns": 2,
    }
    values.update(overrides)
    return SessionRuntimeConfig(**values)  # type: ignore[arg-type]


class OfflineSshDispatchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "state.db")
        self.store.migrate()
        self.transport = FakeSshRunnerTransport()
        self.verifier = FakeBundleVerifier()
        self.orchestrator = OfflineTurnOrchestrator(
            store=self.store,
            transport=self.transport,
            bundle_verifier=self.verifier,
        )
        self.service = OfflineSshDispatchService(
            config=make_config(global_max_active=4),
            store=self.store,
            orchestrator=self.orchestrator,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_new_claimed_issue_is_persisted_and_prepared_once(self) -> None:
        item = self.service.resolve_and_prepare(
            claimed_task(),
            base_sha=BASE_SHA,
            source_bundle=source_bundle(),
            created_at="2026-08-13T02:00:00Z",
        )
        same = self.service.resolve_and_prepare(
            claimed_task(),
            base_sha="b" * 40,
        )

        self.assertEqual(WorkItemState.READY, item.state)
        self.assertEqual(item, same)
        self.assertEqual(
            [RunnerOperation.PREPARE],
            [call.operation for call in self.transport.calls],
        )
        self.assertEqual(
            item,
            self.store.get_work_item_by_issue("owner/repo", 42),
        )

    def test_turn_runs_from_frozen_plan_and_reactivation_reuses_session(self) -> None:
        item = self.service.resolve_and_prepare(
            claimed_task(), base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, blocked_result()),
        )

        first = self.service.run_claimed_turn(
            claimed_task(), turn_id="turn_" + "1" * 32
        )

        self.assertEqual(TurnState.BLOCKED, first.turn.state)
        self.assertEqual(WorkItemState.BLOCKED, first.work_item.state)
        self.assertEqual(1, first.turn.turn_number)
        reactivated = self.service.resolve_and_prepare(
            claimed_task(), base_sha="b" * 40
        )
        self.assertEqual(WorkItemState.READY, reactivated.state)
        self.assertEqual(SESSION, reactivated.codex_session_id)
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, blocked_result()),
        )
        second = self.service.run_claimed_turn(
            replace(claimed_task(), updated_at="2026-08-13T02:00:00Z"),
            turn_id="turn_" + "2" * 32,
        )

        self.assertEqual(2, second.turn.turn_number)
        self.assertEqual("2026-08-13T02:00:00Z", second.turn.issue_revision)
        self.assertEqual(
            [RunnerOperation.PREPARE, RunnerOperation.START, RunnerOperation.RESUME],
            [call.operation for call in self.transport.calls],
        )

    def test_v2_generation_starts_then_resumes_with_only_new_context(self) -> None:
        config = replace(
            make_config(global_max_active=4),
            session_runtime=session_runtime_config(),
        )
        service = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=self.orchestrator,
        )
        item = service.resolve_and_prepare(
            claimed_task(), base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        usage = CodexTurnUsage(2_000, 1_000, 0, 100, 50)
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, blocked_result(), usage=usage),
        )
        initial_comments = (
            {"id": "C1", "author": "alice", "body": "/codex-context initial"},
        )

        first = service.run_claimed_turn(
            claimed_task(),
            comments=initial_comments,
            turn_id="turn_" + "3" * 32,
        )

        self.assertEqual(TurnState.BLOCKED, first.turn.state)
        self.assertIsNone(first.work_item.codex_session_id)
        generation = self.store.get_live_session_generation(item.work_item_id)
        self.assertIsNotNone(generation)
        assert generation is not None
        self.assertEqual(SESSION, generation.codex_session_id)
        persisted_usage = self.store.get_turn_usage(first.turn.turn_id)
        self.assertIsNotNone(persisted_usage)
        assert persisted_usage is not None
        self.assertEqual(usage.input_tokens, persisted_usage.input_tokens)
        self.assertEqual(
            usage.cached_input_tokens, persisted_usage.cached_input_tokens
        )
        self.assertEqual(usage.output_tokens, persisted_usage.output_tokens)

        service.resolve_and_prepare(claimed_task(), base_sha="b" * 40)
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, blocked_result(), usage=usage),
        )
        second = service.run_claimed_turn(
            replace(claimed_task(), updated_at="2026-08-13T02:00:00Z"),
            comments=initial_comments
            + (
                {
                    "id": "C2",
                    "author": "alice",
                    "body": "/codex-context answer",
                },
            ),
            turn_id="turn_" + "4" * 32,
        )

        self.assertEqual(TurnState.BLOCKED, second.turn.state)
        self.assertEqual(
            [2, 2],
            [call.version for call in self.transport.calls if call.turn_id],
        )
        turn_prompts = self.store.list_session_generation_turn_prompt_inputs(
            generation.session_generation_id
        )
        self.assertEqual(["full", "delta"], [item.prompt_kind.value for item in turn_prompts])
        self.assertEqual(("C2",), second.turn.included_comment_ids)

    def test_v2_turn_budget_rotates_to_a_fresh_session_atomically(self) -> None:
        config = replace(
            make_config(global_max_active=4),
            session_runtime=session_runtime_config(max_turns_per_session=1),
        )
        service = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=self.orchestrator,
        )
        item = service.resolve_and_prepare(
            claimed_task(), base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        comments = (
            {"id": "C1", "author": "alice", "body": "/codex-context initial"},
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, blocked_result()),
        )
        service.run_claimed_turn(
            claimed_task(), comments=comments, turn_id="turn_" + "5" * 32
        )
        service.resolve_and_prepare(claimed_task(), base_sha=BASE_SHA)
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION_2, BASE_SHA, blocked_result()),
        )

        second = service.run_claimed_turn(
            replace(claimed_task(), updated_at="2026-08-13T02:00:00Z"),
            comments=comments,
            turn_id="turn_" + "6" * 32,
        )

        generations = self.store.list_session_generations(item.work_item_id)
        self.assertEqual(2, len(generations))
        self.assertEqual("retired", generations[0].state.value)
        self.assertEqual("active", generations[1].state.value)
        self.assertEqual("turn_budget", generations[1].rotation_reason)
        self.assertEqual(SESSION_2, generations[1].codex_session_id)
        handoff = self.store.get_session_handoff_for_generation(
            generations[1].session_generation_id
        )
        self.assertIsNotNone(handoff)
        assert handoff is not None
        self.assertEqual(handoff, self.store.get_turn_handoff(second.turn.turn_id))
        self.assertEqual(
            "unverified", handoff.trusted_facts["acceptance"]["status"]
        )
        self.assertEqual(
            [{"name": "tests", "status": "not_observed"}],
            handoff.trusted_facts["required_checks"],
        )
        self.assertEqual(
            "untrusted_advisory",
            handoff.untrusted_advisory["classification"],
        )
        self.assertTrue(handoff.untrusted_advisory["result_receipt_complete"])
        self.assertEqual(
            blocked_result().next_step,
            handoff.untrusted_advisory["recommended_next_action"],
        )
        self.assertEqual(
            [RunnerOperation.START, RunnerOperation.START],
            [call.operation for call in self.transport.calls if call.turn_id],
        )
        self.assertEqual(("C1",), second.turn.included_comment_ids)

    def test_v2_rotation_threshold_cannot_hide_issue_or_context_drift(self) -> None:
        config = replace(
            make_config(global_max_active=4),
            session_runtime=session_runtime_config(max_turns_per_session=1),
        )
        service = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=self.orchestrator,
        )
        item = service.resolve_and_prepare(
            claimed_task(), base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        comments = (
            {"id": "C1", "author": "alice", "body": "/codex-context initial"},
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, blocked_result()),
        )
        service.run_claimed_turn(
            claimed_task(), comments=comments, turn_id="turn_" + "a" * 32
        )
        service.resolve_and_prepare(claimed_task(), base_sha=BASE_SHA)

        with self.assertRaisesRegex(
            SshDispatchPlanningError, "Issue semantic content changed"
        ):
            service.run_claimed_turn(
                replace(claimed_task(), title="Unreviewed semantic edit"),
                comments=comments,
                turn_id="turn_" + "b" * 32,
            )
        with self.assertRaisesRegex(
            SshDispatchPlanningError, "previously delivered approved context was edited"
        ):
            service.run_claimed_turn(
                claimed_task(),
                comments=(
                    {
                        "id": "C1",
                        "author": "alice",
                        "body": "/codex-context edited",
                    },
                ),
                turn_id="turn_" + "c" * 32,
            )

        generations = self.store.list_session_generations(item.work_item_id)
        self.assertEqual(1, len(generations))
        self.assertEqual("active", generations[0].state.value)
        self.assertEqual(1, len(self.store.list_turns(item.work_item_id)))
        self.assertEqual(
            1,
            len([call for call in self.transport.calls if call.turn_id is not None]),
        )

    def test_v2_policy_change_rotates_to_new_digest(self) -> None:
        first_config = replace(
            make_config(global_max_active=4),
            session_runtime=session_runtime_config(),
        )
        first_service = OfflineSshDispatchService(
            config=first_config,
            store=self.store,
            orchestrator=self.orchestrator,
        )
        item = first_service.resolve_and_prepare(
            claimed_task(), base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        comments = (
            {"id": "C1", "author": "alice", "body": "/codex-context initial"},
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, blocked_result()),
        )
        first_service.run_claimed_turn(
            claimed_task(), comments=comments, turn_id="turn_" + "d" * 32
        )
        first_service.resolve_and_prepare(claimed_task(), base_sha=BASE_SHA)

        upgraded_service = OfflineSshDispatchService(
            config=replace(
                make_config(global_max_active=4),
                session_runtime=session_runtime_config(
                    agent_policy_digest=POLICY_DIGEST_2
                ),
            ),
            store=self.store,
            orchestrator=self.orchestrator,
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION_2, BASE_SHA, blocked_result()),
        )
        upgraded_service.run_claimed_turn(
            claimed_task(), comments=comments, turn_id="turn_" + "e" * 32
        )

        generations = self.store.list_session_generations(item.work_item_id)
        self.assertEqual(["retired", "active"], [item.state.value for item in generations])
        self.assertEqual(POLICY_DIGEST, generations[0].policy_sha256)
        self.assertEqual(POLICY_DIGEST_2, generations[1].policy_sha256)
        self.assertEqual("policy_changed", generations[1].rotation_reason)

    def test_v2_activation_rotates_imported_legacy_session(self) -> None:
        item = self.service.resolve_and_prepare(
            claimed_task(), base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, blocked_result()),
        )
        legacy_turn = self.service.run_claimed_turn(
            claimed_task(), turn_id="turn_" + "f" * 32
        )
        legacy_generation_id = "sg_" + "f" * 32
        now = "2026-08-13T02:00:00Z"
        with self.store._transaction() as connection:
            connection.execute(
                "INSERT INTO session_generations ("
                "session_generation_id, work_item_id, generation_number, state, role, "
                "codex_session_id, start_head_sha, policy_sha256, rotation_reason, "
                "created_at, started_at, updated_at"
                ") VALUES (?, ?, 1, 'active', 'implementation', ?, ?, NULL, "
                "'legacy_migration', ?, ?, ?)",
                (
                    legacy_generation_id,
                    item.work_item_id,
                    SESSION,
                    BASE_SHA,
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO turn_session_generations (turn_id, session_generation_id) "
                "VALUES (?, ?)",
                (legacy_turn.turn.turn_id, legacy_generation_id),
            )

        v2_service = OfflineSshDispatchService(
            config=replace(
                make_config(global_max_active=4),
                session_runtime=session_runtime_config(),
            ),
            store=self.store,
            orchestrator=self.orchestrator,
        )
        v2_service.resolve_and_prepare(claimed_task(), base_sha=BASE_SHA)
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION_2, BASE_SHA, blocked_result()),
        )
        v2_service.run_claimed_turn(
            claimed_task(), turn_id="turn_" + "0" * 32
        )

        generations = self.store.list_session_generations(item.work_item_id)
        self.assertEqual(["retired", "active"], [item.state.value for item in generations])
        self.assertIsNone(generations[0].policy_sha256)
        self.assertEqual(POLICY_DIGEST, generations[1].policy_sha256)
        self.assertEqual("legacy_policy_activation", generations[1].rotation_reason)

    def test_v2_interrupted_start_reconciles_without_a_second_start(self) -> None:
        config = replace(
            make_config(global_max_active=4),
            session_runtime=session_runtime_config(),
        )
        service = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=self.orchestrator,
        )
        item = service.resolve_and_prepare(
            claimed_task(), base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, blocked_result()),
        )
        self.transport.interrupt_next(RunnerOperation.START)

        interrupted = service.run_claimed_turn(
            claimed_task(), turn_id="turn_" + "7" * 32
        )
        self.assertEqual(TurnState.RECONCILING, interrupted.turn.state)
        generation = self.store.get_live_session_generation(item.work_item_id)
        self.assertIsNotNone(generation)
        assert generation is not None
        self.assertEqual("starting", generation.state.value)

        reconciled = self.orchestrator.reconcile_turn(interrupted.turn.turn_id)

        self.assertEqual(TurnState.BLOCKED, reconciled.turn.state)
        generation = self.store.get_live_session_generation(item.work_item_id)
        self.assertIsNotNone(generation)
        assert generation is not None
        self.assertEqual("active", generation.state.value)
        self.assertEqual(SESSION, generation.codex_session_id)
        self.assertEqual(
            [RunnerOperation.START, RunnerOperation.STATUS],
            [call.operation for call in self.transport.calls if call.turn_id],
        )

    def test_v2_publication_advances_work_item_and_generation_together(self) -> None:
        config = replace(
            make_config(global_max_active=4),
            session_runtime=session_runtime_config(max_turns_per_session=1),
        )
        service = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=self.orchestrator,
        )
        item = service.resolve_and_prepare(
            claimed_task(), base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        artifact = b"v2-checkpoint"
        head_sha = "b" * 40
        self.verifier.register(
            VerifiedBundle(
                bundle_sha256=sha256(artifact).hexdigest(),
                head_sha=head_sha,
                parent_anchor_sha=BASE_SHA,
                changed_paths=("src/codex_dispatcher/parser.py",),
                commit_count=1,
                size_bytes=len(artifact),
            )
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, head_sha, blocked_result(), artifact),
        )
        progress = service.run_claimed_turn(
            claimed_task(), turn_id="turn_" + "8" * 32
        )
        self.assertEqual(TurnState.CHECKPOINTING, progress.turn.state)

        completed = service.publish_checkpoint(
            progress.turn.turn_id,
            publisher=SimpleNamespace(
                publish=lambda artifact, *, plan, work_item: SimpleNamespace(
                    observed_remote_sha=plan.source_sha
                )
            ),
        )

        generation = self.store.get_turn_session_generation(progress.turn.turn_id)
        self.assertIsNotNone(generation)
        assert generation is not None
        self.assertEqual(head_sha, completed.work_item.last_published_sha)
        self.assertEqual(head_sha, generation.last_published_sha)
        self.assertEqual(TurnState.BLOCKED, completed.turn.state)
        checkpoints = self.store.list_work_item_publication_checkpoints(
            item.work_item_id
        )
        self.assertEqual(1, len(checkpoints))
        self.assertEqual(
            ("src/codex_dispatcher/parser.py",), checkpoints[0].changed_paths
        )

        captured: list[dict[str, object]] = []

        def import_for_head(**kwargs: object) -> ActionsEvidenceSnapshot:
            captured.append(dict(kwargs))
            run = ActionsRunEvidence(
                name="tests",
                workflow_id=200,
                run_id=100,
                run_attempt=1,
                repository="owner/repo",
                head_repository="owner/repo",
                head_branch=item.task_branch,
                head_sha=head_sha,
                event="pull_request",
                status="completed",
                conclusion="success",
                created_at="2026-08-22T08:00:00Z",
                updated_at="2026-08-22T08:01:00Z",
                html_url="https://github.com/owner/repo/actions/runs/100",
            )
            return ActionsEvidenceSnapshot(
                repository="owner/repo",
                task_branch=item.task_branch,
                head_sha=head_sha,
                remote_ref_sha=head_sha,
                observed_at=str(kwargs["observed_at"]),
                required_checks=(
                    RequiredCheckEvidence("tests", run.check_status, run),
                ),
            )

        service.resolve_and_prepare(claimed_task(), base_sha=BASE_SHA)
        evidence_service = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=self.orchestrator,
            ci_evidence_importer=SimpleNamespace(import_for_head=import_for_head),
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION_2, head_sha, blocked_result()),
        )
        evidence_service.run_claimed_turn(
            claimed_task(), turn_id="turn_" + "9" * 32
        )

        self.assertEqual(1, len(captured))
        self.assertEqual(head_sha, captured[0]["head_sha"])
        replacement = self.store.list_session_generations(item.work_item_id)[1]
        handoff = self.store.get_session_handoff_for_generation(
            replacement.session_generation_id
        )
        self.assertIsNotNone(handoff)
        assert handoff is not None
        self.assertEqual(
            [{"name": "tests", "status": "passed"}],
            handoff.trusted_facts["required_checks"],
        )
        self.assertEqual(
            "github_actions", handoff.trusted_facts["ci_evidence"]["provider"]
        )
        self.assertTrue(
            handoff.trusted_facts["git"]["publication_evidence_complete"]
        )
        self.assertEqual(
            ["src/codex_dispatcher/parser.py"],
            handoff.trusted_facts["git"]["verified_changed_paths"],
        )

        def reject_import(**_: object) -> ActionsEvidenceSnapshot:
            raise CiEvidenceError("provider denied the exact-head read")

        evidence_service.resolve_and_prepare(claimed_task(), base_sha=BASE_SHA)
        failing_service = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=self.orchestrator,
            ci_evidence_importer=SimpleNamespace(import_for_head=reject_import),
        )
        with self.assertRaisesRegex(
            SshDispatchPlanningError, "Actions evidence import failed"
        ):
            failing_service.run_claimed_turn(
                claimed_task(), turn_id="turn_" + "a" * 32
            )
        generations = self.store.list_session_generations(item.work_item_id)
        self.assertEqual(2, len(generations))
        self.assertEqual(
            ["retired", "active"], [item.state.value for item in generations]
        )
        self.assertEqual(2, len(self.store.list_turns(item.work_item_id)))

    def test_missing_or_conflicting_source_bundle_fails_before_persistence(self) -> None:
        for bundle in (None, source_bundle("b" * 40)):
            with self.subTest(bundle=bundle):
                with self.assertRaises(SshDispatchPlanningError):
                    self.service.resolve_and_prepare(
                        claimed_task(),
                        base_sha=BASE_SHA,
                        source_bundle=bundle,
                    )
                self.assertIsNone(
                    self.store.get_work_item_by_issue("owner/repo", 42)
                )
                self.assertEqual([], self.transport.calls)

    def test_candidate_plan_observes_the_database_global_turn_lock(self) -> None:
        item = self.service.resolve_and_prepare(
            claimed_task(), base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        tracker = FakeTracker()
        tracker.ready_tasks = (
            replace(
                claimed_task(43),
                state=TaskState.READY,
                labels=("agent:ready", "exec:ssh-cli"),
            ),
        )
        available = self.service.plan_candidates(tracker)
        self.assertEqual([43], [task.issue_number for task in available.selected])

        self.store.plan_turn(
            item.work_item_id,
            issue_revision="revision-active",
            prompt_sha256="d" * 64,
            input_head_sha=BASE_SHA,
        )
        blocked = self.service.plan_candidates(tracker)
        self.assertEqual((), blocked.selected)
        self.assertEqual("global_capacity", blocked.rejected[0].code)

    def test_identity_conflict_does_not_reactivate_or_call_runner(self) -> None:
        item = self.service.resolve_and_prepare(
            claimed_task(), base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        self.store.update_work_item_state(item.work_item_id, WorkItemState.BLOCKED)
        calls_before = tuple(self.transport.calls)

        with self.assertRaises(SshDispatchPlanningError):
            self.service.resolve_and_prepare(
                replace(claimed_task(), issue_node_id="I_kwDOReplacement42"),
                base_sha=BASE_SHA,
            )

        persisted = self.store.get_work_item(item.work_item_id)
        self.assertEqual(WorkItemState.BLOCKED, persisted.state)
        self.assertEqual(calls_before, tuple(self.transport.calls))


if __name__ == "__main__":
    unittest.main()
