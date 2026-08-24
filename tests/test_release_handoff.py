from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from codex_dispatcher.github_api_metrics import (
    GitHubApiMetrics,
    GitHubApiSweepOutcome,
)
from codex_dispatcher.release_handoff import (
    ReleaseHandoffError,
    record_release_handoff,
)
from codex_dispatcher.runner_reclamation_status import (
    build_runner_reclamation_status,
)
from codex_dispatcher.state_store import StateStore


COMMIT = "a" * 40
PREVIOUS = "b" * 40
IMAGE = "ghcr.io/longwdl/codex-cloud-task-scheduler-runner@sha256:" + "c" * 64
UPDATED = "2026-08-24T08:05:30Z"
NOW = datetime(2026, 8, 24, 8, 20, tzinfo=timezone.utc)


def _canonical(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(raw.encode()).hexdigest()


class _Commands:
    def __init__(
        self,
        *,
        backup: Path,
        timer_active: bool = True,
        reclamation_plan_ready: bool = False,
    ) -> None:
        self.calls = 0
        self.backup = backup
        self.timer_active = timer_active
        self.references: dict[str, object] = {
            "schema_version": 2,
            "kind": "runner_reclamation_rollback_references",
            "current_release_commit": COMMIT,
            "immediate_rollback_release_commit": PREVIOUS,
            "current_image_ref": IMAGE,
            "protected_release_commits": [COMMIT, PREVIOUS],
            "protected_image_refs": [IMAGE],
        }
        self.reference_receipt: dict[str, object] = {
            "schema_version": 1,
            "kind": "runner_reclamation_reference_change_receipt",
            "operation": "apply",
            "status": "applied",
            "release_commit": COMMIT,
            "previous_release_commit": PREVIOUS,
            "current_image_ref": IMAGE,
            "before_references": {},
            "before_sha256": "d" * 64,
            "after_references": self.references,
            "after_sha256": _canonical(self.references),
            "recorded_at": "2026-08-24T08:06:00Z",
        }
        self.status = build_runner_reclamation_status(
            host_available_bytes=74 * 1024**3,
            release_count=5 if reclamation_plan_ready else 4,
            release_target_count=3 if reclamation_plan_ready else 2,
            image_target_count=0,
            expected_total_bytes=1024,
            plan_sha256="e" * 64,
            now=datetime(2026, 8, 24, 8, 16, tzinfo=timezone.utc),
        ).to_mapping()

    def __call__(self, command: tuple[str, ...]) -> str:
        self.calls += 1
        if "/usr/bin/readlink" in command:
            return f"releases/{COMMIT}\n"
        if "/usr/bin/cat" in command:
            path = command[-1]
            if path.endswith("reclamation-rollback-references.json"):
                return json.dumps(self.references, sort_keys=True) + "\n"
            if path.endswith(f"{COMMIT}.apply.json"):
                return json.dumps(self.reference_receipt, sort_keys=True) + "\n"
            if path.endswith("reclamation-status/latest.json"):
                return json.dumps(self.status, sort_keys=True) + "\n"
            raise AssertionError(path)
        if "/usr/bin/systemctl" in command:
            unit = command[command.index("show") + 1]
            is_timer = unit.endswith(".timer")
            active = "active" if not is_timer or self.timer_active else "inactive"
            enabled = "enabled" if is_timer else "static"
            return "\n".join(
                (
                    f"Id={unit}",
                    f"ActiveState={active}",
                    f"UnitFileState={enabled}",
                    "Result=success",
                    "ExecMainStatus=0",
                )
            ) + "\n"
        if command[0] == "/usr/bin/journalctl":
            unit = command[command.index("-u") + 1]
            if unit.endswith("backup.service"):
                payload = {
                    "ok": True,
                    "state_backup": True,
                    "path": str(self.backup),
                    "integrity": "ok",
                }
            elif unit.endswith("restore-drill.service"):
                payload = {
                    "ok": True,
                    "state_restore_drill": True,
                    "source_path": str(self.backup),
                    "integrity": "ok",
                    "foreign_key_violations": 0,
                    "schema_migrations": list(range(1, 21)),
                    "temporary_restore_removed": True,
                }
            else:
                alerts = []
                notification: dict[str, object] = {"action": "healthy"}
                if self.status["trigger_reasons"]:
                    alerts = [
                        {
                            "code": "runner_reclamation_plan_ready",
                            "plan_sha256": self.status["plan_sha256"],
                        }
                    ]
                    notification = {
                        "action": "alert_opened",
                        "delivery_key": "slack-health:" + "f" * 64 + ":alert",
                        "permalink": "https://fixture.slack.com/archives/C0BS3LPG43G/p1",
                    }
                payload = {
                    "ok": True,
                    "lifecycle_health": True,
                    "checked_at": "2026-08-24T08:17:00Z",
                    "integrity": "ok",
                    "foreign_key_violations": 0,
                    "alert_count": len(alerts),
                    "alerts": alerts,
                    "alerts_truncated": False,
                    "active_turns": 0,
                    "work_items_total": 0,
                    "slack_notification": notification,
                }
            return json.dumps(payload, sort_keys=True) + "\n"
        raise AssertionError(command)


class ReleaseHandoffTests(unittest.TestCase):
    def _fixture(self, root: Path) -> dict[str, Path]:
        database = root / "state.db"
        with StateStore(database) as store:
            store.migrate()
            store.record_github_api_sweep(
                GitHubApiMetrics(4, 4, 0, 0, 20),
                started_at="2026-08-24T08:12:00+00:00",
                completed_at="2026-08-24T08:12:01+00:00",
                outcome=GitHubApiSweepOutcome.SUCCESS,
                sweep_status="idle",
            )
        backups = root / "backups"
        backups.mkdir(mode=0o700)
        backup = backups / "state-20260824T081541.833957Z.db"
        with StateStore(database, read_only=True) as store:
            store.backup(backup)
        backup.chmod(0o600)
        timestamp = datetime(2026, 8, 24, 8, 15, tzinfo=timezone.utc).timestamp()
        os.utime(backup, (timestamp, timestamp))
        receipts = root / "release-receipts"
        receipts.mkdir(mode=0o700)
        release = {
            "schema_version": 2,
            "kind": "codex_dispatcher_release",
            "operation": "apply",
            "status": "committed",
            "phase": "handoff_required",
            "committed": True,
            "runner_references_applied": True,
            "release_commit": COMMIT,
            "previous_runner": f"releases/{PREVIOUS}",
            "updated_at": UPDATED,
        }
        (receipts / f"{COMMIT}.json").write_text(json.dumps(release) + "\n")
        (receipts / f"{COMMIT}.json").chmod(0o600)
        current = root / "current"
        current.symlink_to(f"/opt/codex-dispatcher/releases/{COMMIT}")
        return {
            "database": database,
            "backups": backups,
            "backup": backup,
            "receipts": receipts,
            "handoffs": root / "handoffs",
            "current": current,
        }

    def test_records_complete_handoff_once_and_returns_it_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self._fixture(Path(temp_dir))
            commands = _Commands(backup=paths["backup"])
            with patch(
                "codex_dispatcher.release_handoff.pwd.getpwnam",
                return_value=SimpleNamespace(pw_uid=os.geteuid()),
            ):
                first = record_release_handoff(
                    COMMIT,
                    command_runner=commands,
                    now=NOW,
                    control_current=paths["current"],
                    release_receipt_root=paths["receipts"],
                    handoff_receipt_root=paths["handoffs"],
                    database_path=paths["database"],
                    backup_root=paths["backups"],
                )
            calls = commands.calls
            second = record_release_handoff(
                COMMIT,
                command_runner=lambda _: (_ for _ in ()).throw(AssertionError()),
                now=NOW,
                control_current=paths["current"],
                release_receipt_root=paths["receipts"],
                handoff_receipt_root=paths["handoffs"],
                database_path=paths["database"],
                backup_root=paths["backups"],
            )

            self.assertEqual(first, second)
            self.assertGreater(calls, 0)
            self.assertEqual("operational", first["status"])
            self.assertTrue(first["timers_started"])
            self.assertFalse(first["external_writes"])
            self.assertFalse(first["authorizes_reclamation_apply"])
            receipt = paths["handoffs"] / f"{COMMIT}.json"
            self.assertEqual(0o600, receipt.stat().st_mode & 0o777)

    def test_inactive_timer_fails_without_writing_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self._fixture(Path(temp_dir))
            commands = _Commands(backup=paths["backup"], timer_active=False)
            with self.assertRaisesRegex(ReleaseHandoffError, "timer handoff"):
                record_release_handoff(
                    COMMIT,
                    command_runner=commands,
                    now=NOW,
                    control_current=paths["current"],
                    release_receipt_root=paths["receipts"],
                    handoff_receipt_root=paths["handoffs"],
                    database_path=paths["database"],
                    backup_root=paths["backups"],
                )
            self.assertFalse((paths["handoffs"] / f"{COMMIT}.json").exists())

    def test_exact_plan_ready_alert_is_non_blocking_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self._fixture(Path(temp_dir))
            commands = _Commands(
                backup=paths["backup"],
                reclamation_plan_ready=True,
            )
            with patch(
                "codex_dispatcher.release_handoff.pwd.getpwnam",
                return_value=SimpleNamespace(pw_uid=os.geteuid()),
            ):
                receipt = record_release_handoff(
                    COMMIT,
                    command_runner=commands,
                    now=NOW,
                    control_current=paths["current"],
                    release_receipt_root=paths["receipts"],
                    handoff_receipt_root=paths["handoffs"],
                    database_path=paths["database"],
                    backup_root=paths["backups"],
                )

            health = receipt["lifecycle_health"]
            self.assertIsInstance(health, dict)
            assert isinstance(health, dict)
            self.assertEqual(1, health["alert_count"])
            self.assertEqual(
                "runner_reclamation_plan_ready",
                health["alerts"][0]["code"],  # type: ignore[index]
            )


if __name__ == "__main__":
    unittest.main()
