from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_dispatcher.ci_evidence import (
    ActionsEvidenceSnapshot,
    ActionsRunEvidence,
    CiEvidenceError,
    RequiredCheckEvidence,
)
from codex_dispatcher.codex_jsonl import CodexTurnUsage
from codex_dispatcher.completion_gate import repairable_ci_failures
from codex_dispatcher.config import SessionRuntimeConfig
from codex_dispatcher.publisher import VerifiedBundle
from codex_dispatcher.runner_protocol import RunnerOperation, parse_agent_result
from codex_dispatcher.source_bundle import SourceBundle
from codex_dispatcher.ssh_dispatch_planning import SshDispatchPlanningError
from codex_dispatcher.ssh_recovery import SshRecoveryAction, plan_ssh_recovery
from codex_dispatcher.ssh_dispatch_service import (
    AutonomyBudgetError,
    OfflineSshDispatchService,
)
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
from codex_dispatcher.work_item_lifecycle import WorkItemArchiveStatus
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
                "schema_version": 2,
                "status": "blocked",
                "summary": "No repository changes were required",
                "acceptance": [],
                "remaining_work": [],
                "needs_input": [],
                "tests": [{"name": "inspection", "status": "passed"}],
                "changed_paths": [],
                "blocker_code": "fixture_blocked",
                "next_step": "Record the blocker in the Issue",
            }
        )
    )


def completed_result(
    extra_acceptance: tuple[dict[str, str], ...] = (),
):
    return parse_agent_result(
        json.dumps(
            {
                "schema_version": 2,
                "status": "completed",
                "summary": "Completion candidate",
                "acceptance": [
                    {"id": "AC-1", "status": "passed", "evidence": "tests passed"},
                    {"id": "AC-2", "status": "passed", "evidence": "paths verified"},
                    {"id": "AC-3", "status": "passed", "evidence": "head published"},
                    *extra_acceptance,
                ],
                "remaining_work": [],
                "needs_input": [],
                "tests": [{"name": "tests", "status": "passed"}],
                "changed_paths": ["src/codex_dispatcher/parser.py"],
                "blocker_code": None,
                "next_step": "Wait for trusted completion evidence",
            }
        )
    )


def checkpoint_result():
    return parse_agent_result(
        json.dumps(
            {
                "schema_version": 2,
                "status": "checkpoint",
                "summary": "A bounded implementation step completed",
                "acceptance": [
                    {"id": "AC-1", "status": "not_verified", "evidence": "CI pending"},
                    {"id": "AC-2", "status": "passed", "evidence": "paths inspected"},
                    {"id": "AC-3", "status": "not_verified", "evidence": "publish pending"},
                ],
                "remaining_work": ["Complete the remaining implementation"],
                "needs_input": [],
                "tests": [{"name": "focused tests", "status": "passed"}],
                "changed_paths": [],
                "blocker_code": None,
                "next_step": "Continue autonomously",
            }
        )
    )


def audit_completed_result(
    extra_acceptance: tuple[dict[str, str], ...] = (),
):
    result = json.loads(json.dumps({
        "schema_version": 2,
        "status": "completed",
        "summary": "Fresh read-only audit passed",
        "acceptance": [
            {"id": "AC-1", "status": "passed", "evidence": "exact Actions evidence"},
            {"id": "AC-2", "status": "passed", "evidence": "publication ledger"},
            {"id": "AC-3", "status": "passed", "evidence": "exact HEAD receipt"},
            *extra_acceptance,
        ],
        "remaining_work": [],
        "needs_input": [],
        "tests": [{"name": "audit verification", "status": "passed"}],
        "changed_paths": [],
        "blocker_code": None,
        "next_step": "Review the audited candidate",
    }))
    return parse_agent_result(json.dumps(result))


def structured_claimed_task():
    return replace(
        claimed_task(),
        body=claimed_task().body.replace(
            "- [ ] 测试通过",
            "- [AC-1] required-check: tests\n"
            "- [AC-2] changed-paths-within-allowed\n"
            "- [AC-3] task-head-published",
        ),
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
        "max_repair_cycles": 3,
        "max_audit_cycles": 3,
        "max_total_tokens": 1_000_000,
        "max_work_item_age_seconds": 604_800,
    }
    values.update(overrides)
    return SessionRuntimeConfig(**values)  # type: ignore[arg-type]


