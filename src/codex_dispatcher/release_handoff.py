"""Permanent proof that a committed two-host release became operational."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
from typing import Any

from codex_dispatcher.control_reclamation_status import (
    MAXIMUM_STATUS_AGE_SECONDS as MAXIMUM_CONTROL_STATUS_AGE_SECONDS,
    ControlReclamationStatus,
    load_control_reclamation_status,
)
from codex_dispatcher.github_api_metrics import GitHubApiSweepOutcome
from codex_dispatcher.runner_reclamation_status import (
    MAXIMUM_STATUS_AGE_SECONDS,
    RunnerReclamationStatus,
)
from codex_dispatcher.state_store import StateStore


CONTROL_CURRENT = Path("/opt/codex-dispatcher/current")
RELEASE_RECEIPT_ROOT = Path("/opt/codex-dispatcher/release-receipts")
HANDOFF_RECEIPT_ROOT = Path("/opt/codex-dispatcher/release-handoff-receipts")
DATABASE_PATH = Path("/var/lib/codex-dispatcher/state.db")
BACKUP_ROOT = Path("/var/lib/codex-dispatcher/backups")
RUNNER_HOST = "codex-runner"
CONTROL_SSH_USER = "ecs-user"
RUNNER_CURRENT = "/srv/codex-runner/current"
RUNNER_REFERENCES = "/srv/codex-runner/etc/reclamation-rollback-references.json"
RUNNER_REFERENCE_RECEIPTS = "/srv/codex-runner/reclamation-reference-receipts"
RUNNER_STATUS = "/srv/codex-runner/reclamation-status/latest.json"
CONTROL_RECLAMATION_STATUS = Path(
    "/var/lib/codex-dispatcher/control-reclamation-status/latest.json"
)

CONTROL_TIMERS = (
    "codex-dispatcher.timer",
    "codex-dispatcher-backup.timer",
    "codex-dispatcher-health.timer",
    "codex-dispatcher-restore-drill.timer",
    "codex-dispatcher-control-reclamation.timer",
)
CONTROL_SERVICES = (
    "codex-dispatcher-backup.service",
    "codex-dispatcher-restore-drill.service",
    "codex-dispatcher-health.service",
    "codex-dispatcher-control-reclamation.service",
)
RUNNER_TIMER = "codex-runner-reclamation-plan.timer"
RUNNER_SERVICE = "codex-runner-reclamation-plan.service"
EXPECTED_SCHEMA = tuple(range(1, 22))

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")

CommandRunner = Callable[[tuple[str, ...]], str]


class ReleaseHandoffError(RuntimeError):
    """Raised before an operational handoff can be recorded."""


def record_release_handoff(
    release_commit: str,
    *,
    command_runner: CommandRunner | None = None,
    now: datetime | None = None,
    control_current: Path = CONTROL_CURRENT,
    release_receipt_root: Path = RELEASE_RECEIPT_ROOT,
    handoff_receipt_root: Path = HANDOFF_RECEIPT_ROOT,
    database_path: Path = DATABASE_PATH,
    backup_root: Path = BACKUP_ROOT,
    control_reclamation_status_path: Path = CONTROL_RECLAMATION_STATUS,
    control_reclamation_status_owner_uid: int = 0,
) -> dict[str, object]:
    """Validate the post-release boundary and write one immutable receipt."""
    _validate_commit(release_commit)
    receipt_path = handoff_receipt_root / f"{release_commit}.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        receipt = _read_protected_json(receipt_path, "handoff receipt", 256 * 1024)
        _validate_handoff_receipt(receipt, release_commit)
        return receipt

    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("handoff timestamp must be timezone-aware")
    moment = moment.astimezone(timezone.utc)
    run = _run_command if command_runner is None else command_runner

    release_path = release_receipt_root / f"{release_commit}.json"
    release = _read_protected_json(release_path, "release receipt", 128 * 1024)
    _validate_release_receipt(release, release_commit)
    release_updated = _timestamp(release.get("updated_at"), "release updated_at")
    release_epoch = int(release_updated.timestamp())
    release_sha = _sha256_file(release_path)

    try:
        control_target = os.readlink(control_current)
    except OSError as exc:
        raise ReleaseHandoffError("Control current link is unavailable") from exc
    expected_control = f"/opt/codex-dispatcher/releases/{release_commit}"
    if control_target != expected_control:
        raise ReleaseHandoffError("Control current differs from release receipt")
    runner_target = run(
        (*_runner_ssh_prefix(),
            "/usr/bin/readlink",
            RUNNER_CURRENT,
        )
    ).strip()
    if runner_target != f"releases/{release_commit}":
        raise ReleaseHandoffError("Runner current differs from release receipt")

    control_timers = {
        unit: _systemd_state(run, unit) for unit in CONTROL_TIMERS
    }
    for state in control_timers.values():
        if state["ActiveState"] != "active" or state["UnitFileState"] != "enabled":
            raise ReleaseHandoffError("Control timer handoff is incomplete")
    control_services = {
        unit: _systemd_state(run, unit) for unit in CONTROL_SERVICES
    }
    for state in control_services.values():
        if state["Result"] != "success" or state["ExecMainStatus"] != "0":
            raise ReleaseHandoffError("Control validation service did not succeed")

    runner_timer = _systemd_state(run, RUNNER_TIMER, remote=True)
    if (
        runner_timer["ActiveState"] != "active"
        or runner_timer["UnitFileState"] != "enabled"
    ):
        raise ReleaseHandoffError("Runner planner timer handoff is incomplete")
    runner_service = _systemd_state(run, RUNNER_SERVICE, remote=True)
    if runner_service["Result"] != "success" or runner_service["ExecMainStatus"] != "0":
        raise ReleaseHandoffError("Runner planner service did not succeed")

    with StateStore(database_path, read_only=True) as store:
        if store.integrity_check() != "ok" or store.foreign_key_violation_count():
            raise ReleaseHandoffError("online database checks failed")
        sweep = store.get_latest_github_api_sweep()
    if (
        sweep is None
        or sweep.outcome is not GitHubApiSweepOutcome.SUCCESS
        or sweep.metrics.failure_count
        or _timestamp(sweep.completed_at, "sweep completed_at") < release_updated
    ):
        raise ReleaseHandoffError("no successful post-release Dispatcher sweep exists")

    backup_receipt = _latest_journal_json(
        run,
        "codex-dispatcher-backup.service",
        release_epoch,
        marker="state_backup",
    )
    if backup_receipt.get("ok") is not True or backup_receipt.get("integrity") != "ok":
        raise ReleaseHandoffError("post-release backup receipt is invalid")
    backup_path = Path(str(backup_receipt.get("path")))
    _validate_backup(backup_path, backup_root, not_before=release_updated)
    with StateStore(backup_path, read_only=True) as backup_store:
        if (
            backup_store.integrity_check() != "ok"
            or backup_store.foreign_key_violation_count()
            or backup_store.schema_migration_versions() != EXPECTED_SCHEMA
        ):
            raise ReleaseHandoffError("post-release backup content is invalid")

    restore_receipt = _latest_journal_json(
        run,
        "codex-dispatcher-restore-drill.service",
        release_epoch,
        marker="state_restore_drill",
    )
    if (
        restore_receipt.get("ok") is not True
        or restore_receipt.get("integrity") != "ok"
        or restore_receipt.get("foreign_key_violations") != 0
        or restore_receipt.get("schema_migrations") != list(EXPECTED_SCHEMA)
        or restore_receipt.get("temporary_restore_removed") is not True
        or restore_receipt.get("source_path") != str(backup_path)
    ):
        raise ReleaseHandoffError("post-release restore drill receipt is invalid")

    health = _latest_journal_json(
        run,
        "codex-dispatcher-health.service",
        release_epoch,
        marker="lifecycle_health",
    )
    notification = health.get("slack_notification")
    if (
        health.get("ok") is not True
        or health.get("integrity") != "ok"
        or health.get("foreign_key_violations") != 0
        or not isinstance(notification, dict)
        or health.get("alerts_truncated") is not False
    ):
        raise ReleaseHandoffError("post-release lifecycle health is not healthy")
    health_checked = _timestamp(health.get("checked_at"), "health checked_at")
    if health_checked < release_updated:
        raise ReleaseHandoffError("lifecycle health predates the release")

    references_raw = _remote_file(run, RUNNER_REFERENCES)
    references = _decode_json(references_raw, "Runner references")
    reference_receipt_path = f"{RUNNER_REFERENCE_RECEIPTS}/{release_commit}.apply.json"
    reference_receipt_raw = _remote_file(run, reference_receipt_path)
    reference_receipt = _decode_json(reference_receipt_raw, "Runner reference receipt")
    _validate_runner_references(references, reference_receipt, release_commit, release)

    status_raw = _remote_file(run, RUNNER_STATUS)
    status_payload = _decode_json(status_raw, "Runner reclamation status")
    status = RunnerReclamationStatus.from_mapping(status_payload)
    status_checked = _timestamp(status.checked_at, "Runner status checked_at")
    status_age = int((moment - status_checked).total_seconds())
    if status_checked < release_updated or status_age < -300 or status_age > MAXIMUM_STATUS_AGE_SECONDS:
        raise ReleaseHandoffError("Runner reclamation status is not current for this release")
    control_status_raw = control_reclamation_status_path.read_bytes()
    control_status = load_control_reclamation_status(
        control_reclamation_status_path,
        trusted_owner_uid=control_reclamation_status_owner_uid,
    )
    control_checked = _timestamp(
        control_status.checked_at, "Control reclamation status checked_at"
    )
    control_age = int((moment - control_checked).total_seconds())
    if (
        control_status.current_release_commit != release_commit
        or control_checked < release_updated
        or control_age < -300
        or control_age > MAXIMUM_CONTROL_STATUS_AGE_SECONDS
    ):
        raise ReleaseHandoffError("Control reclamation status is not current for this release")
    _validate_health_alert_boundary(health, notification, status, control_status)

    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": "codex_dispatcher_release_handoff",
        "status": "operational",
        "release_commit": release_commit,
        "observed_at": moment.isoformat().replace("+00:00", "Z"),
        "release_receipt_path": str(release_path),
        "release_receipt_sha256": release_sha,
        "control_current": control_target,
        "runner_current": runner_target,
        "dispatcher_sweep": sweep.to_mapping(),
        "backup": {
            "path": str(backup_path),
            "sha256": _sha256_file(backup_path),
            "size_bytes": backup_path.stat(follow_symlinks=False).st_size,
            "receipt": backup_receipt,
        },
        "restore_drill": restore_receipt,
        "lifecycle_health": {
            "checked_at": health.get("checked_at"),
            "alert_count": health.get("alert_count"),
            "alerts": health.get("alerts"),
            "slack_notification": notification,
            "work_items_total": health.get("work_items_total"),
            "active_turns": health.get("active_turns"),
        },
        "control_timers": control_timers,
        "control_services": control_services,
        "runner_planner_timer": runner_timer,
        "runner_planner_service": runner_service,
        "runner_references_sha256": sha256(references_raw).hexdigest(),
        "runner_reference_receipt_path": reference_receipt_path,
        "runner_reference_receipt_sha256": sha256(reference_receipt_raw).hexdigest(),
        "runner_reclamation_status_sha256": sha256(status_raw).hexdigest(),
        "runner_reclamation_status": status.to_mapping(),
        "control_reclamation_status_sha256": sha256(control_status_raw).hexdigest(),
        "control_reclamation_status": control_status.to_mapping(),
        "timers_started": True,
        "external_writes": False,
        "authorizes_reclamation_apply": False,
    }
    payload["evidence_sha256"] = _canonical_sha256(payload)
    _write_receipt(receipt_path, payload)
    return payload


def read_release_handoff(
    release_commit: str,
    *,
    handoff_receipt_root: Path = HANDOFF_RECEIPT_ROOT,
) -> dict[str, object]:
    _validate_commit(release_commit)
    receipt = _read_protected_json(
        handoff_receipt_root / f"{release_commit}.json",
        "handoff receipt",
        256 * 1024,
    )
    _validate_handoff_receipt(receipt, release_commit)
    return receipt


def _validate_release_receipt(payload: dict[str, Any], release_commit: str) -> None:
    if (
        payload.get("schema_version") != 2
        or payload.get("kind") != "codex_dispatcher_release"
        or payload.get("operation") != "apply"
        or payload.get("status") != "committed"
        or payload.get("phase") != "handoff_required"
        or payload.get("committed") is not True
        or payload.get("runner_references_applied") is not True
        or payload.get("release_commit") != release_commit
    ):
        raise ReleaseHandoffError("release receipt is not a committed transactional handoff")


def _validate_runner_references(
    references: dict[str, Any],
    receipt: dict[str, Any],
    release_commit: str,
    release: dict[str, Any],
) -> None:
    previous = str(release.get("previous_runner", "")).removeprefix("releases/")
    image = references.get("current_image_ref")
    if (
        references.get("schema_version") != 2
        or references.get("kind") != "runner_reclamation_rollback_references"
        or references.get("current_release_commit") != release_commit
        or references.get("immediate_rollback_release_commit") != previous
        or references.get("protected_release_commits") != [release_commit, previous]
        or not isinstance(image, str)
        or _IMAGE_RE.fullmatch(image) is None
        or references.get("protected_image_refs") != [image]
    ):
        raise ReleaseHandoffError("Runner reference ledger conflicts with release")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("kind") != "runner_reclamation_reference_change_receipt"
        or receipt.get("operation") != "apply"
        or receipt.get("status") != "applied"
        or receipt.get("release_commit") != release_commit
        or receipt.get("previous_release_commit") != previous
        or receipt.get("after_references") != references
        or receipt.get("after_sha256") != _canonical_sha256(references)
    ):
        raise ReleaseHandoffError("Runner reference receipt conflicts with live ledger")


def _validate_health_alert_boundary(
    health: dict[str, Any],
    notification: dict[str, Any],
    runner_status: RunnerReclamationStatus,
    control_status: ControlReclamationStatus,
) -> None:
    alerts = health.get("alerts")
    expected: list[dict[str, object]] = []
    if runner_status.trigger_reasons:
        expected.append(
            {
                "code": "runner_reclamation_plan_ready",
                "plan_sha256": runner_status.plan_sha256,
            }
        )
    if control_status.trigger_reasons:
        expected.append(
            {
                "code": "control_reclamation_plan_ready",
                "plan_sha256": control_status.plan_sha256,
            }
        )
    if expected:
        if (
            health.get("alert_count") != len(expected)
            or alerts != expected
            or notification.get("action")
            not in {"alert_opened", "alert_updated", "unchanged"}
            or not isinstance(notification.get("delivery_key"), str)
            or not isinstance(notification.get("permalink"), str)
        ):
            raise ReleaseHandoffError(
                "post-release health is not the exact non-blocking reclamation boundary"
            )
        return
    if (
        health.get("alert_count") != 0
        or alerts != []
        or notification.get("action") not in {"healthy", "recovered"}
    ):
        raise ReleaseHandoffError("post-release lifecycle health has blocking alerts")


def _systemd_state(
    run: CommandRunner, unit: str, *, remote: bool = False
) -> dict[str, str]:
    fields = (
        ("Id", "ActiveState", "UnitFileState")
        if unit.endswith(".timer")
        else ("Id", "ActiveState", "UnitFileState", "Result", "ExecMainStatus")
    )
    command = ["/usr/bin/systemctl", "show", unit]
    for field in fields:
        command.extend(("-p", field))
    if remote:
        command = [
            *_runner_ssh_prefix(),
            *command,
        ]
    output = run(tuple(command))
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator != "=" or key not in fields or key in values:
            raise ReleaseHandoffError("systemd returned malformed handoff state")
        values[key] = value
    if set(values) != set(fields) or values["Id"] != unit:
        raise ReleaseHandoffError("systemd returned incomplete handoff state")
    return values


def _latest_journal_json(
    run: CommandRunner, unit: str, since_epoch: int, *, marker: str
) -> dict[str, Any]:
    output = run(
        (
            "/usr/bin/journalctl",
            "-u",
            unit,
            "--since",
            f"@{since_epoch}",
            "-o",
            "cat",
            "--no-pager",
        )
    )
    matches: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get(marker) is True:
            matches.append(payload)
    if not matches:
        raise ReleaseHandoffError(f"no post-release {marker} journal receipt exists")
    return matches[-1]


def _remote_file(run: CommandRunner, path: str) -> bytes:
    output = run(
        (*_runner_ssh_prefix(),
            "/usr/bin/sudo",
            "-n",
            "/usr/bin/cat",
            path,
        )
    )
    return output.encode("utf-8")


def _runner_ssh_prefix() -> tuple[str, ...]:
    return (
        "/usr/bin/sudo",
        "-n",
        "-H",
        "-u",
        CONTROL_SSH_USER,
        "/usr/bin/ssh",
        "-o",
        "BatchMode=yes",
        RUNNER_HOST,
    )


def _validate_backup(
    path: Path, backup_root: Path, *, not_before: datetime
) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
        expected_uid = pwd.getpwnam("codex-dispatcher").pw_uid
    except (OSError, KeyError) as exc:
        raise ReleaseHandoffError("post-release backup is unavailable") from exc
    if (
        path.parent != backup_root
        or not re.fullmatch(r"state-[0-9]{8}T[0-9]{6}\.[0-9]{6}Z\.db", path.name)
        or path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or datetime.fromtimestamp(metadata.st_mtime, timezone.utc) < not_before
    ):
        raise ReleaseHandoffError("post-release backup exceeds its protected boundary")


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.parent.stat(follow_symlinks=False)
    if path.parent.is_symlink() or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise ReleaseHandoffError("handoff receipt directory is unsafe")
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(raw + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validate_handoff_receipt(payload: dict[str, Any], release_commit: str) -> None:
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
        or not isinstance(evidence, str)
        or _SHA256_RE.fullmatch(evidence) is None
        or evidence != _canonical_sha256(body)
    ):
        raise ReleaseHandoffError("handoff receipt is invalid")


def _run_command(command: tuple[str, ...]) -> str:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseHandoffError("handoff observation command failed") from exc
    if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
        raise ReleaseHandoffError("handoff observation command failed")
    return completed.stdout


def _read_protected_json(path: Path, field: str, maximum: int) -> dict[str, Any]:
    try:
        metadata = path.stat(follow_symlinks=False)
        raw = path.read_bytes()
    except OSError as exc:
        raise ReleaseHandoffError(f"{field} is unavailable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or not 0 < len(raw) <= maximum
    ):
        raise ReleaseHandoffError(f"{field} is unsafe")
    return _decode_json(raw, field)


def _decode_json(raw: bytes, field: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReleaseHandoffError(f"{field} is malformed") from exc
    if not isinstance(payload, dict):
        raise ReleaseHandoffError(f"{field} must be an object")
    return payload


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ReleaseHandoffError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseHandoffError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise ReleaseHandoffError(f"{field} lacks timezone")
    return parsed.astimezone(timezone.utc)


def _validate_commit(value: str) -> None:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise ValueError("release commit must be 40 lowercase hexadecimal characters")


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(raw.encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-dispatcher-release-handoff-v1")
    parser.add_argument("--commit", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("release handoff requires root", file=sys.stderr)
        return 1
    try:
        payload = (
            record_release_handoff(args.commit)
            if args.apply
            else read_release_handoff(args.commit)
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, **payload}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
