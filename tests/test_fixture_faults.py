from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_dispatcher.fixture_fault_cli import _create_backup, _run
from codex_dispatcher.fixture_faults import (
    FIXTURE_REPOSITORY,
    FixtureFaultInjection,
    FixtureFaultPoint,
    FixtureFaultRejected,
    FixtureProcessInterrupted,
    FixtureReceiptLost,
    validate_fixture_preflight,
)
from codex_dispatcher.git_publisher import (
    GitPublicationInterrupted,
    PublicationReceipt,
)
from codex_dispatcher.publisher import PublicationPlan
from codex_dispatcher.control_sweep import ControlSweepResult, ControlSweepStatus
from codex_dispatcher.ssh_runtime import SshPreflightInspection
from codex_dispatcher.ssh_preflight import SshPreflightPlan, SshPreflightStatus
from codex_dispatcher.ssh_recovery import SshRecoveryAction
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.testing.fakes import FakeTracker
from codex_dispatcher.trackers.base import (
    DraftPullRequestRequest,
    TaskState,
    TrackerTask,
)
from codex_dispatcher.work_items import Turn, TurnState, WorkItem, WorkItemState
from tests.test_scheduler import make_config


ISSUE = 7
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def _task(state: TaskState) -> TrackerTask:
    return TrackerTask(
        repository=FIXTURE_REPOSITORY,
        task_id=str(ISSUE),
        issue_number=ISSUE,
        title="Fixture fault",
        body="fixture",
        state=state,
        labels=(f"agent:{state.value}", "exec:ssh-cli"),
        created_at="2026-08-19T00:00:00Z",
        ready_approved_by="longwdl",
        issue_node_id="I_fixture_fault_7",
        updated_at="2026-08-19T00:00:00Z",
    )


def _running_item() -> WorkItem:
    item = WorkItem.new(
        repository=FIXTURE_REPOSITORY,
        issue_number=ISSUE,
        issue_node_id="I_fixture_fault_7",
        base_branch="main",
        base_sha=BASE_SHA,
        at="2026-08-19T00:00:00Z",
    )
    for state in (
        WorkItemState.PREPARING,
        WorkItemState.READY,
        WorkItemState.RUNNING,
    ):
        item = item.transition_to(state, at="2026-08-19T00:00:00Z")
    return item


def _plan(item: WorkItem) -> PublicationPlan:
    return PublicationPlan(
        work_item_id=item.work_item_id,
        repository=item.repository,
        source_sha=HEAD_SHA,
        target_ref=f"refs/heads/{item.task_branch}",
        expected_remote_sha=None,
        bundle_sha256="c" * 64,
        changed_paths=("README.md",),
    )


def _checkpoint_turn(item: WorkItem) -> Turn:
    turn = Turn.new(
        work_item_id=item.work_item_id,
        turn_number=1,
        issue_revision="revision-1",
        prompt_sha256="d" * 64,
        input_head_sha=BASE_SHA,
        issue_allowed_paths=("README.md",),
        turn_id="turn_" + "1" * 32,
        at="2026-08-19T00:00:00Z",
    )
    return replace(
        turn,
        state=TurnState.CHECKPOINTING,
        output_sha256="e" * 64,
        output_head_sha=HEAD_SHA,
        result_status="completed",
        result_summary="Fixture checkpoint",
        started_at="2026-08-19T00:00:01Z",
        updated_at="2026-08-19T00:00:02Z",
    )


