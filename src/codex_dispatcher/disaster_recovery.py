"""Current-schema disaster-recovery rehearsal and exact external reconciliation."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from codex_dispatcher.control_host_backup import drill_latest_state_backup
from codex_dispatcher.runner_reclamation_status import RunnerReclamationStatus
from codex_dispatcher.runner_transport import (
    RunnerArchiveState,
    parse_runner_archive_reply,
)
from codex_dispatcher.slack_reporting import (
    SlackDeliveryReceipt,
    SlackDeliveryState,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.terminal_retention import TerminalBranchCleanupState
from codex_dispatcher.trackers.base import (
    PullRequestState,
    TaskState,
    Tracker,
)
from codex_dispatcher.work_item_lifecycle import (
    WorkItemArchiveStatus,
    WorkItemDispositionKind,
)
from codex_dispatcher.work_items import WorkItemState, validate_work_item_id


_SCHEMA_VERSION = 2
_EXPECTED_DATABASE_SCHEMA = tuple(range(1, 21))
_RELEASE_FILES = (
    "scripts/codex-dispatcher-v1",
    "scripts/codex-dispatcher-backup-v1",
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
)
_RUNNER_REFERENCES = Path(
    "/srv/codex-runner/etc/reclamation-rollback-references.json"
)
_RUNNER_REFERENCE_RECEIPTS = Path(
    "/srv/codex-runner/reclamation-reference-receipts"
)
_RUNNER_RECLAMATION_STATUS = Path(
    "/srv/codex-runner/reclamation-status/latest.json"
)
_RUNNER_PLANNER_UNITS = (
    "codex-runner-reclamation-plan.service",
    "codex-runner-reclamation-plan.timer",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")


class DisasterRecoveryError(RuntimeError):
    """Raised when any isolated recovery or external read-back is incomplete."""


class SlackReceiptVerifier(Protocol):
    def verify_receipt(self, receipt: SlackDeliveryReceipt) -> SlackDeliveryReceipt: ...


@dataclass(frozen=True, slots=True)
class DisasterRecoveryResult:
    recovery_root: Path
    receipt_path: Path
    source_backup: Path
    source_backup_sha256: str
    source_age_seconds: int
    release_commit: str
    release_receipt_sha256: str
    handoff_receipt_sha256: str
    runner_snapshot_sha256: str
    runner_references_sha256: str
    runner_reference_receipt_sha256: str
    runner_reclamation_status_sha256: str
    control_config_sha256: str
    previous_control_commit: str
    previous_runner_commit: str
    restored_database_sha256: str
    rebuild_manifest_sha256: str
    runner_rebuild_manifest_sha256: str
    work_item_count: int
    runner_archive_count: int
    runner_absence_count: int
    runner_orphan_terminal_count: int
    github_issue_count: int
    github_pr_count: int
    slack_receipt_count: int
    rto_milliseconds: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "kind": "schema18_disaster_recovery_drill",
            "status": "passed",
            "recovery_root": str(self.recovery_root),
            "source_backup": str(self.source_backup),
            "source_backup_sha256": self.source_backup_sha256,
            "source_age_seconds": self.source_age_seconds,
            "release_commit": self.release_commit,
            "release_receipt_sha256": self.release_receipt_sha256,
            "handoff_receipt_sha256": self.handoff_receipt_sha256,
            "runner_snapshot_sha256": self.runner_snapshot_sha256,
            "runner_references_sha256": self.runner_references_sha256,
            "runner_reference_receipt_sha256": self.runner_reference_receipt_sha256,
            "runner_reclamation_status_sha256": self.runner_reclamation_status_sha256,
            "control_config_sha256": self.control_config_sha256,
            "previous_control_commit": self.previous_control_commit,
            "previous_runner_commit": self.previous_runner_commit,
            "restored_database_sha256": self.restored_database_sha256,
            "rebuild_manifest_sha256": self.rebuild_manifest_sha256,
            "runner_rebuild_manifest_sha256": self.runner_rebuild_manifest_sha256,
            "database_schema_migrations": list(_EXPECTED_DATABASE_SCHEMA),
            "work_item_count": self.work_item_count,
            "runner_archive_count": self.runner_archive_count,
            "runner_absence_count": self.runner_absence_count,
            "runner_orphan_terminal_count": self.runner_orphan_terminal_count,
            "github_issue_count": self.github_issue_count,
            "github_pr_count": self.github_pr_count,
            "slack_receipt_count": self.slack_receipt_count,
            "rto_milliseconds": self.rto_milliseconds,
            "rto_scope": (
                "latest-backup selection through isolated schema-20 restore, "
                "Control and Runner application-filesystem rebuilds, and exact "
                "Runner/GitHub/Slack read-back"
            ),
            "infrastructure_provisioning_rto_measured": False,
            "online_state_modified": False,
            "recovery_commands": [
                "sudo systemctl stop codex-dispatcher.timer codex-dispatcher-health.timer",
                f"git clone --filter=blob:none https://github.com/longwdl/"
                f"codex_cloud_task_scheduler.git && git -C codex_cloud_task_scheduler "
                f"checkout --detach {self.release_commit}",
                f"restore {self.source_backup} to a new state.db candidate with "
                "the SQLite Online Backup API; never overwrite the online database in place",
                "install the exact release on Runner first and Control second",
                "run the schema18 disaster-recovery reconciliation before enabling writes",
                "run codex-dispatcher ssh-preflight --config "
                "/etc/codex-dispatcher/config.toml --json",
            ],
            "failure_rollback_boundary": [
                "never replace the online Control database during the drill",
                "never switch either live current symlink during the drill",
                "never start, resume, stop, or archive a Runner Turn during reconciliation",
                "on failure retain the source backup and remove only this exact recovery_root",
            ],
        }


def collect_runner_recovery_snapshot(
    *,
    work_items_root: Path,
    current_release_commit: str,
    trusted_owner_uid: int | None = None,
    release_references_path: Path = _RUNNER_REFERENCES,
    release_reference_receipt_path: Path | None = None,
    reclamation_status_path: Path = _RUNNER_RECLAMATION_STATUS,
    installed_unit_root: Path = Path("/etc/systemd/system"),
    current_release_path: Path = Path("/srv/codex-runner/current"),
) -> dict[str, object]:
    """Collect bounded Runner lifecycle, release, and planner recovery evidence."""
    _validate_commit(current_release_commit, "current_release_commit")
    if trusted_owner_uid is not None and (
        type(trusted_owner_uid) is not int or trusted_owner_uid < 0
    ):
        raise ValueError("trusted_owner_uid must be a non-negative integer or None")
    _protected_directory(
        work_items_root, "Runner work-items root", trusted_owner_uid=trusted_owner_uid
    )
    registry = _json_directory(
        work_items_root / ".registry", trusted_owner_uid=trusted_owner_uid
    )
    archives = _json_directory(
        work_items_root / ".archives", trusted_owner_uid=trusted_owner_uid
    )
    absences = _json_directory(
        work_items_root / ".absences", trusted_owner_uid=trusted_owner_uid
    )

    registry_ids: list[str] = []
    for path, payload, _ in registry:
        work_item_id = validate_work_item_id(payload.get("work_item_id"))
        if path.stem != work_item_id:
            raise DisasterRecoveryError("Runner registry filename conflicts with identity")
        registry_ids.append(work_item_id)

    archive_rows: list[dict[str, object]] = []
    for path, payload, digest in archives:
        work_item_id = validate_work_item_id(payload.get("work_item_id"))
        if path.stem != work_item_id or payload.get("state") != "archived":
            raise DisasterRecoveryError("Runner archive tombstone is not terminal and exact")
        row = {
            "work_item_id": work_item_id,
            "expected_head_sha": payload.get("expected_head_sha"),
            "reclaimed_bytes": payload.get("reclaimed_bytes"),
            "archived_at": payload.get("archived_at"),
            "storage_kind": payload.get("storage_kind"),
            "file_sha256": digest,
        }
        archive_rows.append(row)

    absence_rows: list[dict[str, object]] = []
    for path, payload, digest in absences:
        work_item_id = validate_work_item_id(payload.get("work_item_id"))
        if path.stem != work_item_id:
            raise DisasterRecoveryError("Runner absence filename conflicts with identity")
        absence_rows.append(
            {
                "work_item_id": work_item_id,
                "expected_head_sha": payload.get("expected_head_sha"),
                "evidence_sha256": digest,
            }
        )

    reference_receipt_path = (
        _RUNNER_REFERENCE_RECEIPTS / f"{current_release_commit}.apply.json"
        if release_reference_receipt_path is None
        else release_reference_receipt_path
    )
    references = _read_json_file(
        release_references_path,
        "Runner release references",
        maximum=64 * 1024,
        trusted_owner_uid=0,
    )
    reference_receipt = _read_json_file(
        reference_receipt_path,
        "Runner release reference receipt",
        maximum=128 * 1024,
        trusted_owner_uid=0,
    )
    reclamation_status = _read_json_file(
        reclamation_status_path,
        "Runner reclamation status",
        maximum=64 * 1024,
        trusted_owner_uid=0,
    )
    planner_units: dict[str, str] = {}
    for unit in _RUNNER_PLANNER_UNITS:
        installed = installed_unit_root / unit
        release_unit = current_release_path / "deploy/runner" / unit
        installed_sha = _sha256_file(installed)
        if installed_sha != _sha256_file(release_unit):
            raise DisasterRecoveryError("installed Runner planner unit differs from release")
        planner_units[unit] = installed_sha

    snapshot = {
        "schema_version": _SCHEMA_VERSION,
        "kind": "runner_recovery_snapshot",
        "current_release_commit": current_release_commit,
        "registry_work_item_ids": sorted(registry_ids),
        "archives": sorted(archive_rows, key=lambda row: str(row["work_item_id"])),
        "absences": sorted(absence_rows, key=lambda row: str(row["work_item_id"])),
        "release_references": references,
        "release_references_sha256": _sha256_file(release_references_path),
        "release_reference_receipt": reference_receipt,
        "release_reference_receipt_sha256": _sha256_file(reference_receipt_path),
        "reclamation_status": reclamation_status,
        "reclamation_status_sha256": _sha256_file(reclamation_status_path),
        "planner_unit_sha256": planner_units,
    }
    _validate_runner_snapshot(snapshot)
    return snapshot


def load_runner_recovery_snapshot(path: Path) -> dict[str, object]:
    """Read one protected bounded Runner snapshot copied to the Control Host."""
    payload = _read_json_file(path, "Runner recovery snapshot", maximum=1024 * 1024)
    _validate_runner_snapshot(payload)
    return payload


def run_schema18_disaster_recovery_drill(
    *,
    database_path: Path,
    backup_directory: Path,
    recovery_root: Path,
    control_release_path: Path,
    control_config_path: Path,
    release_receipt_path: Path,
    handoff_receipt_path: Path,
    runner_snapshot: dict[str, object],
    tracker: Tracker,
    slack_verifier: SlackReceiptVerifier | None,
    now: datetime | None = None,
) -> DisasterRecoveryResult:
    """Restore and reconcile one exact schema-20 snapshot without touching live state."""
    started = time.monotonic_ns()
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("disaster-recovery timestamp must be timezone-aware")
    _prepare_new_recovery_root(recovery_root)
    receipt_path = recovery_root / "receipt.json"
    try:
        restore_evidence = drill_latest_state_backup(
            database_path, backup_directory, now=moment
        )
        restored_database = recovery_root / "restored-state.db"
        with StateStore(restore_evidence.source_path, read_only=True) as source:
            source.backup(restored_database)
        os.chmod(restored_database, 0o600, follow_symlinks=False)
        _fsync_file(restored_database)

        release = _read_release_receipt(release_receipt_path)
        release_commit = _validate_commit(release["release_commit"], "release_commit")
        handoff = _read_handoff_receipt(handoff_receipt_path, release_commit)
        release_receipt_sha256 = _sha256_file(release_receipt_path)
        if handoff.get("release_receipt_sha256") != release_receipt_sha256:
            raise DisasterRecoveryError("handoff receipt release digest conflicts")
        if control_release_path.name != release_commit:
            raise DisasterRecoveryError("Control release path conflicts with receipt")
        _validate_release_tree(control_release_path)
        _validate_protected_file(control_config_path, "Control config", maximum=256 * 1024)
        runner_commit = _validate_runner_snapshot(runner_snapshot)
        if runner_commit != release_commit:
            raise DisasterRecoveryError("Control and Runner release commits differ")
        _verify_handoff_runner_evidence(handoff, runner_snapshot)
        previous_control = _release_commit_from_path(
            release.get("previous_control"), "previous_control"
        )
        previous_runner = _release_commit_from_path(
            release.get("previous_runner"), "previous_runner"
        )

        with StateStore(restored_database, read_only=True) as restored:
            if restored.integrity_check() != "ok":
                raise DisasterRecoveryError("restored database integrity check failed")
            if restored.foreign_key_violation_count():
                raise DisasterRecoveryError("restored database has foreign-key violations")
            if restored.schema_migration_versions() != _EXPECTED_DATABASE_SCHEMA:
                raise DisasterRecoveryError("restored database is not exact schema 20")
            work_items = restored.list_work_items()
            archive_count, absence_count, orphan_terminal_count = _verify_runner_snapshot(
                restored, runner_snapshot
            )
            github_issue_count, github_pr_count = _reconcile_github(
                restored, tracker
            )
            slack_count = _reconcile_slack(restored, slack_verifier)

        rebuild_manifest, runner_rebuild_manifest = _rebuild_empty_hosts(
            recovery_root=recovery_root,
            release_path=control_release_path,
            release_commit=release_commit,
            restored_database=restored_database,
            control_config=control_config_path,
            release_receipt=release_receipt_path,
            handoff_receipt=handoff_receipt_path,
            runner_snapshot=runner_snapshot,
        )
        rto_milliseconds = max(0, (time.monotonic_ns() - started) // 1_000_000)
        result = DisasterRecoveryResult(
            recovery_root=recovery_root,
            receipt_path=receipt_path,
            source_backup=restore_evidence.source_path,
            source_backup_sha256=_sha256_file(restore_evidence.source_path),
            source_age_seconds=restore_evidence.source_age_seconds,
            release_commit=release_commit,
            release_receipt_sha256=release_receipt_sha256,
            handoff_receipt_sha256=_sha256_file(handoff_receipt_path),
            runner_snapshot_sha256=_canonical_sha256(runner_snapshot),
            runner_references_sha256=str(
                runner_snapshot["release_references_sha256"]
            ),
            runner_reference_receipt_sha256=str(
                runner_snapshot["release_reference_receipt_sha256"]
            ),
            runner_reclamation_status_sha256=str(
                runner_snapshot["reclamation_status_sha256"]
            ),
            control_config_sha256=_sha256_file(control_config_path),
            previous_control_commit=previous_control,
            previous_runner_commit=previous_runner,
            restored_database_sha256=_sha256_file(restored_database),
            rebuild_manifest_sha256=rebuild_manifest,
            runner_rebuild_manifest_sha256=runner_rebuild_manifest,
            work_item_count=len(work_items),
            runner_archive_count=archive_count,
            runner_absence_count=absence_count,
            runner_orphan_terminal_count=orphan_terminal_count,
            github_issue_count=github_issue_count,
            github_pr_count=github_pr_count,
            slack_receipt_count=slack_count,
            rto_milliseconds=rto_milliseconds,
        )
        _write_json_atomic(receipt_path, result.to_mapping())
        return result
    except BaseException:
        failed = {
            "schema_version": _SCHEMA_VERSION,
            "kind": "schema18_disaster_recovery_drill",
            "status": "failed",
            "online_state_modified": False,
            "recovery_root": str(recovery_root),
            "rollback": "remove only this exact recovery_root after inspection",
        }
        try:
            _write_json_atomic(recovery_root / "failed-receipt.json", failed)
        except OSError:
            pass
        raise


def _verify_runner_snapshot(
    store: StateStore, snapshot: dict[str, object]
) -> tuple[int, int, int]:
    registry_ids = _string_set(snapshot.get("registry_work_item_ids"), "registry ids")
    archive_rows = _indexed_rows(snapshot.get("archives"), "Runner archives")
    absence_rows = _indexed_rows(snapshot.get("absences"), "Runner absences")
    work_item_ids = {item.work_item_id for item in store.list_work_items()}
    absences = {
        record.work_item_id: record
        for record in store.list_work_item_absence_reconciliations()
    }
    archive_ids = set(archive_rows)
    absence_ids = set(absence_rows)
    if archive_ids & absence_ids:
        raise DisasterRecoveryError("Runner terminal evidence sets overlap")
    orphan_archive_ids = archive_ids - work_item_ids
    orphan_absence_ids = absence_ids - work_item_ids
    expected_registry = (work_item_ids - set(absences)) | orphan_archive_ids
    if registry_ids != expected_registry:
        raise DisasterRecoveryError("Runner registry set differs from restored SQLite")

    archives = {
        record.work_item_id: record
        for record in store.list_work_item_archives()
        if record.status is WorkItemArchiveStatus.ARCHIVED
    }
    if archive_ids & work_item_ids != set(archives):
        raise DisasterRecoveryError("Runner archive set differs from restored SQLite")
    for work_item_id, record in archives.items():
        assert record.response_json is not None
        reply = parse_runner_archive_reply(record.response_json.encode("utf-8"))
        row = archive_rows[work_item_id]
        if (
            reply.state is not RunnerArchiveState.ARCHIVED
            or row.get("expected_head_sha") != record.expected_head_sha
            or row.get("reclaimed_bytes") != record.reclaimed_bytes
            or row.get("archived_at") != record.runner_archived_at
            or not isinstance(row.get("file_sha256"), str)
        ):
            raise DisasterRecoveryError("Runner archive tombstone conflicts with SQLite")

    if absence_ids & work_item_ids != set(absences):
        raise DisasterRecoveryError("Runner absence set differs from restored SQLite")
    for work_item_id, record in absences.items():
        row = absence_rows[work_item_id]
        if (
            row.get("expected_head_sha") != record.expected_head_sha
            or row.get("evidence_sha256") != record.evidence_sha256
        ):
            raise DisasterRecoveryError("Runner absence receipt conflicts with SQLite")
    return len(archives), len(absences), len(orphan_archive_ids | orphan_absence_ids)


def _reconcile_github(store: StateStore, tracker: Tracker) -> tuple[int, int]:
    issue_count = 0
    pr_count = 0
    dispositions = {
        record.work_item_id: record for record in store.list_work_item_dispositions()
    }
    runner_reclaimed = {
        record.work_item_id
        for record in store.list_work_item_archives()
        if record.status is WorkItemArchiveStatus.ARCHIVED
    } | {
        record.work_item_id
        for record in store.list_work_item_absence_reconciliations()
    }
    for work_item in store.list_work_items():
        task = tracker.get_task(work_item.repository, str(work_item.issue_number))
        if (
            task is None
            or task.repository != work_item.repository
            or task.issue_number != work_item.issue_number
            or task.issue_node_id != work_item.issue_node_id
            or not task.is_open
        ):
            raise DisasterRecoveryError("GitHub Issue identity conflicts with SQLite")
        disposition = dispositions.get(work_item.work_item_id)
        expected_state = (
            TaskState.DISCARD
            if disposition is not None
            else _task_state_for_work_item(work_item.state)
        )
        if task.state is not expected_state:
            raise DisasterRecoveryError("GitHub Issue state conflicts with SQLite")
        issue_count += 1

        pull_request = tracker.find_pr_by_branch(
            work_item.repository, work_item.task_branch
        )
        if work_item.pr_number is None:
            if pull_request is not None:
                raise DisasterRecoveryError("unbound GitHub Pull Request exists")
        else:
            expected_head = work_item.last_published_sha
            if (
                pull_request is None
                or pull_request.number != work_item.pr_number
                or pull_request.branch_name != work_item.task_branch
                or pull_request.head_sha != expected_head
            ):
                raise DisasterRecoveryError("GitHub Pull Request conflicts with SQLite")
            expected_pr_state = PullRequestState.OPEN
            if work_item.state is WorkItemState.COMPLETED:
                expected_pr_state = PullRequestState.MERGED
            elif (
                disposition is not None
                and disposition.kind is WorkItemDispositionKind.SUPERSEDED
            ):
                expected_pr_state = PullRequestState.CLOSED
            if pull_request.state is not expected_pr_state:
                raise DisasterRecoveryError("GitHub Pull Request state conflicts with SQLite")
            pr_count += 1

        cleanup = store.get_terminal_branch_cleanup(work_item.work_item_id)
        branch_head = tracker.get_branch_head(
            work_item.repository, work_item.task_branch
        )
        if cleanup is not None and cleanup.state is TerminalBranchCleanupState.COMPLETED:
            if branch_head is not None:
                raise DisasterRecoveryError("reclaimed GitHub branch exists again")
        elif work_item.last_published_sha is not None:
            terminal = (
                work_item.state is WorkItemState.COMPLETED
                or disposition is not None
            )
            if branch_head is None:
                if not terminal or work_item.work_item_id not in runner_reclaimed:
                    raise DisasterRecoveryError(
                        "GitHub task branch is missing without terminal Runner evidence "
                        f"for Issue #{work_item.issue_number}"
                    )
            elif branch_head != work_item.last_published_sha:
                raise DisasterRecoveryError(
                    "GitHub task branch conflicts with SQLite for "
                    f"Issue #{work_item.issue_number}"
                )
        elif branch_head not in {None, work_item.base_sha}:
            raise DisasterRecoveryError(
                "unpublished GitHub task branch differs from its persisted base for "
                f"Issue #{work_item.issue_number}"
            )
    return issue_count, pr_count


def _reconcile_slack(
    store: StateStore, verifier: SlackReceiptVerifier | None
) -> int:
    deliveries = store.list_slack_deliveries()
    if not deliveries:
        return 0
    if verifier is None:
        raise DisasterRecoveryError("Slack receipts exist but no verifier is configured")
    count = 0
    for delivery in deliveries:
        if (
            delivery.state is not SlackDeliveryState.DELIVERED
            or delivery.message_ts is None
            or delivery.permalink is None
        ):
            raise DisasterRecoveryError("Slack outbox contains an unfinished delivery")
        thread_ts = (
            delivery.message_ts
            if delivery.thread_ts is None
            else delivery.thread_ts
        )
        receipt = SlackDeliveryReceipt(
            delivery.deduplication_key,
            delivery.channel_id,
            delivery.message_ts,
            thread_ts,
            delivery.permalink,
        )
        if verifier.verify_receipt(receipt) != receipt:
            raise DisasterRecoveryError("Slack receipt verifier returned conflicting evidence")
        count += 1
    return count


def _task_state_for_work_item(state: WorkItemState) -> TaskState:
    mapping = {
        WorkItemState.DISCOVERED: TaskState.READY,
        WorkItemState.PREPARING: TaskState.DISPATCHING,
        WorkItemState.READY: TaskState.DISPATCHING,
        WorkItemState.RUNNING: TaskState.RUNNING,
        WorkItemState.WAITING_INPUT: TaskState.NEEDS_INPUT,
        WorkItemState.REVIEW: TaskState.REVIEW,
        WorkItemState.COMPLETED: TaskState.COMPLETED,
        WorkItemState.BLOCKED: TaskState.BLOCKED,
        WorkItemState.PAUSED: TaskState.PAUSED,
    }
    return mapping[state]


def _rebuild_empty_hosts(
    *,
    recovery_root: Path,
    release_path: Path,
    release_commit: str,
    restored_database: Path,
    control_config: Path,
    release_receipt: Path,
    handoff_receipt: Path,
    runner_snapshot: dict[str, object],
) -> tuple[str, str]:
    empty_root = recovery_root / "empty-control-host"
    empty_root.mkdir(mode=0o700)
    release_target = (
        empty_root / "opt/codex-dispatcher/releases" / release_commit
    )
    release_target.parent.mkdir(mode=0o755, parents=True)
    shutil.copytree(release_path, release_target, symlinks=False)
    current = empty_root / "opt/codex-dispatcher/current"
    current.symlink_to(f"releases/{release_commit}")
    config_target = empty_root / "etc/codex-dispatcher/config.toml"
    config_target.parent.mkdir(mode=0o700, parents=True)
    shutil.copy2(control_config, config_target, follow_symlinks=False)
    os.chmod(config_target, 0o600, follow_symlinks=False)
    state_target = empty_root / "var/lib/codex-dispatcher/state.db"
    state_target.parent.mkdir(mode=0o700, parents=True)
    with StateStore(restored_database, read_only=True) as source:
        source.backup(state_target)
    os.chmod(state_target, 0o600, follow_symlinks=False)
    control_receipt_root = empty_root / "opt/codex-dispatcher/release-receipts"
    control_receipt_root.mkdir(mode=0o700, parents=True)
    shutil.copy2(
        release_receipt,
        control_receipt_root / f"{release_commit}.json",
        follow_symlinks=False,
    )
    handoff_root = empty_root / "opt/codex-dispatcher/release-handoff-receipts"
    handoff_root.mkdir(mode=0o700, parents=True)
    shutil.copy2(
        handoff_receipt,
        handoff_root / f"{release_commit}.json",
        follow_symlinks=False,
    )
    unit_root = empty_root / "etc/systemd/system"
    unit_root.mkdir(mode=0o755, parents=True)
    for relative in _RELEASE_FILES:
        if relative.startswith("deploy/systemd/"):
            source = release_target / relative
            shutil.copy2(source, unit_root / source.name, follow_symlinks=False)
    control_manifest_sha = _write_tree_manifest(
        empty_root, recovery_root / "empty-control-host-manifest.json"
    )

    runner_root = recovery_root / "empty-runner-host"
    runner_release = runner_root / "srv/codex-runner/releases" / release_commit
    runner_release.parent.mkdir(mode=0o755, parents=True)
    shutil.copytree(release_path, runner_release, symlinks=False)
    runner_current = runner_root / "srv/codex-runner/current"
    runner_current.symlink_to(f"releases/{release_commit}")
    runner_etc = runner_root / "srv/codex-runner/etc"
    runner_etc.mkdir(mode=0o700, parents=True)
    references = runner_snapshot.get("release_references")
    reference_receipt_payload = runner_snapshot.get("release_reference_receipt")
    reclamation_status = runner_snapshot.get("reclamation_status")
    if not all(
        isinstance(value, dict)
        for value in (references, reference_receipt_payload, reclamation_status)
    ):
        raise DisasterRecoveryError("Runner recovery artifacts are incomplete")
    assert isinstance(references, dict)
    assert isinstance(reference_receipt_payload, dict)
    assert isinstance(reclamation_status, dict)
    reference_target = runner_etc / "reclamation-rollback-references.json"
    _write_json_atomic(reference_target, references)
    reference_receipt_root = (
        runner_root / "srv/codex-runner/reclamation-reference-receipts"
    )
    reference_receipt_root.mkdir(mode=0o700, parents=True)
    reference_receipt_target = reference_receipt_root / f"{release_commit}.apply.json"
    _write_json_atomic(reference_receipt_target, reference_receipt_payload)
    status_root = runner_root / "srv/codex-runner/reclamation-status"
    status_root.mkdir(mode=0o750, parents=True)
    status_target = status_root / "latest.json"
    _write_json_atomic(status_target, reclamation_status)
    for path in (reference_target, reference_receipt_target, status_target):
        os.chmod(path, 0o600, follow_symlinks=False)
    if (
        _sha256_file(reference_target)
        != runner_snapshot.get("release_references_sha256")
        or _sha256_file(reference_receipt_target)
        != runner_snapshot.get("release_reference_receipt_sha256")
        or _sha256_file(status_target)
        != runner_snapshot.get("reclamation_status_sha256")
    ):
        raise DisasterRecoveryError("rebuilt Runner artifact digest conflicts")
    runner_units = runner_root / "etc/systemd/system"
    runner_units.mkdir(mode=0o755, parents=True)
    planner_units = runner_snapshot.get("planner_unit_sha256")
    if not isinstance(planner_units, dict):
        raise DisasterRecoveryError("Runner planner unit evidence is incomplete")
    for unit in _RUNNER_PLANNER_UNITS:
        source = runner_release / "deploy/runner" / unit
        target = runner_units / unit
        shutil.copy2(source, target, follow_symlinks=False)
        if _sha256_file(target) != planner_units.get(unit):
            raise DisasterRecoveryError("rebuilt Runner planner unit conflicts")
    runner_manifest_sha = _write_tree_manifest(
        runner_root, recovery_root / "empty-runner-host-manifest.json"
    )
    return control_manifest_sha, runner_manifest_sha


def _write_tree_manifest(root: Path, manifest_path: Path) -> str:
    manifest: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            manifest.append({"path": relative, "symlink": os.readlink(path)})
        elif path.is_file():
            manifest.append(
                {
                    "path": relative,
                    "size_bytes": path.stat(follow_symlinks=False).st_size,
                    "sha256": _sha256_file(path),
                }
            )
    canonical = json.dumps(
        manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    _write_bytes_atomic(manifest_path, canonical + b"\n")
    return sha256(canonical).hexdigest()


def _read_release_receipt(path: Path) -> dict[str, Any]:
    payload = _read_json_file(path, "release receipt", maximum=64 * 1024)
    if (
        payload.get("schema_version") != 2
        or payload.get("kind") != "codex_dispatcher_release"
        or payload.get("operation") != "apply"
        or payload.get("status") != "committed"
        or payload.get("phase") != "handoff_required"
        or payload.get("committed") is not True
    ):
        raise DisasterRecoveryError("release receipt is not a committed handoff")
    return payload


def _read_handoff_receipt(path: Path, release_commit: str) -> dict[str, Any]:
    payload = _read_json_file(path, "release handoff receipt", maximum=256 * 1024)
    evidence = payload.get("evidence_sha256")
    body = dict(payload)
    body.pop("evidence_sha256", None)
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != "codex_dispatcher_release_handoff"
        or payload.get("status") != "operational"
        or payload.get("release_commit") != release_commit
        or payload.get("timers_started") is not True
        or payload.get("external_writes") is not False
        or payload.get("authorizes_reclamation_apply") is not False
        or evidence != _canonical_sha256(body)
    ):
        raise DisasterRecoveryError("release handoff receipt is invalid")
    return payload


def _verify_handoff_runner_evidence(
    handoff: dict[str, Any], snapshot: dict[str, object]
) -> None:
    if (
        handoff.get("runner_references_sha256")
        != snapshot.get("release_references_sha256")
        or handoff.get("runner_reference_receipt_sha256")
        != snapshot.get("release_reference_receipt_sha256")
    ):
        raise DisasterRecoveryError("handoff and Runner recovery evidence conflict")


def _validate_runner_snapshot(snapshot: dict[str, object]) -> str:
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("schema_version") != _SCHEMA_VERSION
        or snapshot.get("kind") != "runner_recovery_snapshot"
    ):
        raise DisasterRecoveryError("Runner recovery snapshot is invalid")
    commit = _validate_commit(
        snapshot.get("current_release_commit"), "Runner current release"
    )
    references = snapshot.get("release_references")
    reference_receipt = snapshot.get("release_reference_receipt")
    status_payload = snapshot.get("reclamation_status")
    unit_sha = snapshot.get("planner_unit_sha256")
    if not all(
        isinstance(value, dict)
        for value in (references, reference_receipt, status_payload, unit_sha)
    ):
        raise DisasterRecoveryError("Runner operational recovery evidence is incomplete")
    assert isinstance(references, dict)
    assert isinstance(reference_receipt, dict)
    assert isinstance(status_payload, dict)
    assert isinstance(unit_sha, dict)
    rollback = references.get("immediate_rollback_release_commit")
    image = references.get("current_image_ref")
    try:
        rollback_commit = _validate_commit(rollback, "Runner rollback release")
    except ValueError as exc:
        raise DisasterRecoveryError(
            "Runner release reference evidence is inconsistent"
        ) from exc
    if (
        references.get("schema_version") != 2
        or references.get("kind") != "runner_reclamation_rollback_references"
        or references.get("current_release_commit") != commit
        or references.get("protected_release_commits") != [commit, rollback_commit]
        or not isinstance(image, str)
        or _IMAGE_RE.fullmatch(image) is None
        or references.get("protected_image_refs") != [image]
        or reference_receipt.get("schema_version") != 1
        or reference_receipt.get("kind")
        != "runner_reclamation_reference_change_receipt"
        or reference_receipt.get("operation") != "apply"
        or reference_receipt.get("status") != "applied"
        or reference_receipt.get("release_commit") != commit
        or reference_receipt.get("previous_release_commit") != rollback_commit
        or reference_receipt.get("after_references") != references
        or reference_receipt.get("after_sha256") != _canonical_sha256(references)
    ):
        raise DisasterRecoveryError("Runner release reference evidence is inconsistent")
    RunnerReclamationStatus.from_mapping(status_payload)
    for field, payload in (
        ("release_references_sha256", references),
        ("release_reference_receipt_sha256", reference_receipt),
        ("reclamation_status_sha256", status_payload),
    ):
        digest = snapshot.get(field)
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise DisasterRecoveryError("Runner artifact digest is invalid")
        canonical_file = (
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        if sha256(canonical_file).hexdigest() != digest:
            raise DisasterRecoveryError("Runner artifact file digest is inconsistent")
    if set(unit_sha) != set(_RUNNER_PLANNER_UNITS) or any(
        not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
        for value in unit_sha.values()
    ):
        raise DisasterRecoveryError("Runner planner unit evidence is invalid")
    return commit


def _validate_release_tree(path: Path) -> None:
    _protected_directory(path, "Control release")
    root_device = path.stat(follow_symlinks=False).st_dev
    for candidate in sorted(path.rglob("*")):
        try:
            metadata = candidate.stat(follow_symlinks=False)
        except OSError as exc:
            raise DisasterRecoveryError("Control release tree is unavailable") from exc
        if (
            candidate.is_symlink()
            or metadata.st_dev != root_device
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_mode & 0o022
            or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
        ):
            raise DisasterRecoveryError("Control release tree contains an unsafe entry")
    for relative in _RELEASE_FILES:
        candidate = path / relative
        try:
            metadata = candidate.stat(follow_symlinks=False)
        except OSError as exc:
            raise DisasterRecoveryError("Control release is incomplete") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or candidate.is_symlink()
            or metadata.st_mode & 0o022
        ):
            raise DisasterRecoveryError("Control release contains unsafe required files")


def _prepare_new_recovery_root(path: Path) -> None:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise ValueError("recovery_root must be a normalized absolute path")
    _protected_directory(path.parent, "recovery parent")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise DisasterRecoveryError("recovery_root already exists") from exc


def _json_directory(
    path: Path, *, trusted_owner_uid: int | None
) -> tuple[tuple[Path, dict[str, Any], str], ...]:
    _protected_directory(
        path, "Runner evidence directory", trusted_owner_uid=trusted_owner_uid
    )
    rows: list[tuple[Path, dict[str, Any], str]] = []
    for child in sorted(path.iterdir()):
        if child.suffix != ".json":
            raise DisasterRecoveryError("Runner evidence directory has an unknown entry")
        payload = _read_json_file(
            child,
            "Runner evidence",
            maximum=16 * 1024,
            trusted_owner_uid=trusted_owner_uid,
        )
        rows.append((child, payload, _sha256_file(child)))
    return tuple(rows)


def _read_json_file(
    path: Path,
    field: str,
    *,
    maximum: int,
    trusted_owner_uid: int | None = None,
) -> dict[str, Any]:
    raw = _validate_protected_file(
        path, field, maximum=maximum, trusted_owner_uid=trusted_owner_uid
    )
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DisasterRecoveryError(f"{field} is malformed") from exc
    if not isinstance(payload, dict):
        raise DisasterRecoveryError(f"{field} must be a JSON object")
    return payload


def _validate_protected_file(
    path: Path,
    field: str,
    *,
    maximum: int,
    trusted_owner_uid: int | None = None,
) -> bytes:
    try:
        metadata = path.stat(follow_symlinks=False)
        raw = path.read_bytes()
    except OSError as exc:
        raise DisasterRecoveryError(f"{field} is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid not in {
            0,
            os.geteuid(),
            *(set() if trusted_owner_uid is None else {trusted_owner_uid}),
        }
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or not 0 < len(raw) <= maximum
        or b"\x00" in raw
    ):
        raise DisasterRecoveryError(f"{field} exceeds its protected boundary")
    return raw


def _indexed_rows(value: object, field: str) -> dict[str, dict[str, object]]:
    if not isinstance(value, list):
        raise DisasterRecoveryError(f"{field} must be a list")
    result: dict[str, dict[str, object]] = {}
    for row in value:
        if not isinstance(row, dict):
            raise DisasterRecoveryError(f"{field} contains an invalid row")
        work_item_id = validate_work_item_id(row.get("work_item_id"))
        if work_item_id in result:
            raise DisasterRecoveryError(f"{field} contains a duplicate identity")
        result[work_item_id] = row
    return result


def _string_set(value: object, field: str) -> set[str]:
    if not isinstance(value, list):
        raise DisasterRecoveryError(f"{field} must be a list")
    result = {validate_work_item_id(item) for item in value}
    if len(result) != len(value):
        raise DisasterRecoveryError(f"{field} contains duplicate identities")
    return result


def _release_commit_from_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise DisasterRecoveryError(f"{field} is invalid")
    return _validate_commit(value.rstrip("/").split("/")[-1], field)


def _validate_commit(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DisasterRecoveryError(f"{field} must be a full lowercase commit")
    return value


def _protected_directory(
    path: Path, field: str, *, trusted_owner_uid: int | None = None
) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DisasterRecoveryError(f"{field} is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid not in {
            0,
            os.geteuid(),
            *(set() if trusted_owner_uid is None else {trusted_owner_uid}),
        }
        or metadata.st_mode & 0o022
    ):
        raise DisasterRecoveryError(f"{field} must be owned and protected")


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(payload: object) -> str:
    raw = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(raw).hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    raw = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    _write_bytes_atomic(path, raw)


def _write_bytes_atomic(path: Path, raw: bytes) -> None:
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result
