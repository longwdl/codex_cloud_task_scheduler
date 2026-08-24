from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_dispatcher.control_host_backup import create_state_backup
from codex_dispatcher.disaster_recovery import (
    _reconcile_github,
    collect_runner_recovery_snapshot,
    create_disaster_recovery_bundle,
    DisasterRecoveryError,
    load_disaster_recovery_bundle,
    run_schema18_disaster_recovery_drill,
)
from codex_dispatcher.runner_protocol import RunnerOperation, RunnerRequest
from codex_dispatcher.health_alert_delivery import HealthAlertDeliveryCoordinator
from codex_dispatcher.lifecycle_health import LifecycleAlert
from codex_dispatcher.reclamation_canary import run_local_reclamation_canary
from codex_dispatcher.runner_reclamation_status import build_runner_reclamation_status
from codex_dispatcher.runner_transport import RunnerArchiveReply, RunnerArchiveState
from codex_dispatcher.slack_reporting import (
    SlackDeliveryReceipt,
    SlackOutboundMessage,
    SlackReportKind,
    build_slack_report,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.testing.fakes import FakeTracker
from codex_dispatcher.trackers.base import (
    PullRequest,
    PullRequestState,
    TaskState,
    TrackerTask,
)
from codex_dispatcher.work_items import WorkItem, WorkItemState
from codex_dispatcher.work_item_lifecycle import (
    TerminalGithubClosureKind,
    TerminalGithubClosureOutcome,
)


NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
COMMIT = "c" * 40
PREVIOUS = "b" * 40
HEAD = "d" * 40
CHANNEL = "C0BR2D0MS8Y"
MESSAGE_TS = "1700000000.000001"
IMAGE = "ghcr.io/longwdl/codex-cloud-task-scheduler-runner@sha256:" + "f" * 64
ORPHAN_ITEM = WorkItem.new(
    repository="owner/canary",
    issue_number=7,
    issue_node_id="I_kwDOCanary7",
    base_branch="main",
    base_sha="8" * 40,
    at="2026-08-22T00:00:00+00:00",
)
ORPHAN_WORK_ITEM = ORPHAN_ITEM.work_item_id


def _canonical(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(raw.encode()).hexdigest()


def _write_canonical(path: Path, payload: dict[str, object]) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    path.write_text(raw, encoding="utf-8")
    path.chmod(0o600)
    return sha256(raw.encode()).hexdigest()


class _SlackVerifier:
    def __init__(self) -> None:
        self.receipts: list[SlackDeliveryReceipt] = []

    def verify_receipt(self, receipt: SlackDeliveryReceipt) -> SlackDeliveryReceipt:
        self.receipts.append(receipt)
        return receipt


class _SlackPublisher:
    def __init__(self) -> None:
        self.count = 0

    def publish(self, report: SlackOutboundMessage) -> SlackDeliveryReceipt:
        self.count += 1
        message_ts = f"1700000100.{self.count:06d}"
        thread_ts = report.thread_ts or message_ts
        query = (
            ""
            if report.thread_ts is None
            else f"?thread_ts={thread_ts}&cid={report.channel_id}"
        )
        return SlackDeliveryReceipt(
            report.deduplication_key,
            report.channel_id,
            message_ts,
            thread_ts,
            f"https://fixture.slack.com/archives/{report.channel_id}/"
            f"p{message_ts.replace('.', '')}{query}",
        )


class DisasterRecoveryTests(unittest.TestCase):
    def test_discarded_issue_reconciliation_requires_closed_not_planned_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            item = WorkItem.new(
                repository="owner/repo",
                issue_number=10,
                issue_node_id="I_kwDOFixture10",
                base_branch="main",
                base_sha="a" * 40,
                at="2026-08-23T00:00:00+00:00",
            )
            tracker = FakeTracker()
            tracker.tasks["10"] = TrackerTask(
                repository=item.repository,
                task_id="10",
                issue_number=10,
                title="discarded fixture",
                body="fixture",
                state=TaskState.DISCARD,
                labels=("agent:discard",),
                created_at="2026-08-23T00:00:00Z",
                ready_approved_by=None,
                issue_node_id=item.issue_node_id,
                is_open=False,
                state_reason="not_planned",
            )
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                store.create_work_item(item)
                store.prepare_work_item_discard_request(
                    item.work_item_id,
                    expected_head_sha=item.base_sha,
                    pr_number=None,
                    requested_by="owner",
                    request_event_id="1001",
                    requested_at="2026-08-23T00:01:00+00:00",
                )
                store.record_discarded_work_item(item.work_item_id)
                store.prepare_terminal_github_closure(
                    item.work_item_id,
                    kind=TerminalGithubClosureKind.DISCARDED_ISSUE,
                )
                store.complete_terminal_github_closure(
                    item.work_item_id,
                    kind=TerminalGithubClosureKind.DISCARDED_ISSUE,
                    outcome=TerminalGithubClosureOutcome.CLOSED,
                )

                self.assertEqual((1, 0), _reconcile_github(store, tracker))

    def test_unpublished_branch_may_remain_at_exact_persisted_base(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            item = WorkItem.new(
                repository="owner/repo",
                issue_number=9,
                issue_node_id="I_kwDOFixture9",
                base_branch="main",
                base_sha="9" * 40,
                at="2026-08-23T00:00:00+00:00",
            )
            tracker = FakeTracker()
            tracker.tasks["9"] = TrackerTask(
                repository=item.repository,
                task_id="9",
                issue_number=9,
                title="fixture",
                body="fixture",
                state=TaskState.READY,
                labels=("agent:ready",),
                created_at="2026-08-23T00:00:00Z",
                ready_approved_by="maintainer",
                issue_node_id=item.issue_node_id,
            )
            tracker.branches[(item.repository, item.task_branch)] = item.base_sha
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                store.create_work_item(item)

                self.assertEqual((1, 0), _reconcile_github(store, tracker))

    def test_snapshot_binds_operational_runner_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            work_items = root / "work-items"
            for name in (".registry", ".archives", ".absences"):
                (work_items / name).mkdir(mode=0o700, parents=True)
            owner_uid = work_items.stat().st_uid
            references = {
                "schema_version": 2,
                "kind": "runner_reclamation_rollback_references",
                "current_release_commit": COMMIT,
                "immediate_rollback_release_commit": PREVIOUS,
                "current_image_ref": IMAGE,
                "protected_release_commits": [COMMIT, PREVIOUS],
                "protected_image_refs": [IMAGE],
            }
            reference_receipt = {
                "schema_version": 1,
                "kind": "runner_reclamation_reference_change_receipt",
                "operation": "apply",
                "status": "applied",
                "release_commit": COMMIT,
                "previous_release_commit": PREVIOUS,
                "after_references": references,
                "after_sha256": _canonical(references),
            }
            status = build_runner_reclamation_status(
                host_available_bytes=74 * 1024**3,
                release_count=2,
                release_target_count=0,
                image_target_count=0,
                expected_total_bytes=0,
                plan_sha256="1" * 64,
                now=NOW,
            ).to_mapping()
            references_path = root / "references.json"
            reference_receipt_path = root / "reference-receipt.json"
            status_path = root / "status.json"
            _write_canonical(references_path, references)
            _write_canonical(reference_receipt_path, reference_receipt)
            _write_canonical(status_path, status)
            installed_units = root / "installed-units"
            release = root / "release"
            for unit in (
                "codex-runner-reclamation-plan.service",
                "codex-runner-reclamation-plan.timer",
            ):
                (installed_units / unit).parent.mkdir(parents=True, exist_ok=True)
                (release / "deploy/runner" / unit).parent.mkdir(
                    parents=True, exist_ok=True
                )
                (installed_units / unit).write_text(unit)
                (release / "deploy/runner" / unit).write_text(unit)

            snapshot = collect_runner_recovery_snapshot(
                work_items_root=work_items,
                current_release_commit=COMMIT,
                trusted_owner_uid=owner_uid,
                release_references_path=references_path,
                release_reference_receipt_path=reference_receipt_path,
                reclamation_status_path=status_path,
                installed_unit_root=installed_units,
                current_release_path=release,
            )

            self.assertEqual(COMMIT, snapshot["current_release_commit"])
            self.assertEqual([], snapshot["registry"])

    def _fixture(self, root: Path) -> dict[str, object]:
        database = root / "state.db"
        backups = root / "backups"
        backups.mkdir(mode=0o700)
        item = WorkItem.new(
            repository="owner/repo",
            issue_number=1,
            issue_node_id="I_kwDOFixture1",
            base_branch="main",
            base_sha="a" * 40,
            at="2026-08-23T00:00:00+00:00",
        )
        with StateStore(database) as store:
            store.migrate()
            store.create_work_item(item)
            store.record_published_sha(
                item.work_item_id,
                previous_sha=item.base_sha,
                head_sha=HEAD,
            )
            store._connection.execute(
                "UPDATE work_items SET pr_number = 2 WHERE work_item_id = ?",
                (item.work_item_id,),
            )
            store._connection.commit()
            for state in (
                WorkItemState.PREPARING,
                WorkItemState.READY,
                WorkItemState.RUNNING,
                WorkItemState.REVIEW,
                WorkItemState.COMPLETED,
            ):
                store.update_work_item_state(item.work_item_id, state)
            store.prepare_terminal_github_closure(
                item.work_item_id,
                kind=TerminalGithubClosureKind.COMPLETED_ISSUE,
            )
            store.complete_terminal_github_closure(
                item.work_item_id,
                kind=TerminalGithubClosureKind.COMPLETED_ISSUE,
                outcome=TerminalGithubClosureOutcome.CLOSED,
            )
            archive_request = RunnerRequest(
                RunnerOperation.ARCHIVE,
                item.work_item_id,
                version=2,
                expected_head_sha=HEAD,
            )
            store.prepare_work_item_archive(
                item.work_item_id,
                expected_head_sha=HEAD,
                eligible_at="2026-08-23T01:00:00+00:00",
                request_sha256=sha256(
                    archive_request.to_json().encode("utf-8")
                ).hexdigest(),
            )
            archive_reply = RunnerArchiveReply(
                RunnerOperation.ARCHIVE,
                item.work_item_id,
                HEAD,
                RunnerArchiveState.ARCHIVED,
                archived_at="2026-08-23T01:01:00+00:00",
                reclaimed_bytes=8192,
            )
            store.complete_work_item_archive(item.work_item_id, reply=archive_reply)
            report = build_slack_report(
                work_item_id=item.work_item_id,
                kind=SlackReportKind.ROOT,
                channel_id=CHANNEL,
                text="fixture",
            )
            store.prepare_slack_delivery(report)
            permalink = (
                f"https://fixture.slack.com/archives/{CHANNEL}/"
                f"p{MESSAGE_TS.replace('.', '')}"
            )
            store.complete_slack_delivery(
                report.deduplication_key,
                SlackDeliveryReceipt(
                    report.deduplication_key,
                    CHANNEL,
                    MESSAGE_TS,
                    MESSAGE_TS,
                    permalink,
                ),
            )
            coordinator = HealthAlertDeliveryCoordinator(
                store=store,
                publisher=_SlackPublisher(),
                channel_id="C0BS3LPG43G",
            )
            coordinator.reconcile(
                (LifecycleAlert("runner_reclamation_plan_ready", plan_sha256="1" * 64),),
                checked_at="2026-08-23T01:10:00Z",
                alerts_truncated=False,
            )
            coordinator.reconcile(
                (), checked_at="2026-08-23T01:11:00Z", alerts_truncated=False
            )
        create_state_backup(database, backups, now=NOW)

        release = root / "releases" / COMMIT
        for relative in (
            "scripts/codex-dispatcher-v1",
            "scripts/codex-dispatcher-backup-v1",
            "scripts/codex-dispatcher-control-reclamation-plan-v1",
            "scripts/codex-dispatcher-release-handoff-v1",
            "scripts/codex-dispatcher-restore-drill-v1",
            "scripts/codex-runner-maintenance-v1",
            "deploy/systemd/codex-dispatcher.service",
            "deploy/systemd/codex-dispatcher.timer",
            "deploy/systemd/codex-dispatcher-backup.service",
            "deploy/systemd/codex-dispatcher-backup.timer",
            "deploy/systemd/codex-dispatcher-health.service",
            "deploy/systemd/codex-dispatcher-health.timer",
            "deploy/systemd/codex-dispatcher-restore-drill.service",
            "deploy/systemd/codex-dispatcher-restore-drill.timer",
            "deploy/runner/codex-runner-reclamation-plan.service",
            "deploy/runner/codex-runner-reclamation-plan.timer",
        ):
            path = release / relative
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(relative, encoding="utf-8")
        receipt = root / "release-receipt.json"
        receipt.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "kind": "codex_dispatcher_release",
                    "operation": "apply",
                    "status": "committed",
                    "phase": "handoff_required",
                    "committed": True,
                    "runner_references_applied": True,
                    "release_commit": COMMIT,
                    "previous_control": f"/opt/codex-dispatcher/releases/{PREVIOUS}",
                    "previous_runner": f"releases/{PREVIOUS}",
                    "updated_at": "2026-08-23T12:30:00Z",
                }
            ),
            encoding="utf-8",
        )
        config = root / "config.toml"
        config.write_text("# secret-free fixture config\n", encoding="utf-8")

        references = {
            "schema_version": 2,
            "kind": "runner_reclamation_rollback_references",
            "current_release_commit": COMMIT,
            "immediate_rollback_release_commit": PREVIOUS,
            "current_image_ref": IMAGE,
            "protected_release_commits": [COMMIT, PREVIOUS],
            "protected_image_refs": [IMAGE],
        }
        reference_receipt = {
            "schema_version": 1,
            "kind": "runner_reclamation_reference_change_receipt",
            "operation": "apply",
            "status": "applied",
            "release_commit": COMMIT,
            "previous_release_commit": PREVIOUS,
            "after_references": references,
            "after_sha256": _canonical(references),
        }
        reclamation_status = build_runner_reclamation_status(
            host_available_bytes=74 * 1024**3,
            release_count=2,
            release_target_count=0,
            image_target_count=0,
            expected_total_bytes=0,
            plan_sha256="1" * 64,
            now=NOW,
        ).to_mapping()
        references_sha = sha256(
            (json.dumps(references, sort_keys=True, separators=(",", ":")) + "\n").encode()
        ).hexdigest()
        reference_receipt_sha = sha256(
            (
                json.dumps(reference_receipt, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode()
        ).hexdigest()
        status_sha = sha256(
            (
                json.dumps(reclamation_status, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode()
        ).hexdigest()
        runner_snapshot = {
            "schema_version": 3,
            "kind": "runner_recovery_snapshot",
            "current_release_commit": COMMIT,
            "registry": [
                {
                    "work_item_id": item.work_item_id,
                    "repository": item.repository,
                    "issue_number": item.issue_number,
                },
                {
                    "work_item_id": ORPHAN_ITEM.work_item_id,
                    "repository": ORPHAN_ITEM.repository,
                    "issue_number": ORPHAN_ITEM.issue_number,
                },
            ],
            "archives": [
                {
                    "work_item_id": item.work_item_id,
                    "expected_head_sha": HEAD,
                    "reclaimed_bytes": 8192,
                    "archived_at": "2026-08-23T01:01:00+00:00",
                    "storage_kind": "bounded_image",
                    "file_sha256": "e" * 64,
                },
                {
                    "work_item_id": ORPHAN_WORK_ITEM,
                    "expected_head_sha": "9" * 40,
                    "reclaimed_bytes": 1024,
                    "archived_at": "2026-08-22T00:00:00+00:00",
                    "storage_kind": "fuse_ext4_image",
                    "file_sha256": "8" * 64,
                },
            ],
            "absences": [],
            "release_references": references,
            "release_references_sha256": references_sha,
            "release_reference_receipt": reference_receipt,
            "release_reference_receipt_sha256": reference_receipt_sha,
            "reclamation_status": reclamation_status,
            "reclamation_status_sha256": status_sha,
            "planner_unit_sha256": {
                unit: sha256(
                    (release / "deploy/runner" / unit).read_bytes()
                ).hexdigest()
                for unit in (
                    "codex-runner-reclamation-plan.service",
                    "codex-runner-reclamation-plan.timer",
                )
            },
        }
        release_sha = sha256(receipt.read_bytes()).hexdigest()
        handoff_payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "codex_dispatcher_release_handoff",
            "status": "operational",
            "release_commit": COMMIT,
            "release_receipt_sha256": release_sha,
            "runner_references_sha256": references_sha,
            "runner_reference_receipt_sha256": reference_receipt_sha,
            "runner_reclamation_status_sha256": status_sha,
            "runner_reclamation_status": reclamation_status,
            "timers_started": True,
            "external_writes": False,
            "authorizes_reclamation_apply": False,
        }
        handoff_payload["evidence_sha256"] = _canonical(handoff_payload)
        handoff = root / "handoff-receipt.json"
        handoff.write_text(json.dumps(handoff_payload), encoding="utf-8")
        runner_snapshot_path = root / "runner-snapshot.json"
        _write_canonical(runner_snapshot_path, runner_snapshot)
        provenance = root / "provenance.db"
        with StateStore(provenance) as store:
            store.migrate()
            store.create_work_item(ORPHAN_ITEM)
            store.record_published_sha(
                ORPHAN_ITEM.work_item_id,
                previous_sha=ORPHAN_ITEM.base_sha,
                head_sha="9" * 40,
            )
        system_receipt = root / "system-canary-receipt.json"
        _write_canonical(
            system_receipt,
            run_local_reclamation_canary(
                fixture_id="rc_" + "3" * 32,
                system_channel_id="C0BS3LPG43G",
                issue_channel_id=CHANNEL,
            ),
        )
        tracker = FakeTracker()
        tracker.tasks["1"] = TrackerTask(
            repository="owner/repo",
            task_id="1",
            issue_number=1,
            title="fixture",
            body="fixture",
            state=TaskState.COMPLETED,
            labels=("agent:completed",),
            created_at="2026-08-23T00:00:00Z",
            ready_approved_by=None,
            issue_node_id=item.issue_node_id,
            is_open=False,
            state_reason="completed",
        )
        tracker.pull_requests[(item.repository, item.task_branch)] = PullRequest(
            number=2,
            url="https://github.com/owner/repo/pull/2",
            branch_name=item.task_branch,
            title="fixture",
            is_draft=False,
            base_branch="main",
            state=PullRequestState.MERGED,
            head_sha=HEAD,
        )
        return {
            "database": database,
            "backups": backups,
            "release": release,
            "receipt": receipt,
            "handoff": handoff,
            "config": config,
            "runner_snapshot": runner_snapshot,
            "runner_snapshot_path": runner_snapshot_path,
            "provenance": provenance,
            "system_receipt": system_receipt,
            "tracker": tracker,
            "item": item,
        }

    def _bundle(
        self, root: Path, fixture: dict[str, object], *, name: str = "bundle"
    ):
        return create_disaster_recovery_bundle(
            database_path=fixture["database"],  # type: ignore[arg-type]
            backup_directory=fixture["backups"],  # type: ignore[arg-type]
            bundle_root=root / name,
            control_release_path=fixture["release"],  # type: ignore[arg-type]
            control_config_path=fixture["config"],  # type: ignore[arg-type]
            release_receipt_path=fixture["receipt"],  # type: ignore[arg-type]
            handoff_receipt_path=fixture["handoff"],  # type: ignore[arg-type]
            runner_snapshot_path=fixture["runner_snapshot_path"],  # type: ignore[arg-type]
            provenance_database_paths=(fixture["provenance"],),  # type: ignore[arg-type]
            system_slack_receipt_paths=(fixture["system_receipt"],),  # type: ignore[arg-type]
            now=NOW.replace(hour=13),
        )

    def test_full_schema18_drill_restores_reconciles_and_rebuilds_empty_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = self._fixture(root)
            verifier = _SlackVerifier()
            recovery = root / "recovery"
            bundle = self._bundle(root, fixture)
            with patch(
                "codex_dispatcher.disaster_recovery.os.geteuid",
                return_value=os.getuid() + 1,
            ):
                trusted = load_disaster_recovery_bundle(
                    bundle.root, trusted_owner_uid=os.getuid()
                )
            self.assertEqual(bundle.manifest_sha256, trusted.manifest_sha256)

            result = run_schema18_disaster_recovery_drill(
                bundle=bundle,
                recovery_root=recovery,
                tracker=fixture["tracker"],  # type: ignore[arg-type]
                slack_verifier=verifier,
                now=NOW.replace(hour=13),
            )

            self.assertEqual(COMMIT, result.release_commit)
            self.assertEqual(tuple(range(1, 22)), tuple(
                json.loads(result.receipt_path.read_text())["database_schema_migrations"]
            ))
            self.assertEqual(1, result.work_item_count)
            self.assertEqual(1, result.runner_archive_count)
            self.assertEqual(1, result.runner_orphan_terminal_count)
            self.assertEqual(1, result.github_issue_count)
            self.assertEqual(1, result.github_pr_count)
            self.assertEqual(5, result.slack_receipt_count)
            self.assertEqual(1, result.work_item_slack_receipt_count)
            self.assertEqual(4, result.system_slack_receipt_count)
            self.assertEqual(5, len(verifier.receipts))
            rebuilt = (
                recovery
                / "empty-control-host/var/lib/codex-dispatcher/state.db"
            )
            with StateStore(rebuilt, read_only=True) as store:
                self.assertEqual("ok", store.integrity_check())
                self.assertEqual(tuple(range(1, 22)), store.schema_migration_versions())
            self.assertTrue((recovery / "empty-control-host-manifest.json").is_file())
            self.assertTrue((recovery / "empty-runner-host-manifest.json").is_file())
            self.assertTrue(
                (
                    recovery
                    / "empty-runner-host/srv/codex-runner/etc/"
                    "reclamation-rollback-references.json"
                ).is_file()
            )

    def test_runner_tombstone_drift_fails_without_touching_online_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = self._fixture(root)
            before = Path(fixture["database"]).read_bytes()  # type: ignore[arg-type]
            snapshot = fixture["runner_snapshot"]  # type: ignore[assignment]
            assert isinstance(snapshot, dict)
            archives = snapshot["archives"]
            assert isinstance(archives, list) and isinstance(archives[0], dict)
            archives[0]["reclaimed_bytes"] = 1
            _write_canonical(
                fixture["runner_snapshot_path"], snapshot  # type: ignore[arg-type]
            )
            bundle = self._bundle(root, fixture, name="drifted-bundle")
            recovery = root / "failed-recovery"

            with self.assertRaisesRegex(
                DisasterRecoveryError, "archive tombstone conflicts"
            ):
                run_schema18_disaster_recovery_drill(
                    bundle=bundle,
                    recovery_root=recovery,
                    tracker=fixture["tracker"],  # type: ignore[arg-type]
                    slack_verifier=_SlackVerifier(),
                    now=NOW,
                )

            self.assertEqual(before, Path(fixture["database"]).read_bytes())  # type: ignore[arg-type]
            failed = json.loads((recovery / "failed-receipt.json").read_text())
            self.assertFalse(failed["online_state_modified"])


if __name__ == "__main__":
    unittest.main()
