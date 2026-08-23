from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_dispatcher.control_host_backup import create_state_backup
from codex_dispatcher.disaster_recovery import (
    _reconcile_github,
    collect_runner_recovery_snapshot,
    DisasterRecoveryError,
    run_schema18_disaster_recovery_drill,
)
from codex_dispatcher.runner_protocol import RunnerOperation, RunnerRequest
from codex_dispatcher.runner_transport import RunnerArchiveReply, RunnerArchiveState
from codex_dispatcher.slack_reporting import (
    SlackDeliveryReceipt,
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


NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
COMMIT = "c" * 40
PREVIOUS = "b" * 40
HEAD = "d" * 40
CHANNEL = "C0BR2D0MS8Y"
MESSAGE_TS = "1700000000.000001"


class _SlackVerifier:
    def __init__(self) -> None:
        self.receipts: list[SlackDeliveryReceipt] = []

    def verify_receipt(self, receipt: SlackDeliveryReceipt) -> SlackDeliveryReceipt:
        self.receipts.append(receipt)
        return receipt


class DisasterRecoveryTests(unittest.TestCase):
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

    def test_root_admin_can_bind_snapshot_to_explicit_runner_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_items = Path(temp_dir) / "work-items"
            for name in (".registry", ".archives", ".absences"):
                (work_items / name).mkdir(mode=0o700, parents=True)
            owner_uid = work_items.stat().st_uid

            with patch(
                "codex_dispatcher.disaster_recovery.os.geteuid",
                return_value=owner_uid + 1000,
            ):
                snapshot = collect_runner_recovery_snapshot(
                    work_items_root=work_items,
                    current_release_commit=COMMIT,
                    trusted_owner_uid=owner_uid,
                )

            self.assertEqual(COMMIT, snapshot["current_release_commit"])
            self.assertEqual([], snapshot["registry_work_item_ids"])

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
        create_state_backup(database, backups, now=NOW)

        release = root / "releases" / COMMIT
        for relative in (
            "scripts/codex-dispatcher-v1",
            "scripts/codex-dispatcher-backup-v1",
            "scripts/codex-dispatcher-restore-drill-v1",
            "deploy/systemd/codex-dispatcher.service",
            "deploy/systemd/codex-dispatcher.timer",
            "deploy/systemd/codex-dispatcher-backup.service",
            "deploy/systemd/codex-dispatcher-backup.timer",
            "deploy/systemd/codex-dispatcher-health.service",
            "deploy/systemd/codex-dispatcher-health.timer",
            "deploy/systemd/codex-dispatcher-restore-drill.service",
            "deploy/systemd/codex-dispatcher-restore-drill.timer",
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
                    "release_commit": COMMIT,
                    "previous_control": f"/opt/codex-dispatcher/releases/{PREVIOUS}",
                    "previous_runner": f"releases/{PREVIOUS}",
                }
            ),
            encoding="utf-8",
        )
        config = root / "config.toml"
        config.write_text("# secret-free fixture config\n", encoding="utf-8")

        runner_snapshot = {
            "schema_version": 1,
            "kind": "runner_recovery_snapshot",
            "current_release_commit": COMMIT,
            "registry_work_item_ids": [item.work_item_id],
            "archives": [
                {
                    "work_item_id": item.work_item_id,
                    "expected_head_sha": HEAD,
                    "reclaimed_bytes": 8192,
                    "archived_at": "2026-08-23T01:01:00+00:00",
                    "storage_kind": "bounded_image",
                    "file_sha256": "e" * 64,
                }
            ],
            "absences": [],
        }
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
        tracker.branches[(item.repository, item.task_branch)] = HEAD
        return {
            "database": database,
            "backups": backups,
            "release": release,
            "receipt": receipt,
            "config": config,
            "runner_snapshot": runner_snapshot,
            "tracker": tracker,
            "item": item,
        }

    def test_full_schema18_drill_restores_reconciles_and_rebuilds_empty_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fixture = self._fixture(root)
            verifier = _SlackVerifier()
            recovery = root / "recovery"

            result = run_schema18_disaster_recovery_drill(
                database_path=fixture["database"],  # type: ignore[arg-type]
                backup_directory=fixture["backups"],  # type: ignore[arg-type]
                recovery_root=recovery,
                control_release_path=fixture["release"],  # type: ignore[arg-type]
                control_config_path=fixture["config"],  # type: ignore[arg-type]
                release_receipt_path=fixture["receipt"],  # type: ignore[arg-type]
                runner_snapshot=fixture["runner_snapshot"],  # type: ignore[arg-type]
                tracker=fixture["tracker"],  # type: ignore[arg-type]
                slack_verifier=verifier,
                now=NOW.replace(hour=13),
            )

            self.assertEqual(COMMIT, result.release_commit)
            self.assertEqual(tuple(range(1, 19)), tuple(
                json.loads(result.receipt_path.read_text())["database_schema_migrations"]
            ))
            self.assertEqual(1, result.work_item_count)
            self.assertEqual(1, result.runner_archive_count)
            self.assertEqual(1, result.github_issue_count)
            self.assertEqual(1, result.github_pr_count)
            self.assertEqual(1, result.slack_receipt_count)
            self.assertEqual(1, len(verifier.receipts))
            rebuilt = (
                recovery
                / "empty-control-host/var/lib/codex-dispatcher/state.db"
            )
            with StateStore(rebuilt, read_only=True) as store:
                self.assertEqual("ok", store.integrity_check())
                self.assertEqual(tuple(range(1, 19)), store.schema_migration_versions())
            self.assertTrue((recovery / "empty-control-host-manifest.json").is_file())

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
            recovery = root / "failed-recovery"

            with self.assertRaisesRegex(
                DisasterRecoveryError, "archive tombstone conflicts"
            ):
                run_schema18_disaster_recovery_drill(
                    database_path=fixture["database"],  # type: ignore[arg-type]
                    backup_directory=fixture["backups"],  # type: ignore[arg-type]
                    recovery_root=recovery,
                    control_release_path=fixture["release"],  # type: ignore[arg-type]
                    control_config_path=fixture["config"],  # type: ignore[arg-type]
                    release_receipt_path=fixture["receipt"],  # type: ignore[arg-type]
                    runner_snapshot=snapshot,
                    tracker=fixture["tracker"],  # type: ignore[arg-type]
                    slack_verifier=_SlackVerifier(),
                    now=NOW,
                )

            self.assertEqual(before, Path(fixture["database"]).read_bytes())  # type: ignore[arg-type]
            failed = json.loads((recovery / "failed-receipt.json").read_text())
            self.assertFalse(failed["online_state_modified"])


if __name__ == "__main__":
    unittest.main()
