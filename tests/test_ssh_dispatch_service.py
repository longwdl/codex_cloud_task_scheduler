from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

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


class OfflineSshDispatchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "state.db")
        self.store.migrate()
        self.transport = FakeSshRunnerTransport()
        self.orchestrator = OfflineTurnOrchestrator(
            store=self.store,
            transport=self.transport,
            bundle_verifier=FakeBundleVerifier(),
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