class FixtureFaultTests(unittest.TestCase):
    def test_publisher_discards_only_a_successful_exact_fixture_receipt(self) -> None:
        item = _running_item()
        plan = _plan(item)
        receipt = PublicationReceipt(
            item.work_item_id,
            plan.target_ref,
            HEAD_SHA,
            False,
        )
        delegate = SimpleNamespace(publish=lambda *args, **kwargs: receipt)
        injection = FixtureFaultInjection(
            FixtureFaultPoint.PUBLISHER_RECEIPT,
            ISSUE,
        )
        publisher = injection.wrap_publisher(delegate)

        with self.assertRaisesRegex(GitPublicationInterrupted, "discarded"):
            publisher.publish(b"bundle", plan=plan, work_item=item)

        self.assertTrue(injection.triggered)
        with self.assertRaisesRegex(FixtureFaultRejected, "escaped"):
            publisher.publish(
                b"bundle",
                plan=replace(plan, repository="owner/production"),
                work_item=item,
            )

        reused = SimpleNamespace(
            publish=lambda *args, **kwargs: replace(receipt, reused=True)
        )
        reused_injection = FixtureFaultInjection(
            FixtureFaultPoint.PUBLISHER_RECEIPT,
            ISSUE,
        )
        with self.assertRaisesRegex(FixtureFaultRejected, "newly written"):
            reused_injection.wrap_publisher(reused).publish(
                b"bundle",
                plan=plan,
                work_item=item,
            )
        self.assertFalse(reused_injection.triggered)

    def test_tracker_discards_pr_and_comment_receipts_after_delegate_write(self) -> None:
        item = _running_item()
        request = DraftPullRequestRequest(
            repository=FIXTURE_REPOSITORY,
            branch_name=item.task_branch,
            base_branch="main",
            title=f"Codex work for Issue #{ISSUE}",
            body="fixture",
        )
        pr_delegate = FakeTracker()
        pr_injection = FixtureFaultInjection(
            FixtureFaultPoint.DRAFT_PR_RECEIPT,
            ISSUE,
        )

        with self.assertRaisesRegex(FixtureReceiptLost, "draft-pr-receipt"):
            pr_injection.wrap_tracker(pr_delegate).create_draft_pr(request)

        self.assertTrue(pr_injection.triggered)
        self.assertIn((FIXTURE_REPOSITORY, item.task_branch), pr_delegate.pull_requests)

        comment_delegate = FakeTracker()
        comment_delegate.tasks[str(ISSUE)] = _task(TaskState.RUNNING)
        comment_injection = FixtureFaultInjection(
            FixtureFaultPoint.ISSUE_COMMENT_RECEIPT,
            ISSUE,
        )
        with self.assertRaisesRegex(FixtureReceiptLost, "issue-comment-receipt"):
            comment_injection.wrap_tracker(comment_delegate).upsert_run_comment(
                FIXTURE_REPOSITORY,
                str(ISSUE),
                f"work-item:{item.work_item_id}:status",
                "fixture status",
            )

        self.assertTrue(comment_injection.triggered)
        self.assertEqual("upsert_run_comment", comment_delegate.calls[-1].method)

    def test_tracker_rejects_wrong_issue_and_stage_before_delegate_write(self) -> None:
        delegate = FakeTracker()
        injection = FixtureFaultInjection(FixtureFaultPoint.DRAFT_PR_RECEIPT, ISSUE)
        tracker = injection.wrap_tracker(delegate)

        with self.assertRaisesRegex(FixtureFaultRejected, "Issue"):
            tracker.claim(
                FIXTURE_REPOSITORY,
                str(ISSUE + 1),
                "dispatcher",
                approved_by=("longwdl",),
            )
        with self.assertRaisesRegex(FixtureFaultRejected, "unexpected Issue comment"):
            tracker.upsert_run_comment(
                FIXTURE_REPOSITORY,
                str(ISSUE),
                "work-item:wi_000000000000000000000000:status",
                "fixture",
            )

        self.assertEqual([], delegate.calls)

    def test_publication_recorded_hook_and_recovery_io_guards_are_exact(self) -> None:
        recorded = replace(_running_item(), last_published_sha=HEAD_SHA)
        turn = _checkpoint_turn(recorded)
        injection = FixtureFaultInjection(
            FixtureFaultPoint.PUBLICATION_RECORDED,
            ISSUE,
        )

        with self.assertRaisesRegex(FixtureProcessInterrupted, "recording"):
            injection.after_publication_recorded(recorded, turn)

        self.assertTrue(injection.triggered)
        recovery = FixtureFaultInjection(
            FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY,
            ISSUE,
        )
        guarded_transport = recovery.wrap_transport(
            SimpleNamespace(invoke=lambda *args, **kwargs: None)
        )
        with self.assertRaisesRegex(FixtureFaultRejected, "Runner"):
            guarded_transport.invoke(SimpleNamespace())

        receipt = PublicationReceipt(
            recorded.work_item_id,
            _plan(recorded).target_ref,
            HEAD_SHA,
            True,
        )
        guarded_publisher = recovery.wrap_publisher(
            SimpleNamespace(publish=lambda *args, **kwargs: receipt)
        )
        with self.assertRaisesRegex(FixtureFaultRejected, "Publisher"):
            guarded_publisher.publish(
                b"bundle",
                plan=_plan(recorded),
                work_item=recorded,
            )

    def test_claim_acquired_hook_stops_before_any_later_port(self) -> None:
        delegate = FakeTracker()
        delegate.ready_tasks = (_task(TaskState.READY),)
        delegate.tasks[str(ISSUE)] = _task(TaskState.READY)
        observed: list[str] = []
        injection = FixtureFaultInjection(
            FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL,
            ISSUE,
            claim_acquired_callback=lambda task: observed.append(task.issue_node_id),
        )
        tracker = injection.wrap_tracker(delegate)

        claimed = tracker.claim(
            FIXTURE_REPOSITORY,
            str(ISSUE),
            "codex-dispatcher",
            approved_by=("longwdl",),
        )
        assert claimed.task is not None
        with self.assertRaisesRegex(FixtureFaultRejected, "unexpectedly returned"):
            injection.after_claim_acquired(claimed.task)

        self.assertTrue(injection.triggered)
        self.assertEqual(["I_fixture_fault_7"], observed)
        with self.assertRaisesRegex(FixtureFaultRejected, "Runner"):
            injection.wrap_transport(
                SimpleNamespace(invoke=lambda *args, **kwargs: None)
            ).invoke(SimpleNamespace())
        with self.assertRaisesRegex(FixtureFaultRejected, "state write"):
            tracker.set_state(
                FIXTURE_REPOSITORY,
                str(ISSUE),
                TaskState.RUNNING,
            )
        with self.assertRaisesRegex(FixtureFaultRejected, "claim callback"):
            FixtureFaultInjection(
                FixtureFaultPoint.PUBLISHER_RECEIPT,
                ISSUE,
                claim_acquired_callback=lambda task: None,
            )

    def test_preflight_requires_the_exact_fault_sequences(self) -> None:
        ready = SshPreflightPlan(
            SshPreflightStatus.READY_CANDIDATE,
            SshRecoveryAction.IDLE,
            task=_task(TaskState.READY),
        )
        validate_fixture_preflight(
            ready,
            fault=FixtureFaultPoint.PUBLISHER_RECEIPT,
            issue_number=ISSUE,
        )
        validate_fixture_preflight(
            ready,
            fault=FixtureFaultPoint.PUBLICATION_RECORDED,
            issue_number=ISSUE,
        )
        validate_fixture_preflight(
            ready,
            fault=FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL,
            issue_number=ISSUE,
        )

        running = _running_item()
        publication = SshPreflightPlan(
            SshPreflightStatus.READY_RECOVERY,
            SshRecoveryAction.RESUME_PUBLICATION,
            task=_task(TaskState.RUNNING),
            work_item=running,
        )
        validate_fixture_preflight(
            publication,
            fault=FixtureFaultPoint.DRAFT_PR_RECEIPT,
            issue_number=ISSUE,
        )

        recorded = replace(running, last_published_sha=HEAD_SHA)
        recorded_recovery = SshPreflightPlan(
            SshPreflightStatus.READY_RECOVERY,
            SshRecoveryAction.RESUME_PUBLICATION,
            task=_task(TaskState.DISPATCHING),
            work_item=recorded,
            turn=_checkpoint_turn(recorded),
        )
        validate_fixture_preflight(
            recorded_recovery,
            fault=FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY,
            issue_number=ISSUE,
        )
        with self.assertRaisesRegex(FixtureFaultRejected, "durable checkpoint"):
            validate_fixture_preflight(
                replace(recorded_recovery, turn=None),
                fault=FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY,
                issue_number=ISSUE,
            )

        review = replace(
            running.transition_to(
                WorkItemState.REVIEW,
                at="2026-08-19T00:00:00Z",
            ),
            last_published_sha=HEAD_SHA,
        )
        comment = SshPreflightPlan(
            SshPreflightStatus.READY_RECOVERY,
            SshRecoveryAction.SYNC_TRACKER_STATE,
            task=_task(TaskState.RUNNING),
            work_item=review,
        )
        validate_fixture_preflight(
            comment,
            fault=FixtureFaultPoint.ISSUE_COMMENT_RECEIPT,
            issue_number=ISSUE,
        )

        with self.assertRaisesRegex(FixtureFaultRejected, "unbound"):
            validate_fixture_preflight(
                replace(comment, work_item=replace(review, pr_number=3)),
                fault=FixtureFaultPoint.ISSUE_COMMENT_RECEIPT,
                issue_number=ISSUE,
            )

    def test_cli_guards_fail_before_loading_live_config(self) -> None:
        environments = (
            {},
            {
                "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS": "1",
                "GITHUB_TOKEN": "github_pat_fixture_test",
            },
        )
        for environment in environments:
            with (
                self.subTest(environment=environment),
                patch.dict("os.environ", environment, clear=True),
                patch(
                    "codex_dispatcher.fixture_fault_cli.load_protected_ssh_config"
                ) as load,
            ):
                code, payload = _run(
                    Path("/protected/config.toml"),
                    issue_number=ISSUE,
                    fault=FixtureFaultPoint.PUBLISHER_RECEIPT,
                )

                self.assertEqual(1, code)
                self.assertFalse(payload["ok"])
                load.assert_not_called()

    def test_cli_reports_an_expected_publisher_receipt_loss_as_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.db"
            with StateStore(database) as store:
                store.migrate()
            config = make_config(global_max_active=1, repository_max_active=1)
            config = replace(
                config,
                scheduler=replace(config.scheduler, database_path=database),
                repositories=(
                    replace(
                        config.repositories[0],
                        slug=FIXTURE_REPOSITORY,
                        allowed_paths=("README.md",),
                        denied_paths=(),
                        maintainers=("longwdl",),
                        required_checks=("fixture",),
                    ),
                ),
            )
            plan = SshPreflightPlan(
                SshPreflightStatus.READY_CANDIDATE,
                SshRecoveryAction.IDLE,
                task=_task(TaskState.READY),
            )

            def build_fixture_sweep(**kwargs):
                injection = kwargs["injection"]

                def run_once():
                    injection.triggered = True
                    return ControlSweepResult(
                        ControlSweepStatus.AWAITING_PUBLICATION,
                        FIXTURE_REPOSITORY,
                        ISSUE,
                        "wi_000000000000000000000000",
                        "turn_00000000000000000000000000000000",
                        "publication_outcome_ambiguous",
                    )

                return SimpleNamespace(run_once=run_once)

            with (
                patch.dict(
                    "os.environ",
                    {
                        "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                        "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS": FIXTURE_REPOSITORY,
                        "GITHUB_TOKEN": "github_pat_fixture_test",
                    },
                    clear=True,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.load_protected_ssh_config",
                    return_value=config,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.validate_runtime_state_path"
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.run_ssh_preflight",
                    return_value=SshPreflightInspection(plan, (), True),
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.build_ssh_fixture_fault_sweep",
                    side_effect=build_fixture_sweep,
                ),
            ):
                code, payload = _run(
                    Path(root) / "config.toml",
                    issue_number=ISSUE,
                    fault=FixtureFaultPoint.PUBLISHER_RECEIPT,
                )

        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["fault_triggered"])
        self.assertTrue(payload["recovery_required"])
        self.assertEqual("awaiting_publication", payload["status"])

    def test_cli_reports_guarded_recorded_publication_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.db"
            with StateStore(database) as store:
                store.migrate()
            config = make_config(global_max_active=1, repository_max_active=1)
            config = replace(
                config,
                scheduler=replace(config.scheduler, database_path=database),
                repositories=(
                    replace(
                        config.repositories[0],
                        slug=FIXTURE_REPOSITORY,
                        allowed_paths=("README.md",),
                        denied_paths=(),
                        maintainers=("longwdl",),
                        required_checks=("fixture",),
                    ),
                ),
            )
            recorded = replace(_running_item(), last_published_sha=HEAD_SHA)
            plan = SshPreflightPlan(
                SshPreflightStatus.READY_RECOVERY,
                SshRecoveryAction.RESUME_PUBLICATION,
                task=_task(TaskState.DISPATCHING),
                work_item=recorded,
                turn=_checkpoint_turn(recorded),
            )

            def build_fixture_sweep(**kwargs):
                def run_once():
                    return ControlSweepResult(
                        ControlSweepStatus.REVIEW,
                        FIXTURE_REPOSITORY,
                        ISSUE,
                        recorded.work_item_id,
                        plan.turn.turn_id,
                    )

                return SimpleNamespace(run_once=run_once)

            with (
                patch.dict(
                    "os.environ",
                    {
                        "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                        "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS": FIXTURE_REPOSITORY,
                        "GITHUB_TOKEN": "github_pat_fixture_test",
                    },
                    clear=True,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.load_protected_ssh_config",
                    return_value=config,
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.validate_runtime_state_path"
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.run_ssh_preflight",
                    return_value=SshPreflightInspection(plan, (), True),
                ),
                patch(
                    "codex_dispatcher.fixture_fault_cli.build_ssh_fixture_fault_sweep",
                    side_effect=build_fixture_sweep,
                ),
            ):
                code, payload = _run(
                    Path(root) / "config.toml",
                    issue_number=ISSUE,
                    fault=FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY,
                )

        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["fault_triggered"])
        self.assertTrue(payload["recovery_guarded"])
        self.assertFalse(payload["recovery_required"])
        self.assertEqual("review", payload["status"])

    def test_backup_is_private_complete_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.db"
            with StateStore(database) as store:
                store.migrate()
            backup = _create_backup(database, FixtureFaultPoint.PUBLISHER_RECEIPT)

            self.assertEqual(0o600, backup.stat().st_mode & 0o777)
            with StateStore(backup, read_only=True) as store:
                self.assertEqual("ok", store.integrity_check())


if __name__ == "__main__":
    unittest.main()
