"""Current-schema disaster-recovery rehearsal and exact external reconciliation."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
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


_SCHEMA_VERSION = 3
_BUNDLE_SCHEMA_VERSION = 1
_EXPECTED_DATABASE_SCHEMA = tuple(range(1, 21))
_RELEASE_FILES = (
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
_BACKUP_NAME_RE = re.compile(r"state-([0-9]{8}T[0-9]{6}\.[0-9]{6}Z)\.db")


class DisasterRecoveryError(RuntimeError):
    """Raised when any isolated recovery or external read-back is incomplete."""


class SlackReceiptVerifier(Protocol):
    def verify_receipt(self, receipt: SlackDeliveryReceipt) -> SlackDeliveryReceipt: ...


@dataclass(frozen=True, slots=True)
class DisasterRecoveryBundle:
    root: Path
    manifest_sha256: str
    release_commit: str
    source_backup_at: str
    source_backup: Path
    control_release: Path
    control_config: Path
    release_receipt: Path
    handoff_receipt: Path
    runner_snapshot: Path
    provenance_databases: tuple[Path, ...]
    system_slack_receipts: tuple[Path, ...]


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
    work_item_slack_receipt_count: int
    system_slack_receipt_count: int
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
            "work_item_slack_receipt_count": self.work_item_slack_receipt_count,
            "system_slack_receipt_count": self.system_slack_receipt_count,
            "slack_receipt_count": self.slack_receipt_count,
            "rto_milliseconds": self.rto_milliseconds,
            "rto_scope": (
                "complete independent-bundle validation through isolated schema-20 "
                "restore, Control and Runner application-filesystem rebuilds, and "
                "exact Runner/GitHub/Slack read-back"
            ),
            "infrastructure_provisioning_rto_measured": False,
            "online_state_modified": False,
            "recovery_commands": [
                "sudo systemctl stop codex-dispatcher.timer codex-dispatcher-health.timer",
                f"verify the independent bundle manifest and install its exact release tree "
                f"as commit {self.release_commit}",
                f"copy validated bundled database {self.source_backup} to a new state.db "
                "candidate; never overwrite the online database in place",
                "install the exact release on Runner first and Control second",
                "run the schema18 disaster-recovery reconciliation before enabling writes",
                "run codex-dispatcher ssh-preflight --config "
                "/etc/codex-dispatcher/config.toml --json",
            ],
            "failure_rollback_boundary": [
                "never replace the online Control database during the drill",
                "never switch either live current symlink during the drill",
                "never start, resume, stop, or archive a Runner Turn during reconciliation",
                "on failure retain the source bundle and remove only this exact recovery_root",
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

    registry_rows: list[dict[str, object]] = []
    for path, payload, _ in registry:
        work_item_id = validate_work_item_id(payload.get("work_item_id"))
        repository = payload.get("repository")
        issue_number = payload.get("issue_number")
        if (
            path.stem != work_item_id
            or not isinstance(repository, str)
            or "/" not in repository
            or type(issue_number) is not int
            or issue_number <= 0
        ):
            raise DisasterRecoveryError("Runner registry filename conflicts with identity")
        registry_rows.append(
            {
                "work_item_id": work_item_id,
                "repository": repository,
                "issue_number": issue_number,
            }
        )

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
        "registry": sorted(registry_rows, key=lambda row: str(row["work_item_id"])),
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


def create_disaster_recovery_bundle(
    *,
    database_path: Path,
    backup_directory: Path,
    bundle_root: Path,
    control_release_path: Path,
    control_config_path: Path,
    release_receipt_path: Path,
    handoff_receipt_path: Path,
    runner_snapshot_path: Path,
    provenance_database_paths: tuple[Path, ...] = (),
    system_slack_receipt_paths: tuple[Path, ...] = (),
    now: datetime | None = None,
) -> DisasterRecoveryBundle:
    """Create one self-contained, hash-bound bundle for loss of both Linux hosts."""
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("bundle timestamp must be timezone-aware")
    moment = moment.astimezone(timezone.utc)
    restore_evidence = drill_latest_state_backup(database_path, backup_directory, now=moment)
    release = _read_release_receipt(release_receipt_path)
    release_commit = _validate_commit(release.get("release_commit"), "release_commit")
    if control_release_path.name != release_commit:
        raise DisasterRecoveryError("Control release path conflicts with receipt")
    _read_handoff_receipt(handoff_receipt_path, release_commit)
    _validate_release_tree(control_release_path)
    _validate_protected_file(control_config_path, "Control config", maximum=256 * 1024)
    snapshot = load_runner_recovery_snapshot(runner_snapshot_path)
    if _validate_runner_snapshot(snapshot) != release_commit:
        raise DisasterRecoveryError("Control and Runner release commits differ")
    _prepare_new_recovery_root(bundle_root)
    try:
        state_target = bundle_root / "state.db"
        with StateStore(restore_evidence.source_path, read_only=True) as source:
            source.backup(state_target)
        os.chmod(state_target, 0o600, follow_symlinks=False)

        release_target = bundle_root / "release"
        shutil.copytree(control_release_path, release_target, symlinks=False)
        config_target = bundle_root / "control-config.toml"
        release_receipt_target = bundle_root / "release-receipt.json"
        handoff_target = bundle_root / "handoff-receipt.json"
        snapshot_target = bundle_root / "runner-snapshot.json"
        for source, target in (
            (control_config_path, config_target),
            (release_receipt_path, release_receipt_target),
            (handoff_receipt_path, handoff_target),
            (runner_snapshot_path, snapshot_target),
        ):
            shutil.copy2(source, target, follow_symlinks=False)
            os.chmod(target, 0o600, follow_symlinks=False)

        provenance_root = bundle_root / "provenance-databases"
        provenance_root.mkdir(mode=0o700)
        provenance_targets: list[Path] = []
        for index, source_path in enumerate(provenance_database_paths):
            target = provenance_root / f"source-{index:02d}.db"
            with StateStore(source_path, read_only=True) as source:
                if source.integrity_check() != "ok" or source.foreign_key_violation_count():
                    raise DisasterRecoveryError("provenance database is not integral")
                if source.schema_migration_versions() != _EXPECTED_DATABASE_SCHEMA:
                    raise DisasterRecoveryError("provenance database is not exact schema 20")
                source.backup(target)
            os.chmod(target, 0o600, follow_symlinks=False)
            provenance_targets.append(target)

        system_root = bundle_root / "system-slack-receipts"
        system_root.mkdir(mode=0o700)
        system_targets: list[Path] = []
        for index, source_path in enumerate(system_slack_receipt_paths):
            target = system_root / f"receipt-{index:02d}.json"
            _read_json_file(source_path, "system Slack receipt", maximum=512 * 1024)
            shutil.copy2(source_path, target, follow_symlinks=False)
            os.chmod(target, 0o600, follow_symlinks=False)
            system_targets.append(target)

        artifacts: list[dict[str, object]] = []
        for path in sorted(bundle_root.rglob("*")):
            if path.is_dir():
                continue
            relative = path.relative_to(bundle_root).as_posix()
            artifacts.append(
                {
                    "path": relative,
                    "size_bytes": path.stat(follow_symlinks=False).st_size,
                    "sha256": _sha256_file(path),
                }
            )
        manifest: dict[str, object] = {
            "schema_version": _BUNDLE_SCHEMA_VERSION,
            "kind": "codex_dispatcher_disaster_recovery_bundle",
            "release_commit": release_commit,
            "created_at": moment.isoformat().replace("+00:00", "Z"),
            "source_backup_at": _backup_timestamp_from_name(
                restore_evidence.source_path.name
            ).isoformat().replace("+00:00", "Z"),
            "contains_protected_control_config": True,
            "online_state_modified": False,
            "artifacts": artifacts,
            "provenance_databases": [
                path.relative_to(bundle_root).as_posix() for path in provenance_targets
            ],
            "system_slack_receipts": [
                path.relative_to(bundle_root).as_posix() for path in system_targets
            ],
        }
        manifest["evidence_sha256"] = _canonical_sha256(manifest)
        _write_json_atomic(bundle_root / "manifest.json", manifest)
        _fsync_directory(bundle_root)
        return load_disaster_recovery_bundle(bundle_root)
    except BaseException:
        failed = bundle_root / "failed-receipt.json"
        if bundle_root.is_dir() and not failed.exists():
            try:
                _write_json_atomic(
                    failed,
                    {
                        "schema_version": _BUNDLE_SCHEMA_VERSION,
                        "kind": "codex_dispatcher_disaster_recovery_bundle",
                        "status": "failed",
                        "online_state_modified": False,
                    },
                )
            except OSError:
                pass
        raise


def load_disaster_recovery_bundle(root: Path) -> DisasterRecoveryBundle:
    """Validate every bundle byte without consulting either original Linux host."""
    _protected_directory(root, "disaster recovery bundle")
    manifest_path = root / "manifest.json"
    manifest = _read_json_file(
        manifest_path, "disaster recovery bundle manifest", maximum=2 * 1024 * 1024
    )
    evidence = manifest.get("evidence_sha256")
    body = dict(manifest)
    body.pop("evidence_sha256", None)
    artifacts = manifest.get("artifacts")
    provenance_values = manifest.get("provenance_databases")
    system_values = manifest.get("system_slack_receipts")
    if (
        manifest.get("schema_version") != _BUNDLE_SCHEMA_VERSION
        or manifest.get("kind") != "codex_dispatcher_disaster_recovery_bundle"
        or manifest.get("contains_protected_control_config") is not True
        or manifest.get("online_state_modified") is not False
        or not isinstance(evidence, str)
        or evidence != _canonical_sha256(body)
        or not isinstance(artifacts, list)
        or not isinstance(provenance_values, list)
        or not isinstance(system_values, list)
    ):
        raise DisasterRecoveryError("disaster recovery bundle manifest is invalid")
    expected_paths: set[str] = set()
    for row in artifacts:
        if not isinstance(row, dict):
            raise DisasterRecoveryError("disaster recovery bundle artifact is invalid")
        relative = row.get("path")
        digest = row.get("sha256")
        size = row.get("size_bytes")
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or ".." in Path(relative).parts
            or relative in expected_paths
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or type(size) is not int
            or size < 0
        ):
            raise DisasterRecoveryError("disaster recovery bundle artifact is invalid")
        path = root / relative
        metadata = path.stat(follow_symlinks=False)
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_mode & 0o022
            or metadata.st_nlink != 1
            or metadata.st_size != size
            or _sha256_file(path) != digest
        ):
            raise DisasterRecoveryError("disaster recovery bundle artifact digest conflicts")
        expected_paths.add(relative)
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if actual_paths != expected_paths:
        raise DisasterRecoveryError("disaster recovery bundle contains unknown artifacts")

    release_commit = _validate_commit(manifest.get("release_commit"), "release_commit")
    backup_timestamp = manifest.get("source_backup_at")
    if not isinstance(backup_timestamp, str):
        raise DisasterRecoveryError("disaster recovery bundle backup timestamp is invalid")
    try:
        parsed_backup_timestamp = datetime.fromisoformat(
            backup_timestamp.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise DisasterRecoveryError(
            "disaster recovery bundle backup timestamp is invalid"
        ) from exc
    if parsed_backup_timestamp.tzinfo is None:
        raise DisasterRecoveryError("disaster recovery bundle backup timestamp lacks timezone")
    source_backup_at = parsed_backup_timestamp.astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    provenance = _bundle_path_list(root, provenance_values, expected_paths)
    system = _bundle_path_list(root, system_values, expected_paths)
    bundle = DisasterRecoveryBundle(
        root=root,
        manifest_sha256=_sha256_file(manifest_path),
        release_commit=release_commit,
        source_backup_at=source_backup_at,
        source_backup=root / "state.db",
        control_release=root / "release",
        control_config=root / "control-config.toml",
        release_receipt=root / "release-receipt.json",
        handoff_receipt=root / "handoff-receipt.json",
        runner_snapshot=root / "runner-snapshot.json",
        provenance_databases=provenance,
        system_slack_receipts=system,
    )
    for required in (
        bundle.source_backup,
        bundle.control_config,
        bundle.release_receipt,
        bundle.handoff_receipt,
        bundle.runner_snapshot,
    ):
        if required.relative_to(root).as_posix() not in expected_paths:
            raise DisasterRecoveryError("disaster recovery bundle is incomplete")
    _validate_release_tree(bundle.control_release)
    with tempfile.TemporaryDirectory() as raw:
        validation_copy = Path(raw) / "state.db"
        shutil.copy2(bundle.source_backup, validation_copy, follow_symlinks=False)
        with StateStore(validation_copy, read_only=True) as store:
            if (
                store.integrity_check() != "ok"
                or store.foreign_key_violation_count()
                or store.schema_migration_versions() != _EXPECTED_DATABASE_SCHEMA
            ):
                raise DisasterRecoveryError("bundled Control database is invalid")
    return bundle


def _bundle_path_list(
    root: Path, values: list[object], expected_paths: set[str]
) -> tuple[Path, ...]:
    paths: list[Path] = []
    for value in values:
        if (
            not isinstance(value, str)
            or value not in expected_paths
            or value.startswith("/")
            or ".." in Path(value).parts
        ):
            raise DisasterRecoveryError("disaster recovery bundle path list is invalid")
        paths.append(root / value)
    if len(paths) != len(set(paths)):
        raise DisasterRecoveryError("disaster recovery bundle path list is duplicated")
    return tuple(paths)


def _backup_timestamp_from_name(name: str) -> datetime:
    match = _BACKUP_NAME_RE.fullmatch(name)
    if match is None:
        raise DisasterRecoveryError("source backup has a noncanonical name")
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S.%fZ").replace(
        tzinfo=timezone.utc
    )


def run_schema18_disaster_recovery_drill(
    *,
    bundle: DisasterRecoveryBundle,
    recovery_root: Path,
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
        verified_bundle = load_disaster_recovery_bundle(bundle.root)
        if verified_bundle != bundle:
            raise DisasterRecoveryError("disaster recovery bundle changed after loading")
        restored_database = recovery_root / "restored-state.db"
        shutil.copy2(bundle.source_backup, restored_database, follow_symlinks=False)
        os.chmod(restored_database, 0o600, follow_symlinks=False)
        _fsync_file(restored_database)

        release = _read_release_receipt(bundle.release_receipt)
        release_commit = _validate_commit(release["release_commit"], "release_commit")
        handoff = _read_handoff_receipt(bundle.handoff_receipt, release_commit)
        release_receipt_sha256 = _sha256_file(bundle.release_receipt)
        if handoff.get("release_receipt_sha256") != release_receipt_sha256:
            raise DisasterRecoveryError("handoff receipt release digest conflicts")
        if bundle.release_commit != release_commit:
            raise DisasterRecoveryError("bundle release identity conflicts")
        if bundle.control_release.name != "release":
            raise DisasterRecoveryError("Control release path conflicts with receipt")
        _validate_release_tree(bundle.control_release)
        _validate_protected_file(bundle.control_config, "Control config", maximum=256 * 1024)
        runner_snapshot = load_runner_recovery_snapshot(bundle.runner_snapshot)
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
                restored, runner_snapshot, bundle.provenance_databases
            )
            github_issue_count, github_pr_count = _reconcile_github(
                restored, tracker
            )
            work_item_slack_count, system_database_slack_count = _reconcile_slack(
                restored, slack_verifier
            )
            system_external_slack_count = _reconcile_system_slack_receipts(
                bundle.system_slack_receipts, slack_verifier
            )
            system_slack_count = system_database_slack_count + system_external_slack_count

        rebuild_manifest, runner_rebuild_manifest = _rebuild_empty_hosts(
            recovery_root=recovery_root,
            release_path=bundle.control_release,
            release_commit=release_commit,
            restored_database=restored_database,
            control_config=bundle.control_config,
            release_receipt=bundle.release_receipt,
            handoff_receipt=bundle.handoff_receipt,
            runner_snapshot=runner_snapshot,
        )
        rto_milliseconds = max(0, (time.monotonic_ns() - started) // 1_000_000)
        result = DisasterRecoveryResult(
            recovery_root=recovery_root,
            receipt_path=receipt_path,
            source_backup=bundle.source_backup,
            source_backup_sha256=_sha256_file(bundle.source_backup),
            source_age_seconds=max(
                0,
                int(
                    (
                        moment
                        - datetime.fromisoformat(
                            bundle.source_backup_at.replace("Z", "+00:00")
                        )
                    ).total_seconds()
                ),
            ),
            release_commit=release_commit,
            release_receipt_sha256=release_receipt_sha256,
            handoff_receipt_sha256=_sha256_file(bundle.handoff_receipt),
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
            control_config_sha256=_sha256_file(bundle.control_config),
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
            work_item_slack_receipt_count=work_item_slack_count,
            system_slack_receipt_count=system_slack_count,
            slack_receipt_count=work_item_slack_count + system_slack_count,
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
    store: StateStore,
    snapshot: dict[str, object],
    provenance_database_paths: tuple[Path, ...],
) -> tuple[int, int, int]:
    registry_rows = _indexed_rows(snapshot.get("registry"), "Runner registry")
    registry_ids = set(registry_rows)
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
    orphan_ids = orphan_archive_ids | orphan_absence_ids
    expected_registry = (work_item_ids - set(absences)) | orphan_archive_ids
    if registry_ids != expected_registry:
        raise DisasterRecoveryError("Runner registry set differs from restored SQLite")

    provenance = _provenance_work_items(provenance_database_paths)
    if set(provenance) != orphan_ids:
        raise DisasterRecoveryError(
            "Runner-only terminal evidence lacks exact canary database provenance"
        )
    for work_item_id in orphan_ids:
        source = provenance[work_item_id]
        terminal = archive_rows.get(work_item_id) or absence_rows.get(work_item_id)
        assert terminal is not None
        if source.last_published_sha != terminal.get("expected_head_sha"):
            raise DisasterRecoveryError("Runner-only terminal head conflicts with provenance")
        registry = registry_rows.get(work_item_id)
        if registry is not None and (
            registry.get("repository") != source.repository
            or registry.get("issue_number") != source.issue_number
        ):
            raise DisasterRecoveryError("Runner-only registry conflicts with provenance")

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
    return len(archives), len(absences), len(orphan_ids)


def _provenance_work_items(paths: tuple[Path, ...]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as raw:
        temporary = Path(raw)
        for index, path in enumerate(paths):
            source_copy = temporary / f"source-{index:02d}.db"
            shutil.copy2(path, source_copy, follow_symlinks=False)
            with StateStore(source_copy, read_only=True) as source:
                if (
                    source.integrity_check() != "ok"
                    or source.foreign_key_violation_count()
                    or source.schema_migration_versions() != _EXPECTED_DATABASE_SCHEMA
                ):
                    raise DisasterRecoveryError("Runner provenance database is invalid")
                for work_item in source.list_work_items():
                    if work_item.work_item_id in rows:
                        raise DisasterRecoveryError("Runner provenance identity is duplicated")
                    rows[work_item.work_item_id] = work_item
    return rows


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
) -> tuple[int, int]:
    deliveries = store.list_slack_deliveries()
    health_deliveries = store.list_health_alert_deliveries()
    if (deliveries or health_deliveries) and verifier is None:
        raise DisasterRecoveryError("Slack receipts exist but no verifier is configured")
    work_item_count = 0
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
        work_item_count += 1
    system_count = 0
    for delivery in health_deliveries:
        if (
            delivery.state is not SlackDeliveryState.DELIVERED
            or delivery.message_ts is None
            or delivery.permalink is None
        ):
            raise DisasterRecoveryError("system Slack outbox contains an unfinished delivery")
        thread_ts = delivery.message_ts if delivery.thread_ts is None else delivery.thread_ts
        receipt = SlackDeliveryReceipt(
            delivery.delivery_key,
            delivery.channel_id,
            delivery.message_ts,
            thread_ts,
            delivery.permalink,
        )
        assert verifier is not None
        if verifier.verify_receipt(receipt) != receipt:
            raise DisasterRecoveryError(
                "system Slack receipt verifier returned conflicting evidence"
            )
        system_count += 1
    return work_item_count, system_count


def _reconcile_system_slack_receipts(
    paths: tuple[Path, ...], verifier: SlackReceiptVerifier | None
) -> int:
    if paths and verifier is None:
        raise DisasterRecoveryError("external system Slack receipts lack a verifier")
    count = 0
    for path in paths:
        payload = _read_json_file(path, "external system Slack receipt", maximum=512 * 1024)
        evidence = payload.get("evidence_sha256")
        body = dict(payload)
        body.pop("evidence_sha256", None)
        channel = payload.get("system_channel_id")
        issue_channel = payload.get("issue_channel_id")
        alert = payload.get("alert")
        recovery = payload.get("recovery")
        if (
            payload.get("schema_version") != 1
            or payload.get("kind") != "runner_reclamation_projection_canary"
            or payload.get("status") != "passed"
            or payload.get("issue_channel_writes") != 0
            or payload.get("online_state_modified") is not False
            or payload.get("authorizes_apply") is not False
            or payload.get("asset_deletions") != 0
            or not isinstance(channel, str)
            or not isinstance(issue_channel, str)
            or channel == issue_channel
            or not isinstance(alert, dict)
            or not isinstance(recovery, dict)
            or evidence != _canonical_sha256(body)
        ):
            raise DisasterRecoveryError("external system Slack receipt is invalid")
        alert_receipt = _slack_receipt_from_mapping(alert, channel, root=True)
        recovery_receipt = _slack_receipt_from_mapping(
            recovery, channel, root=False, expected_thread=alert_receipt.message_ts
        )
        assert verifier is not None
        for receipt in (alert_receipt, recovery_receipt):
            if verifier.verify_receipt(receipt) != receipt:
                raise DisasterRecoveryError(
                    "external system Slack verifier returned conflicting evidence"
                )
            count += 1
    return count


def _slack_receipt_from_mapping(
    payload: dict[str, object],
    channel_id: str,
    *,
    root: bool,
    expected_thread: str | None = None,
) -> SlackDeliveryReceipt:
    key = payload.get("delivery_key")
    message_ts = payload.get("message_ts")
    permalink = payload.get("permalink")
    thread_ts = message_ts if root else payload.get("thread_ts")
    if (
        not all(isinstance(value, str) for value in (key, message_ts, permalink, thread_ts))
        or (expected_thread is not None and thread_ts != expected_thread)
    ):
        raise DisasterRecoveryError("external system Slack receipt is incomplete")
    assert (
        isinstance(key, str)
        and isinstance(message_ts, str)
        and isinstance(permalink, str)
        and isinstance(thread_ts, str)
    )
    return SlackDeliveryReceipt(key, channel_id, message_ts, thread_ts, permalink)


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
    registry = _indexed_rows(snapshot.get("registry"), "Runner registry")
    _indexed_rows(snapshot.get("archives"), "Runner archives")
    _indexed_rows(snapshot.get("absences"), "Runner absences")
    if any(
        not isinstance(row.get("repository"), str)
        or "/" not in str(row.get("repository"))
        or type(row.get("issue_number")) is not int
        or int(row["issue_number"]) <= 0
        for row in registry.values()
    ):
        raise DisasterRecoveryError("Runner registry evidence is invalid")
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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
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