class OfflineSshDispatchServiceTests(unittest.TestCase):
    def test_no_progress_budget_exhaustion_is_durable(self) -> None:
        config = replace(
            make_config(global_max_active=4),
            session_runtime=session_runtime_config(max_no_progress_turns=1),
        )
        service = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=self.orchestrator,
        )
        task = structured_claimed_task()
        item = service.resolve_and_prepare(
            task, base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        source_turn_id = "turn_" + "8" * 32
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, checkpoint_result()),
        )
        service.run_claimed_turn(task, turn_id=source_turn_id)
        service.plan_checkpoint_followup(task, source_turn_id)
        running_task = replace(
            task,
            state=TaskState.RUNNING,
            labels=("agent:running", "exec:ssh-cli", "priority:p1"),
        )

        with self.assertRaisesRegex(
            AutonomyBudgetError, "no_progress_budget_exhausted"
        ):
            service.run_claimed_turn(
                running_task, turn_id="turn_" + "9" * 32
            )

        intent = self.store.get_turn_followup_intent(source_turn_id)
        blocked = self.store.get_work_item(item.work_item_id)
        self.assertIsNotNone(intent)
        self.assertIsNotNone(blocked)
        assert intent is not None and blocked is not None
        self.assertEqual("exhausted", intent.state.value)
        self.assertIs(WorkItemState.BLOCKED, blocked.state)
        self.assertEqual(2, len(self.transport.calls))  # PREPARE + first START only

    def test_ci_failure_rotates_to_repair_then_requires_a_fresh_audit(self) -> None:
        config = replace(
            make_config(global_max_active=4),
            session_runtime=session_runtime_config(
                rotate_before_final_audit=True,
                max_session_generations=6,
            ),
        )
        ci_state = {"conclusion": "failure", "run_id": 401}

        def import_for_head(**kwargs: object) -> ActionsEvidenceSnapshot:
            run = ActionsRunEvidence(
                name="tests",
                workflow_id=301,
                run_id=int(ci_state["run_id"]),
                run_attempt=1,
                repository="owner/repo",
                head_repository="owner/repo",
                head_branch=str(kwargs["task_branch"]),
                head_sha=str(kwargs["head_sha"]),
                event="pull_request",
                status="completed",
                conclusion=str(ci_state["conclusion"]),
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:01:00Z",
                html_url=(
                    "https://github.com/owner/repo/actions/runs/"
                    + str(ci_state["run_id"])
                ),
            )
            return ActionsEvidenceSnapshot(
                repository="owner/repo",
                task_branch=str(kwargs["task_branch"]),
                head_sha=str(kwargs["head_sha"]),
                remote_ref_sha=str(kwargs["head_sha"]),
                observed_at=str(kwargs["observed_at"]),
                required_checks=(
                    RequiredCheckEvidence("tests", run.check_status, run),
                ),
            )

        service = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=self.orchestrator,
            ci_evidence_importer=SimpleNamespace(import_for_head=import_for_head),
        )
        task = structured_claimed_task()
        item = service.resolve_and_prepare(
            task, base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        first_head = "4" * 40
        first_artifact = b"initial-completion"
        self.verifier.register(
            VerifiedBundle(
                bundle_sha256=sha256(first_artifact).hexdigest(),
                head_sha=first_head,
                parent_anchor_sha=BASE_SHA,
                changed_paths=("src/codex_dispatcher/parser.py",),
                commit_count=1,
                size_bytes=len(first_artifact),
            )
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, first_head, completed_result(), first_artifact),
        )
        candidate = service.run_claimed_turn(task, turn_id="turn_" + "3" * 32)
        candidate = service.publish_checkpoint(
            candidate.turn.turn_id,
            publisher=SimpleNamespace(
                publish=lambda artifact, *, plan, work_item: SimpleNamespace(
                    observed_remote_sha=plan.source_sha
                )
            ),
        )
        failed = service.evaluate_completion_gate(task, candidate.turn.turn_id)

        self.assertEqual(WorkItemState.READY, failed.work_item.state)
        self.assertEqual("completion_gate_repair_planned", failed.turn.error_code)
        intent = self.store.get_planned_work_item_followup(item.work_item_id)
        self.assertIsNotNone(intent)
        assert intent is not None
        self.assertEqual("ci_failure", intent.cause.value)
        self.assertEqual("failure", intent.context["payload"]["failed_checks"][0]["conclusion"])
        failed_gate = self.store.get_turn_completion_gate(candidate.turn.turn_id)
        self.assertIsNotNone(failed_gate)
        assert failed_gate is not None
        for offset, conclusion in enumerate(("cancelled", "timed_out"), start=1):
            with self.subTest(nonrepairable_conclusion=conclusion):
                run_id = 410 + offset
                nonrepairable_run = ActionsRunEvidence(
                    name="tests",
                    workflow_id=301,
                    run_id=run_id,
                    run_attempt=1,
                    repository="owner/repo",
                    head_repository="owner/repo",
                    head_branch=item.task_branch,
                    head_sha=first_head,
                    event="pull_request",
                    status="completed",
                    conclusion=conclusion,
                    created_at="2026-01-01T00:00:00Z",
                    updated_at="2026-01-01T00:01:00Z",
                    html_url=f"https://github.com/owner/repo/actions/runs/{run_id}",
                )
                nonrepairable = ActionsEvidenceSnapshot(
                    repository="owner/repo",
                    task_branch=item.task_branch,
                    head_sha=first_head,
                    remote_ref_sha=first_head,
                    observed_at="2026-01-01T00:02:00Z",
                    required_checks=(
                        RequiredCheckEvidence(
                            "tests", nonrepairable_run.check_status, nonrepairable_run
                        ),
                    ),
                )
                self.assertEqual(
                    (), repairable_ci_failures(failed_gate, nonrepairable)
                )

        running_task = replace(
            task,
            state=TaskState.RUNNING,
            labels=("agent:running", "exec:ssh-cli", "priority:p1"),
        )
        repair_head = "5" * 40
        repair_artifact = b"ci-repair"
        self.verifier.register(
            VerifiedBundle(
                bundle_sha256=sha256(repair_artifact).hexdigest(),
                head_sha=repair_head,
                parent_anchor_sha=first_head,
                changed_paths=("src/codex_dispatcher/parser.py",),
                commit_count=1,
                size_bytes=len(repair_artifact),
            )
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION_2, repair_head, completed_result(), repair_artifact),
        )
        repair = service.run_claimed_turn(
            running_task, turn_id="turn_" + "4" * 32
        )
        self.assertEqual("ci_repair", self.transport.calls[-1].session_role)
        repair = service.publish_checkpoint(
            repair.turn.turn_id,
            publisher=SimpleNamespace(
                publish=lambda artifact, *, plan, work_item: SimpleNamespace(
                    observed_remote_sha=plan.source_sha
                )
            ),
        )
        ci_state.update(conclusion="success", run_id=402)
        repaired = service.evaluate_completion_gate(running_task, repair.turn.turn_id)
        self.assertTrue(service.requires_fresh_final_audit(repaired.turn.turn_id))
        crash_recovery_tracker = FakeTracker()
        crash_recovery_tracker.tasks[task.task_id] = running_task
        crash_recovery = plan_ssh_recovery(
            config, self.store, crash_recovery_tracker
        )
        self.assertEqual(
            SshRecoveryAction.START_FRESH_FINAL_AUDIT,
            crash_recovery.action,
        )
        self.assertEqual(repair.turn.turn_id, crash_recovery.turn.turn_id)

        session_3 = "323e4567-e89b-12d3-a456-426614174000"
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(session_3, repair_head, checkpoint_result()),
        )
        audit_gap = service.run_fresh_final_audit(
            running_task, turn_id="turn_" + "5" * 32
        )
        self.assertEqual("audit", self.transport.calls[-1].session_role)
        planned_gap = service.plan_checkpoint_followup(
            running_task, audit_gap.turn.turn_id
        )
        self.assertEqual(WorkItemState.READY, planned_gap.work_item.state)
        gap_intent = self.store.get_planned_work_item_followup(item.work_item_id)
        self.assertIsNotNone(gap_intent)
        assert gap_intent is not None
        self.assertEqual("audit_gap", gap_intent.cause.value)
        self.assertEqual("ci_repair", gap_intent.target_role.value)

        second_repair_head = "6" * 40
        second_repair_artifact = b"audit-gap-repair"
        self.verifier.register(
            VerifiedBundle(
                bundle_sha256=sha256(second_repair_artifact).hexdigest(),
                head_sha=second_repair_head,
                parent_anchor_sha=repair_head,
                changed_paths=("src/codex_dispatcher/parser.py",),
                commit_count=1,
                size_bytes=len(second_repair_artifact),
            )
        )
        session_4 = "423e4567-e89b-12d3-a456-426614174000"
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(
                session_4,
                second_repair_head,
                completed_result(),
                second_repair_artifact,
            ),
        )
        second_repair = service.run_claimed_turn(
            running_task, turn_id="turn_" + "6" * 32
        )
        second_repair = service.publish_checkpoint(
            second_repair.turn.turn_id,
            publisher=SimpleNamespace(
                publish=lambda artifact, *, plan, work_item: SimpleNamespace(
                    observed_remote_sha=plan.source_sha
                )
            ),
        )
        ci_state.update(conclusion="success", run_id=403)
        second_repaired = service.evaluate_completion_gate(
            running_task, second_repair.turn.turn_id
        )
        self.assertTrue(
            service.requires_fresh_final_audit(second_repaired.turn.turn_id)
        )

        session_5 = "523e4567-e89b-12d3-a456-426614174000"
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(
                session_5, second_repair_head, audit_completed_result()
            ),
        )
        final_audit = service.run_fresh_final_audit(
            running_task, turn_id="turn_" + "7" * 32
        )
        self.assertEqual("audit", self.transport.calls[-1].session_role)
        final = service.evaluate_completion_gate(
            running_task, final_audit.turn.turn_id
        )
        self.assertEqual(TurnState.FINISHED, final.turn.state)
        self.assertEqual(WorkItemState.REVIEW, final.work_item.state)
        self.assertFalse(service.requires_fresh_final_audit(final.turn.turn_id))

    def test_checkpoint_intent_is_durable_recoverable_and_atomically_consumed(self) -> None:
        config = replace(
            make_config(global_max_active=4),
            session_runtime=session_runtime_config(),
        )
        service = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=self.orchestrator,
        )
        task = structured_claimed_task()
        item = service.resolve_and_prepare(
            task, base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        source_turn_id = "turn_" + "1" * 32
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, checkpoint_result()),
        )

        checkpoint = service.run_claimed_turn(task, turn_id=source_turn_id)
        planned = service.plan_checkpoint_followup(task, source_turn_id)

        self.assertEqual(TurnState.PUBLISHED, checkpoint.turn.state)
        self.assertEqual(TurnState.FINISHED, planned.turn.state)
        self.assertEqual(WorkItemState.READY, planned.work_item.state)
        intent = self.store.get_planned_work_item_followup(item.work_item_id)
        self.assertIsNotNone(intent)
        assert intent is not None
        tracker = FakeTracker()
        running_task = replace(
            task,
            state=TaskState.RUNNING,
            labels=("agent:running", "exec:ssh-cli", "priority:p1"),
        )
        tracker.tasks[task.task_id] = running_task
        recovery = plan_ssh_recovery(config, self.store, tracker)
        self.assertIs(SshRecoveryAction.START_AUTONOMOUS_TURN, recovery.action)

        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, blocked_result()),
        )
        continued = service.run_claimed_turn(
            running_task, turn_id="turn_" + "2" * 32
        )

        consumed = self.store.get_turn_followup_intent(source_turn_id)
        self.assertIsNotNone(consumed)
        assert consumed is not None
        self.assertEqual("started", consumed.state.value)
        self.assertEqual(continued.turn.turn_id, consumed.target_turn_id)
        self.assertIs(RunnerOperation.RESUME, self.transport.calls[-1].operation)
        self.assertEqual("implementation", self.transport.calls[-1].session_role)

    def test_missing_completed_state_requires_runner_bound_absence_receipt(self) -> None:
        work_item = self.service.resolve_and_prepare(
            claimed_task(),
            base_sha=BASE_SHA,
            source_bundle=source_bundle(),
        )
        head_sha = "9" * 40
        self.store.record_published_sha(
            work_item.work_item_id,
            previous_sha=BASE_SHA,
            head_sha=head_sha,
        )
        self.transport.set_head(work_item.work_item_id, head_sha)
        self.store.update_work_item_state(work_item.work_item_id, WorkItemState.RUNNING)
        self.store.update_work_item_state(work_item.work_item_id, WorkItemState.REVIEW)
        self.store.update_work_item_state(
            work_item.work_item_id,
            WorkItemState.COMPLETED,
            updated_at="2026-02-01T00:00:00+00:00",
        )
        self.transport.forget_work_item_for_fixture(work_item.work_item_id)

        receipt = self.service.reconcile_completed_work_item_absence(
            work_item.work_item_id,
            eligible_at="2026-02-01T00:00:00+00:00",
        )
        self.assertEqual("runner_protocol_v2", receipt.observed_by)
        self.assertEqual(head_sha, receipt.expected_head_sha)
        archive = self.store.get_work_item_archive(work_item.work_item_id)
        self.assertIsNotNone(archive)
        assert archive is not None
        self.assertIs(WorkItemArchiveStatus.PREPARED, archive.status)
        self.assertEqual(
            RunnerOperation.PROVE_ABSENCE,
            self.transport.calls[-1].operation,
        )
        call_count = len(self.transport.calls)
        self.assertEqual(
            receipt,
            self.service.reconcile_completed_work_item_absence(
                work_item.work_item_id,
                eligible_at="2026-02-01T00:00:00+00:00",
            ),
        )
        self.assertEqual(call_count, len(self.transport.calls))

    def test_interrupted_absence_receipt_retries_exact_runner_tombstone(self) -> None:
        work_item = self.service.resolve_and_prepare(
            claimed_task(), base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        head_sha = "8" * 40
        self.store.record_published_sha(
            work_item.work_item_id, previous_sha=BASE_SHA, head_sha=head_sha
        )
        self.transport.set_head(work_item.work_item_id, head_sha)
        self.store.update_work_item_state(work_item.work_item_id, WorkItemState.RUNNING)
        self.store.update_work_item_state(work_item.work_item_id, WorkItemState.REVIEW)
        self.store.update_work_item_state(
            work_item.work_item_id,
            WorkItemState.COMPLETED,
            updated_at="2026-02-01T00:00:00+00:00",
        )
        self.transport.forget_work_item_for_fixture(work_item.work_item_id)
        self.transport.interrupt_next(RunnerOperation.PROVE_ABSENCE)

        with self.assertRaisesRegex(RuntimeError, "retry the exact request"):
            self.service.reconcile_completed_work_item_absence(
                work_item.work_item_id,
                eligible_at="2026-02-01T00:00:00+00:00",
            )
        self.assertIsNone(
            self.store.get_work_item_absence_reconciliation(work_item.work_item_id)
        )
        receipt = self.service.reconcile_completed_work_item_absence(
            work_item.work_item_id,
            eligible_at="2026-02-01T00:00:00+00:00",
        )
        self.assertEqual("runner_protocol_v2", receipt.observed_by)
        self.assertEqual(
            [RunnerOperation.PROVE_ABSENCE, RunnerOperation.PROVE_ABSENCE],
            [call.operation for call in self.transport.calls[-2:]],
        )

    def test_archive_lost_receipt_reconciles_with_status_before_retry(self) -> None:
        task = claimed_task()
        work_item = self.service.resolve_and_prepare(
            task,
            base_sha=BASE_SHA,
            source_bundle=source_bundle(),
        )
        head_sha = "f" * 40
        self.store.record_published_sha(
            work_item.work_item_id,
            previous_sha=BASE_SHA,
            head_sha=head_sha,
        )
        # The in-memory fake models the already-published Runner checkpoint.
        self.transport.set_head(work_item.work_item_id, head_sha)
        self.store.update_work_item_state(
            work_item.work_item_id, WorkItemState.RUNNING
        )
        self.store.update_work_item_state(
            work_item.work_item_id, WorkItemState.REVIEW
        )
        self.store.update_work_item_state(
            work_item.work_item_id,
            WorkItemState.COMPLETED,
            updated_at="2026-02-01T00:00:00+00:00",
        )
        self.transport.interrupt_next(RunnerOperation.ARCHIVE)

        original_invoke = self.transport.invoke

        def invoke_after_durable_ambiguity(*args: object, **kwargs: object):
            persisted = self.store.get_work_item_archive(work_item.work_item_id)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertIs(WorkItemArchiveStatus.AMBIGUOUS, persisted.status)
            return original_invoke(*args, **kwargs)

        with patch.object(self.transport, "invoke", invoke_after_durable_ambiguity):
            ambiguous = self.service.archive_completed_work_item(
                work_item.work_item_id,
                eligible_at="2026-02-08T00:00:00+00:00",
            )
        self.assertIs(WorkItemArchiveStatus.AMBIGUOUS, ambiguous.status)
        self.assertEqual(RunnerOperation.ARCHIVE, self.transport.calls[-1].operation)

        self.transport.reject_next(RunnerOperation.ARCHIVE_STATUS)
        still_ambiguous = self.service.reconcile_work_item_archive(
            work_item.work_item_id
        )
        self.assertIs(WorkItemArchiveStatus.AMBIGUOUS, still_ambiguous.status)

        archived = self.service.reconcile_work_item_archive(work_item.work_item_id)
        self.assertIs(WorkItemArchiveStatus.ARCHIVED, archived.status)
        self.assertEqual(
            [
                RunnerOperation.ARCHIVE,
                RunnerOperation.ARCHIVE_STATUS,
                RunnerOperation.ARCHIVE_STATUS,
            ],
            [call.operation for call in self.transport.calls[-3:]],
        )

    def test_archive_rejection_requires_status_before_retry(self) -> None:
        work_item = self.service.resolve_and_prepare(
            claimed_task(),
            base_sha=BASE_SHA,
            source_bundle=source_bundle(),
        )
        head_sha = "e" * 40
        self.store.record_published_sha(
            work_item.work_item_id,
            previous_sha=BASE_SHA,
            head_sha=head_sha,
        )
        self.transport.set_head(work_item.work_item_id, head_sha)
        self.store.update_work_item_state(
            work_item.work_item_id, WorkItemState.RUNNING
        )
        self.store.update_work_item_state(
            work_item.work_item_id, WorkItemState.REVIEW
        )
        self.store.update_work_item_state(
            work_item.work_item_id,
            WorkItemState.COMPLETED,
            updated_at="2026-02-01T00:00:00+00:00",
        )
        self.transport.reject_next(RunnerOperation.ARCHIVE)

        ambiguous = self.service.archive_completed_work_item(
            work_item.work_item_id,
            eligible_at="2026-02-08T00:00:00+00:00",
        )
        retry_ready = self.service.reconcile_work_item_archive(
            work_item.work_item_id
        )
        archived = self.service.archive_completed_work_item(
            work_item.work_item_id,
            eligible_at="2026-02-08T00:00:00+00:00",
        )

        self.assertIs(WorkItemArchiveStatus.AMBIGUOUS, ambiguous.status)
        self.assertIs(WorkItemArchiveStatus.PREPARED, retry_ready.status)
        self.assertIs(WorkItemArchiveStatus.ARCHIVED, archived.status)
        self.assertEqual(
            [
                RunnerOperation.ARCHIVE,
                RunnerOperation.ARCHIVE_STATUS,
                RunnerOperation.ARCHIVE,
            ],
            [call.operation for call in self.transport.calls[-3:]],
        )

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

    def test_rejected_prepare_reactivation_retries_prepare_before_start(self) -> None:
        self.transport.reject_next(RunnerOperation.PREPARE)

        rejected = self.service.resolve_and_prepare(
            claimed_task(), base_sha=BASE_SHA, source_bundle=source_bundle()
        )

        self.assertEqual(WorkItemState.BLOCKED, rejected.state)
        self.assertFalse(
            self.store.runner_preparation_was_acknowledged(rejected.work_item_id)
        )
        self.assertEqual((), self.store.list_turns(rejected.work_item_id))

        retried = self.service.resolve_and_prepare(
            claimed_task(), base_sha="b" * 40, source_bundle=source_bundle()
        )

        self.assertEqual(WorkItemState.READY, retried.state)
        self.assertTrue(
            self.store.runner_preparation_was_acknowledged(retried.work_item_id)
        )
        self.transport.queue_turn(
            retried.work_item_id,
            FakeTurnFixture(SESSION, BASE_SHA, blocked_result()),
        )
        progress = self.service.run_claimed_turn(
            claimed_task(), turn_id="turn_" + "f" * 32
        )
        self.assertEqual(TurnState.BLOCKED, progress.turn.state)
        self.assertEqual(
            [
                RunnerOperation.PREPARE,
                RunnerOperation.PREPARE,
                RunnerOperation.START,
            ],
            [call.operation for call in self.transport.calls],
        )

    def test_v2_pre_session_rejection_reuses_an_orphan_planned_generation(self) -> None:
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
        self.transport.reject_next(RunnerOperation.START)

        rejected = service.run_claimed_turn(
            claimed_task(), turn_id="turn_" + "a" * 32
        )

        self.assertEqual(TurnState.BLOCKED, rejected.turn.state)
        first_generation = self.store.list_session_generations(item.work_item_id)[0]
        self.assertEqual("failed", first_generation.state.value)
        self.assertIsNone(first_generation.codex_session_id)
        service.resolve_and_prepare(claimed_task(), base_sha=BASE_SHA)
        orphan = self.store.plan_session_generation(
            item.work_item_id,
            role=first_generation.role,
            policy_sha256=POLICY_DIGEST,
        )
        self.assertEqual(2, orphan.generation_number)
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION_2, BASE_SHA, blocked_result()),
        )

        retried = service.run_claimed_turn(
            replace(claimed_task(), updated_at="2026-08-13T03:00:00Z"),
            turn_id="turn_" + "b" * 32,
        )

        generations = self.store.list_session_generations(item.work_item_id)
        self.assertEqual(TurnState.BLOCKED, retried.turn.state)
        self.assertEqual("active", generations[1].state.value)
        self.assertEqual(SESSION_2, generations[1].codex_session_id)
        self.assertIsNone(
            self.store.get_session_handoff_for_generation(
                generations[1].session_generation_id
            )
        )
        self.assertEqual(
            ["full", "full"],
            [
                prompt.prompt_kind.value
                for prompt in self.store.list_session_generation_turn_prompt_inputs(
                    first_generation.session_generation_id
                )
                + self.store.list_session_generation_turn_prompt_inputs(
                    generations[1].session_generation_id
                )
            ],
        )
        self.assertEqual(
            [RunnerOperation.PREPARE, RunnerOperation.START, RunnerOperation.START],
            [call.operation for call in self.transport.calls],
        )

    def test_v2_non_rejection_failed_generation_requires_explicit_recovery(self) -> None:
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
        self.transport.reject_next(RunnerOperation.START)
        rejected = service.run_claimed_turn(
            claimed_task(), turn_id="turn_" + "c" * 32
        )
        self.store._connection.execute(
            "UPDATE turns SET error_code = 'runner_unexpected_artifact' WHERE turn_id = ?",
            (rejected.turn.turn_id,),
        )
        self.store._connection.commit()
        service.resolve_and_prepare(claimed_task(), base_sha=BASE_SHA)

        with self.assertRaisesRegex(
            SshDispatchPlanningError, "requires explicit recovery"
        ):
            service.run_claimed_turn(
                replace(claimed_task(), updated_at="2026-08-13T03:00:00Z"),
                turn_id="turn_" + "d" * 32,
            )

        self.assertEqual(1, len(self.store.list_session_generations(item.work_item_id)))

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

    def test_v2_completion_waits_for_exact_head_ci_then_atomically_enters_review(
        self,
    ) -> None:
        config = replace(
            make_config(global_max_active=4),
            session_runtime=session_runtime_config(),
        )
        state = {"status": "queued", "conclusion": None}
        captured: list[dict[str, object]] = []

        def import_for_head(**kwargs: object) -> ActionsEvidenceSnapshot:
            captured.append(dict(kwargs))
            run = ActionsRunEvidence(
                name="tests",
                workflow_id=201,
                run_id=101,
                run_attempt=1,
                repository="owner/repo",
                head_repository="owner/repo",
                head_branch=str(kwargs["task_branch"]),
                head_sha=str(kwargs["head_sha"]),
                event="pull_request",
                status=str(state["status"]),
                conclusion=state["conclusion"],
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:01:00Z",
                html_url="https://github.com/owner/repo/actions/runs/101",
            )
            return ActionsEvidenceSnapshot(
                repository="owner/repo",
                task_branch=str(kwargs["task_branch"]),
                head_sha=str(kwargs["head_sha"]),
                remote_ref_sha=str(kwargs["head_sha"]),
                observed_at=str(kwargs["observed_at"]),
                required_checks=(
                    RequiredCheckEvidence("tests", run.check_status, run),
                ),
            )

        service = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=self.orchestrator,
            ci_evidence_importer=SimpleNamespace(import_for_head=import_for_head),
        )
        task = structured_claimed_task()
        item = service.resolve_and_prepare(
            task, base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        artifact = b"completion-gate-checkpoint"
        head_sha = "c" * 40
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
            FakeTurnFixture(SESSION, head_sha, completed_result(), artifact),
        )
        progress = service.run_claimed_turn(
            task, turn_id="turn_" + "c" * 32
        )
        progress = service.publish_checkpoint(
            progress.turn.turn_id,
            publisher=SimpleNamespace(
                publish=lambda artifact, *, plan, work_item: SimpleNamespace(
                    observed_remote_sha=plan.source_sha
                )
            ),
        )
        self.assertEqual(TurnState.PUBLISHED, progress.turn.state)
        self.assertEqual(WorkItemState.RUNNING, progress.work_item.state)

        pending = service.evaluate_completion_gate(task, progress.turn.turn_id)

        self.assertEqual(TurnState.PUBLISHED, pending.turn.state)
        self.assertEqual(WorkItemState.RUNNING, pending.work_item.state)
        first_gate = self.store.get_turn_completion_gate(progress.turn.turn_id)
        self.assertIsNotNone(first_gate)
        assert first_gate is not None
        self.assertEqual("pending", first_gate.status.value)

        state.update(status="completed", conclusion="success")
        passed = service.evaluate_completion_gate(task, progress.turn.turn_id)

        self.assertEqual(TurnState.FINISHED, passed.turn.state)
        self.assertEqual(WorkItemState.REVIEW, passed.work_item.state)
        final_gate = self.store.get_turn_completion_gate(progress.turn.turn_id)
        self.assertIsNotNone(final_gate)
        assert final_gate is not None
        self.assertEqual("passed", final_gate.status.value)
        self.assertEqual(head_sha, final_gate.head_sha)
        self.assertEqual(2, len(captured))
        self.assertTrue(final_gate.evidence["git"]["publication_evidence_complete"])
        self.assertEqual("passed", final_gate.evidence["acceptance"]["status"])

    def test_v2_completion_blocks_when_exact_head_ci_is_ambiguous(self) -> None:
        config = replace(
            make_config(global_max_active=4),
            session_runtime=session_runtime_config(),
        )

        def reject_import(**_: object) -> ActionsEvidenceSnapshot:
            raise CiEvidenceError("remote ref changed during observation")

        service = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=self.orchestrator,
            ci_evidence_importer=SimpleNamespace(import_for_head=reject_import),
        )
        task = structured_claimed_task()
        item = service.resolve_and_prepare(
            task, base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        artifact = b"ambiguous-completion-checkpoint"
        head_sha = "d" * 40
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
            FakeTurnFixture(SESSION, head_sha, completed_result(), artifact),
        )
        progress = service.run_claimed_turn(
            task, turn_id="turn_" + "d" * 32
        )
        progress = service.publish_checkpoint(
            progress.turn.turn_id,
            publisher=SimpleNamespace(
                publish=lambda artifact, *, plan, work_item: SimpleNamespace(
                    observed_remote_sha=plan.source_sha
                )
            ),
        )

        blocked = service.evaluate_completion_gate(task, progress.turn.turn_id)

        self.assertEqual(TurnState.BLOCKED, blocked.turn.state)
        self.assertEqual(
            "completion_ci_evidence_ambiguous", blocked.turn.error_code
        )
        self.assertEqual(WorkItemState.BLOCKED, blocked.work_item.state)

    def test_passed_implementation_rotates_to_fresh_audit_before_review(self) -> None:
        config = replace(
            make_config(global_max_active=4),
            session_runtime=session_runtime_config(
                rotate_before_final_audit=True
            ),
        )

        def import_for_head(**kwargs: object) -> ActionsEvidenceSnapshot:
            run = ActionsRunEvidence(
                name="tests",
                workflow_id=202,
                run_id=102,
                run_attempt=1,
                repository="owner/repo",
                head_repository="owner/repo",
                head_branch=str(kwargs["task_branch"]),
                head_sha=str(kwargs["head_sha"]),
                event="pull_request",
                status="completed",
                conclusion="success",
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:01:00Z",
                html_url="https://github.com/owner/repo/actions/runs/102",
            )
            return ActionsEvidenceSnapshot(
                repository="owner/repo",
                task_branch=str(kwargs["task_branch"]),
                head_sha=str(kwargs["head_sha"]),
                remote_ref_sha=str(kwargs["head_sha"]),
                observed_at=str(kwargs["observed_at"]),
                required_checks=(
                    RequiredCheckEvidence("tests", run.check_status, run),
                ),
            )

        service = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=self.orchestrator,
            ci_evidence_importer=SimpleNamespace(import_for_head=import_for_head),
        )
        task = replace(
            structured_claimed_task(),
            body=structured_claimed_task().body.replace(
                "- [AC-3] task-head-published",
                "- [AC-3] task-head-published\n"
                "- [AC-4] audit: verify the behavioral edge cases",
            ),
        )
        item = service.resolve_and_prepare(
            task, base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        artifact = b"fresh-audit-implementation"
        head_sha = "e" * 40
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
            FakeTurnFixture(
                SESSION,
                head_sha,
                completed_result(
                    (
                        {
                            "id": "AC-4",
                            "status": "not_verified",
                            "evidence": "Fresh Audit has not run yet",
                        },
                    )
                ),
                artifact,
            ),
        )
        implementation = service.run_claimed_turn(
            task, turn_id="turn_" + "e" * 32
        )
        implementation = service.publish_checkpoint(
            implementation.turn.turn_id,
            publisher=SimpleNamespace(
                publish=lambda artifact, *, plan, work_item: SimpleNamespace(
                    observed_remote_sha=plan.source_sha
                )
            ),
        )
        implementation = service.evaluate_completion_gate(
            task, implementation.turn.turn_id
        )
        self.assertTrue(
            service.requires_fresh_final_audit(implementation.turn.turn_id)
        )
        tracker = FakeTracker()
        running_task = replace(
            task,
            state=TaskState.RUNNING,
            labels=("agent:running", "exec:ssh-cli", "priority:p1"),
        )
        tracker.tasks[task.task_id] = running_task
        recovery = plan_ssh_recovery(config, self.store, tracker)
        self.assertEqual(
            SshRecoveryAction.START_FRESH_FINAL_AUDIT,
            recovery.action,
        )

        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(
                SESSION_2,
                head_sha,
                audit_completed_result(
                    (
                        {
                            "id": "AC-4",
                            "status": "passed",
                            "evidence": "Fresh read-only inspection passed",
                        },
                    )
                ),
            ),
        )
        audit = service.run_fresh_final_audit(
            running_task, turn_id="turn_" + "f" * 32
        )

        self.assertEqual(TurnState.PUBLISHED, audit.turn.state)
        generations = self.store.list_session_generations(item.work_item_id)
        self.assertEqual(
            ["implementation", "audit"],
            [generation.role.value for generation in generations],
        )
        self.assertEqual(
            ["retired", "active"],
            [generation.state.value for generation in generations],
        )
        self.assertEqual(SESSION, generations[0].codex_session_id)
        self.assertEqual(SESSION_2, generations[1].codex_session_id)
        handoff = self.store.get_session_handoff_for_generation(
            generations[1].session_generation_id
        )
        self.assertIsNotNone(handoff)
        assert handoff is not None
        self.assertEqual(
            "completion_candidate",
            handoff.trusted_facts["generation"]["rotation_reason"],
        )

        accepted = service.evaluate_completion_gate(
            running_task, audit.turn.turn_id
        )

        self.assertEqual(TurnState.FINISHED, accepted.turn.state)
        self.assertEqual(WorkItemState.REVIEW, accepted.work_item.state)
        self.assertFalse(service.requires_fresh_final_audit(audit.turn.turn_id))
        self.assertEqual(
            2,
            len(
                [
                    call
                    for call in self.transport.calls
                    if call.operation is RunnerOperation.START
                ]
            ),
        )

    def test_clean_context_failure_rotates_to_new_session_from_exact_anchor(self) -> None:
        config = replace(
            make_config(global_max_active=4),
            session_runtime=session_runtime_config(),
        )
        service = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=self.orchestrator,
        )
        task = claimed_task()
        item = service.resolve_and_prepare(
            task, base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(
                SESSION,
                BASE_SHA,
                blocked_result(),
                error_code="session_context_failure_clean",
                failure_head_sha=BASE_SHA,
                worktree_clean=True,
            ),
        )

        interrupted = service.run_claimed_turn(
            task, turn_id="turn_" + "1" * 32
        )

        self.assertEqual(TurnState.INTERRUPTED, interrupted.turn.state)
        self.assertEqual(WorkItemState.READY, interrupted.work_item.state)
        receipt = self.store.get_turn_context_failure(interrupted.turn.turn_id)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(BASE_SHA, receipt.head_sha)
        self.assertTrue(receipt.worktree_clean)

        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(SESSION_2, BASE_SHA, blocked_result()),
        )
        rotated = service.run_claimed_turn(
            task, turn_id="turn_" + "2" * 32
        )

        self.assertEqual(TurnState.BLOCKED, rotated.turn.state)
        generations = self.store.list_session_generations(item.work_item_id)
        self.assertEqual(["retired", "active"], [g.state.value for g in generations])
        self.assertEqual(
            "context_failure", generations[1].rotation_reason
        )
        self.assertEqual(SESSION, generations[0].codex_session_id)
        self.assertEqual(SESSION_2, generations[1].codex_session_id)
        handoff = self.store.get_session_handoff_for_generation(
            generations[1].session_generation_id
        )
        self.assertIsNotNone(handoff)
        assert handoff is not None
        self.assertEqual(
            "context_failure",
            handoff.trusted_facts["generation"]["rotation_reason"],
        )
        self.assertEqual(
            [RunnerOperation.START, RunnerOperation.START],
            [
                call.operation
                for call in self.transport.calls
                if call.operation in {RunnerOperation.START, RunnerOperation.RESUME}
            ],
        )

    def test_dirty_context_failure_is_terminal_and_never_rotates(self) -> None:
        config = replace(
            make_config(global_max_active=4),
            session_runtime=session_runtime_config(),
        )
        service = OfflineSshDispatchService(
            config=config,
            store=self.store,
            orchestrator=self.orchestrator,
        )
        task = claimed_task()
        item = service.resolve_and_prepare(
            task, base_sha=BASE_SHA, source_bundle=source_bundle()
        )
        self.transport.queue_turn(
            item.work_item_id,
            FakeTurnFixture(
                SESSION,
                BASE_SHA,
                blocked_result(),
                error_code="session_context_failure_dirty_worktree",
                failure_head_sha=BASE_SHA,
                worktree_clean=False,
            ),
        )

        blocked = service.run_claimed_turn(
            task, turn_id="turn_" + "3" * 32
        )

        self.assertEqual(TurnState.BLOCKED, blocked.turn.state)
        self.assertEqual(
            "session_context_failure_dirty_worktree", blocked.turn.error_code
        )
        self.assertEqual(WorkItemState.BLOCKED, blocked.work_item.state)
        self.assertIsNone(
            self.store.get_turn_context_failure(blocked.turn.turn_id)
        )
        generations = self.store.list_session_generations(item.work_item_id)
        self.assertEqual(1, len(generations))
        self.assertEqual("failed", generations[0].state.value)

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
