"""Fail-closed command line entry points."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from codex_dispatcher import __version__
from codex_dispatcher.config import load_config
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.terminal_storage import (
    TerminalStorageEffectiveState,
    effective_terminal_storage_state,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-dispatcher",
        description="Fail-closed GitHub to persistent Codex CLI task dispatcher.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor", help="Run read-only local prerequisite checks."
    )
    doctor.add_argument("--config", type=Path, help="Optional TOML configuration to validate.")
    doctor.add_argument("--json", action="store_true", help="Emit machine-readable output.")

    status = subparsers.add_parser("status", help="Inspect an existing local state database.")
    status.add_argument("--database", required=True, type=Path)
    status.add_argument("--json", action="store_true", help="Emit machine-readable output.")

    runner_capacity = subparsers.add_parser(
        "runner-capacity", help="Inspect protected Runner disk admission capacity."
    )
    runner_capacity.add_argument("--config", required=True, type=Path)
    runner_capacity.add_argument(
        "--json", action="store_true", help="Emit machine-readable output."
    )
    runner_capacity.add_argument(
        "--require-provision-admissible",
        action="store_true",
        help="Exit nonzero unless one additional bounded WorkItem image can be provisioned.",
    )

    lifecycle_health = subparsers.add_parser(
        "lifecycle-health",
        help="Inspect lifecycle backlog and optional Control Host systemd state.",
    )
    lifecycle_health.add_argument("--config", required=True, type=Path)
    lifecycle_health.add_argument(
        "--systemd",
        action="store_true",
        help="Also inspect the fixed Control Host service and timer allowlist.",
    )
    lifecycle_health.add_argument(
        "--runner-capacity",
        action="store_true",
        help="Also read one strict capacity snapshot through the fixed Runner endpoint.",
    )
    lifecycle_health.add_argument(
        "--notify-slack",
        action="store_true",
        help="Persist and deliver idempotent Slack alert/recovery notifications.",
    )
    lifecycle_health.add_argument(
        "--json", action="store_true", help="Emit machine-readable output."
    )

    state_backup = subparsers.add_parser(
        "state-backup", help="Create one protected SQLite Online Backup."
    )
    state_backup.add_argument("--config", required=True, type=Path)
    state_backup.add_argument(
        "--json", action="store_true", help="Emit machine-readable output."
    )

    restore_drill = subparsers.add_parser(
        "state-restore-drill",
        help="Restore the newest protected backup to a temporary database and verify it.",
    )
    restore_drill.add_argument("--config", required=True, type=Path)
    restore_drill.add_argument(
        "--json", action="store_true", help="Emit machine-readable output."
    )

    disaster_recovery = subparsers.add_parser(
        "schema18-disaster-recovery",
        help="Restore schema 18 in isolation and reconcile exact external receipts.",
    )
    disaster_recovery.add_argument("--config", required=True, type=Path)
    disaster_recovery.add_argument("--recovery-root", required=True, type=Path)
    disaster_recovery.add_argument("--control-release", required=True, type=Path)
    disaster_recovery.add_argument("--release-receipt", required=True, type=Path)
    disaster_recovery.add_argument("--handoff-receipt", required=True, type=Path)
    disaster_recovery.add_argument("--runner-snapshot", required=True, type=Path)
    disaster_recovery.add_argument(
        "--execute-isolated",
        action="store_true",
        required=True,
        help="Required acknowledgement of local isolated writes and provider read-backs.",
    )
    disaster_recovery.add_argument(
        "--json", action="store_true", help="Emit machine-readable output."
    )

    ssh_run_once = subparsers.add_parser(
        "ssh-run-once",
        help="Execute one recovery-first SSH sweep with external writes enabled.",
    )
    ssh_run_once.add_argument("--config", required=True, type=Path)
    ssh_run_once.add_argument(
        "--apply",
        action="store_true",
        required=True,
        help="Required explicit acknowledgement that GitHub and SSH writes are enabled.",
    )
    ssh_run_once.add_argument(
        "--json", action="store_true", help="Emit machine-readable output."
    )

    ssh_preflight = subparsers.add_parser(
        "ssh-preflight",
        help="Read GitHub and a temporary state snapshot without external writes.",
    )
    ssh_preflight.add_argument("--config", required=True, type=Path)
    ssh_preflight.add_argument(
        "--json", action="store_true", help="Emit machine-readable output."
    )

    ssh_absence = subparsers.add_parser(
        "ssh-reconcile-absence",
        help="Persist trusted Runner proof for one completed WorkItem already missing on disk.",
    )
    ssh_absence.add_argument("--config", required=True, type=Path)
    ssh_absence.add_argument("--repository", required=True)
    ssh_absence.add_argument("--issue-number", required=True, type=int)
    ssh_absence.add_argument(
        "--apply",
        action="store_true",
        required=True,
        help="Required acknowledgement that Runner and SQLite writes are enabled.",
    )
    ssh_absence.add_argument(
        "--json", action="store_true", help="Emit machine-readable output."
    )

    ssh_abandon = subparsers.add_parser(
        "ssh-abandon-unknown-turn",
        help="Inspect or explicitly abandon one exact inactive v2 Runner Turn.",
    )
    ssh_abandon.add_argument("--config", required=True, type=Path)
    ssh_abandon.add_argument("--turn-id", required=True)
    abandon_mode = ssh_abandon.add_mutually_exclusive_group(required=True)
    abandon_mode.add_argument(
        "--plan",
        action="store_true",
        help="Read exact Runner inactivity evidence without state writes.",
    )
    abandon_mode.add_argument(
        "--apply",
        action="store_true",
        help="Commit Runner and SQLite abandonment receipts after a second proof.",
    )
    ssh_abandon.add_argument(
        "--json", action="store_true", help="Emit machine-readable output."
    )

    slack_fixture = subparsers.add_parser(
        "slack-idempotency-fixture",
        help="Prove one Slack client_msg_id exact-retry contract.",
    )
    slack_fixture.add_argument("--workspace-id", required=True)
    slack_fixture.add_argument("--channel-id", required=True)
    slack_fixture.add_argument("--fixture-id", required=True)
    slack_fixture.add_argument(
        "--request-timeout-seconds", type=float, default=10.0
    )
    slack_fixture.add_argument(
        "--apply",
        action="store_true",
        required=True,
        help="Required acknowledgement that exactly two Slack writes are enabled.",
    )
    slack_fixture.add_argument(
        "--json", action="store_true", help="Emit machine-readable output."
    )
    return parser


def _doctor(config_path: Path | None) -> tuple[int, dict[str, object]]:
    checks: list[dict[str, object]] = []

    python_ok = sys.version_info >= (3, 12)
    checks.append(
        {
            "name": "python",
            "ok": python_ok,
            "detail": platform.python_version(),
        }
    )

    for tool in ("git", "codex", "gh"):
        location = shutil.which(tool)
        checks.append(
            {
                "name": f"tool:{tool}",
                "ok": location is not None,
                "detail": location or "not found",
                "required_now": tool == "git",
            }
        )

    if config_path is not None:
        try:
            load_config(config_path)
        except (OSError, ValueError) as exc:
            checks.append(
                {"name": "config", "ok": False, "detail": str(exc), "required_now": True}
            )
        else:
            checks.append(
                {
                    "name": "config",
                    "ok": True,
                    "detail": str(config_path),
                    "required_now": True,
                }
            )

    required_failures = [
        check
        for check in checks
        if not check["ok"] and bool(check.get("required_now", check["name"] == "python"))
    ]
    result: dict[str, object] = {
        "ok": not required_failures,
        "phase": "offline-core",
        "checks": checks,
    }
    return (0 if not required_failures else 1), result


def _status(database_path: Path) -> tuple[int, dict[str, object]]:
    if not database_path.is_file():
        return 1, {"ok": False, "error": "database does not exist", "path": str(database_path)}

    try:
        with StateStore(database_path, read_only=True) as store:
            integrity = store.integrity_check()
            effective_counts = {
                state.value: 0 for state in TerminalStorageEffectiveState
            }
            effective_overrides: list[dict[str, object]] = []
            archives = {
                archive.work_item_id: archive
                for archive in store.list_work_item_archives()
            }
            absences = {
                absence.work_item_id: absence
                for absence in store.list_work_item_absence_reconciliations()
            }
            for work_item in store.list_work_items():
                archive = archives.get(work_item.work_item_id)
                absence = absences.get(work_item.work_item_id)
                effective = effective_terminal_storage_state(archive, absence)
                effective_counts[effective.value] += 1
                if absence is not None or (
                    effective is TerminalStorageEffectiveState.EVIDENCE_CONFLICT
                ):
                    effective_overrides.append(
                        {
                            "work_item_id": work_item.work_item_id,
                            "repository": work_item.repository,
                            "issue_number": work_item.issue_number,
                            "raw_archive_status": (
                                None if archive is None else archive.status.value
                            ),
                            "effective_state": effective.value,
                        }
                    )
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return 1, {"ok": False, "error": str(exc), "path": str(database_path)}

    conflict_count = effective_counts[
        TerminalStorageEffectiveState.EVIDENCE_CONFLICT.value
    ]
    override_limit = 100
    return (0 if integrity == "ok" and conflict_count == 0 else 1), {
        "ok": integrity == "ok" and conflict_count == 0,
        "integrity": integrity,
        "terminal_storage_effective_counts": effective_counts,
        "terminal_storage_effective_overrides": effective_overrides[:override_limit],
        "terminal_storage_effective_overrides_total": len(effective_overrides),
        "terminal_storage_effective_overrides_truncated": (
            len(effective_overrides) > override_limit
        ),
        "path": str(database_path),
    }


def _state_backup(config_path: Path) -> tuple[int, dict[str, object]]:
    try:
        from codex_dispatcher.control_host_backup import (
            create_state_backup,
            rotate_state_backups,
        )
        from codex_dispatcher.ssh_runtime import load_protected_ssh_config

        config = load_protected_ssh_config(config_path)
        database_path = config.scheduler.database_path
        result = create_state_backup(
            database_path,
            database_path.parent / "backups",
        )
        retention = rotate_state_backups(database_path.parent / "backups")
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return 1, {"ok": False, "error": str(exc)}
    return 0, {
        "ok": True,
        "state_backup": True,
        "path": str(result.path),
        "size_bytes": result.size_bytes,
        "integrity": result.integrity,
        "retained_count": len(retention.retained_paths),
        "deleted_count": len(retention.deleted_paths),
        "deleted_bytes": retention.deleted_bytes,
        "oldest_preserved": True,
        "retain_newest": 7,
    }


def _state_restore_drill(config_path: Path) -> tuple[int, dict[str, object]]:
    try:
        from codex_dispatcher.control_host_backup import drill_latest_state_backup
        from codex_dispatcher.ssh_runtime import load_protected_ssh_config

        config = load_protected_ssh_config(config_path)
        database_path = config.scheduler.database_path
        result = drill_latest_state_backup(
            database_path,
            database_path.parent / "backups",
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return 1, {"ok": False, "state_restore_drill": True, "error": str(exc)}
    return 0, {
        "ok": True,
        "state_restore_drill": True,
        "source_path": str(result.source_path),
        "source_size_bytes": result.source_size_bytes,
        "restored_size_bytes": result.restored_size_bytes,
        "integrity": result.integrity,
        "foreign_key_violations": result.foreign_key_violations,
        "schema_migrations": list(result.schema_migrations),
        "source_age_seconds": result.source_age_seconds,
        "temporary_restore_removed": True,
    }


def _runner_capacity(
    config_path: Path, *, require_provision_admissible: bool = False
) -> tuple[int, dict[str, object]]:
    try:
        from codex_dispatcher.runner_disk import FusedWorkItemDisk
        from codex_dispatcher.runner_main import load_runner_configuration

        configuration = load_runner_configuration(config_path)
        if configuration.work_item_disk is None:
            raise ValueError("Runner WorkItem disk is not configured")
        snapshot = FusedWorkItemDisk(
            configuration.work_item_disk,
            work_items_root=configuration.work_items_root,
        ).capacity_snapshot()
    except (OSError, ValueError, RuntimeError) as exc:
        return 1, {"ok": False, "error": str(exc)}
    ok = not require_provision_admissible or snapshot.provision_admissible
    return (0 if ok else 1), {
        "ok": ok,
        "runner_capacity": True,
        "require_provision_admissible": require_provision_admissible,
        "capacity_bytes": snapshot.capacity_bytes,
        "available_bytes": snapshot.available_bytes,
        "image_size_bytes": snapshot.image_size_bytes,
        "host_reserve_bytes": snapshot.host_reserve_bytes,
        "turn_admissible": snapshot.turn_admissible,
        "provision_admissible": snapshot.provision_admissible,
        "provision_shortfall_bytes": snapshot.provision_shortfall_bytes,
    }


def _lifecycle_health(
    config_path: Path,
    *,
    check_systemd: bool,
    check_runner_capacity: bool = False,
    notify_slack: bool = False,
) -> tuple[int, dict[str, object]]:
    slack_token: str | None = None
    try:
        from codex_dispatcher.health_alert_delivery import (
            HealthAlertDeliveryCoordinator,
        )
        from codex_dispatcher.lifecycle_health import (
            MAX_REPORTED_ALERTS,
            inspect_lifecycle_health,
            inspect_runner_capacity,
            inspect_runner_reclamation_status,
            inspect_systemd_health,
        )
        from codex_dispatcher.slack_web_api import SlackWebApiPublisher
        from codex_dispatcher.ssh_runtime import load_protected_ssh_config

        config = load_protected_ssh_config(config_path)
        if notify_slack:
            if config.slack_runtime is None:
                raise RuntimeError("Slack health notifications require slack_runtime")
            if os.environ.get("CODEX_DISPATCHER_ENABLE_SLACK_WRITES") != "1":
                raise RuntimeError(
                    "CODEX_DISPATCHER_ENABLE_SLACK_WRITES=1 is required for Slack health notifications"
                )
            slack_token = _slack_bot_token()
            if slack_token is None:
                raise RuntimeError("a recognized explicit Slack bot token is required")
        with StateStore(
            config.scheduler.database_path, read_only=not notify_slack
        ) as store:
            snapshot = inspect_lifecycle_health(config, store)
            unit_states = ()
            systemd_alerts = ()
            if check_systemd:
                unit_states, systemd_alerts = inspect_systemd_health()
            runner_capacity = None
            runner_reclamation = None
            runner_alerts = ()
            if check_runner_capacity:
                runner_capacity, runner_alerts = inspect_runner_capacity(config)
                runner_reclamation, reclamation_alerts = (
                    inspect_runner_reclamation_status(config)
                )
                runner_alerts = tuple(runner_alerts) + tuple(reclamation_alerts)
            combined_alerts = (
                tuple(snapshot.alerts)
                + tuple(systemd_alerts)
                + tuple(runner_alerts)
            )
            alerts_truncated = snapshot.alerts_truncated or (
                len(combined_alerts) > MAX_REPORTED_ALERTS
            )
            combined_alerts = combined_alerts[:MAX_REPORTED_ALERTS]
            blocking_runner_alerts = tuple(
                alert
                for alert in runner_alerts
                if alert.code != "runner_reclamation_plan_ready"
            )
            notification = None
            if notify_slack:
                if snapshot.integrity != "ok" or snapshot.foreign_key_violations:
                    raise RuntimeError(
                        "unsafe to write health outbox while database checks fail"
                    )
                assert config.slack_runtime is not None
                assert slack_token is not None
                notification = HealthAlertDeliveryCoordinator(
                    store=store,
                    publisher=SlackWebApiPublisher(
                        bot_token=slack_token,
                        timeout_seconds=config.slack_runtime.request_timeout_seconds,
                    ),
                    channel_id=config.slack_runtime.system_channel_id,
                ).reconcile(
                    combined_alerts,
                    checked_at=snapshot.checked_at,
                    alerts_truncated=alerts_truncated,
                )
        payload = snapshot.to_mapping()
        payload["alerts"] = [alert.to_mapping() for alert in combined_alerts]
        payload["alert_count"] = len(combined_alerts)
        payload["alerts_truncated"] = alerts_truncated
        payload["ok"] = (
            snapshot.ok and not systemd_alerts and not blocking_runner_alerts
        )
        payload["systemd_checked"] = check_systemd
        if check_systemd:
            payload["systemd_units"] = [state.to_mapping() for state in unit_states]
        payload["runner_capacity_checked"] = check_runner_capacity
        if runner_capacity is not None:
            payload["runner_capacity"] = runner_capacity.to_mapping()
        if runner_reclamation is not None:
            payload["runner_reclamation"] = runner_reclamation.to_mapping()
        payload["slack_notification_enabled"] = notify_slack
        if notification is not None:
            payload["slack_notification"] = notification.to_mapping()
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        from codex_dispatcher.redaction import redact_text

        secrets = () if slack_token is None else (slack_token,)
        return 1, {
            "ok": False,
            "lifecycle_health": True,
            "error": redact_text(str(exc), secrets),
        }
    return (0 if payload["ok"] else 1), payload


def _github_token() -> str | None:
    """Return only a provider token shape recognized by the dispatcher."""
    for variable in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(variable)
        if token is not None and token.startswith(("github_pat_", "ghp_")):
            return token
    return None


def _slack_bot_token() -> str | None:
    """Return only the bot-token shape accepted by the outbound publisher."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if (
        token is not None
        and token.startswith("xoxb-")
        and 16 <= len(token) <= 512
        and not any(character.isspace() or ord(character) < 32 for character in token)
    ):
        return token
    return None


def _schema18_disaster_recovery(
    *,
    config_path: Path,
    recovery_root: Path,
    control_release: Path,
    release_receipt: Path,
    handoff_receipt: Path,
    runner_snapshot_path: Path,
) -> tuple[int, dict[str, object]]:
    from codex_dispatcher.redaction import redact_text

    github_token = _github_token()
    slack_token = _slack_bot_token()
    secrets = tuple(
        value for value in (github_token, slack_token) if value is not None
    )
    if github_token is None:
        return 1, {
            "ok": False,
            "schema18_disaster_recovery": True,
            "error": "a recognized explicit GitHub token is required",
        }
    try:
        from codex_dispatcher.disaster_recovery import (
            load_runner_recovery_snapshot,
            run_schema18_disaster_recovery_drill,
        )
        from codex_dispatcher.slack_web_api import SlackWebApiPublisher
        from codex_dispatcher.ssh_runtime import (
            load_protected_ssh_config,
            validate_runtime_state_path,
        )
        from codex_dispatcher.trackers.github_cli import GitHubCliTracker

        config = load_protected_ssh_config(config_path)
        validate_runtime_state_path(config.scheduler.database_path)
        runtime = config.ssh_runtime
        if runtime is None:
            raise RuntimeError("schema-18 disaster recovery requires ssh_runtime")
        slack_verifier = None
        if config.slack_runtime is not None:
            if slack_token is None:
                raise RuntimeError("configured Slack runtime requires an explicit bot token")
            slack_verifier = SlackWebApiPublisher(
                bot_token=slack_token,
                timeout_seconds=config.slack_runtime.request_timeout_seconds,
            )
        result = run_schema18_disaster_recovery_drill(
            database_path=config.scheduler.database_path,
            backup_directory=config.scheduler.database_path.parent / "backups",
            recovery_root=recovery_root,
            control_release_path=control_release,
            control_config_path=config_path,
            release_receipt_path=release_receipt,
            handoff_receipt_path=handoff_receipt,
            runner_snapshot=load_runner_recovery_snapshot(runner_snapshot_path),
            tracker=GitHubCliTracker(
                gh_path=runtime.gh_path,
                token=github_token,
                timeout_seconds=runtime.operation_timeout_seconds,
            ),
            slack_verifier=slack_verifier,
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return 1, {
            "ok": False,
            "schema18_disaster_recovery": True,
            "error": redact_text(str(exc), secrets),
        }
    payload = result.to_mapping()
    payload.update(
        ok=True,
        schema18_disaster_recovery=True,
        receipt_path=str(result.receipt_path),
    )
    return 0, payload


def _ssh_run_once(config_path: Path) -> tuple[int, dict[str, object]]:
    if os.environ.get("CODEX_DISPATCHER_ENABLE_SSH_WRITES") != "1":
        return 1, {
            "ok": False,
            "error": "CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 is required",
        }
    token = _github_token()
    if token is None:
        return 1, {"ok": False, "error": "a recognized explicit GitHub token is required"}
    slack_token: str | None = None
    try:
        from codex_dispatcher.redaction import redact_text
        from codex_dispatcher.ssh_runtime import (
            load_protected_ssh_config,
            run_ssh_control_sweep,
            validate_runtime_state_path,
        )

        config = load_protected_ssh_config(config_path)
        if config.slack_runtime is not None:
            if os.environ.get("CODEX_DISPATCHER_ENABLE_SLACK_WRITES") != "1":
                raise RuntimeError(
                    "CODEX_DISPATCHER_ENABLE_SLACK_WRITES=1 is required for configured Slack output"
                )
            slack_token = _slack_bot_token()
            if slack_token is None:
                raise RuntimeError(
                    "a recognized explicit Slack bot token is required"
                )
        validate_runtime_state_path(config.scheduler.database_path)
        with StateStore(config.scheduler.database_path) as store:
            store.migrate()
            if store.integrity_check() != "ok":
                return 1, {
                    "ok": False,
                    "error": "state database integrity check failed",
                }
            result = run_ssh_control_sweep(
                config=config,
                store=store,
                github_token=token,
                slack_token=slack_token,
            )
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        secrets = (token,) if slack_token is None else (token, slack_token)
        return 1, {
            "ok": False,
            "error": redact_text(str(exc), secrets),
        }
    return 0, {
        "ok": True,
        "ssh_run": True,
        "status": result.status.value,
        "repository": result.repository,
        "issue_number": result.issue_number,
        "work_item_id": result.work_item_id,
        "turn_id": result.turn_id,
        "reason": result.reason,
        "github_api": (
            None if result.github_api is None else result.github_api.to_mapping()
        ),
    }


def _ssh_preflight(config_path: Path) -> tuple[int, dict[str, object]]:
    token = _github_token()
    if token is None:
        return 1, {"ok": False, "error": "a recognized explicit GitHub token is required"}
    try:
        from codex_dispatcher.redaction import redact_text
        from codex_dispatcher.ssh_preflight import SshPreflightStatus
        from codex_dispatcher.ssh_runtime import (
            load_protected_ssh_config,
            run_ssh_preflight,
            validate_runtime_state_path,
        )

        config = load_protected_ssh_config(config_path)
        validate_runtime_state_path(config.scheduler.database_path)
        inspection = run_ssh_preflight(config=config, github_token=token)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return 1, {
            "ok": False,
            "error": redact_text(str(exc), (token,)),
        }

    plan = inspection.plan
    task = plan.task
    work_item = plan.work_item
    turn = plan.turn
    pull_request = plan.pull_request
    ok = plan.status is not SshPreflightStatus.BLOCKED
    return (0 if ok else 1), {
        "ok": ok,
        "ssh_preflight": True,
        "external_writes": False,
        "authorizes_apply": False,
        "status": plan.status.value,
        "recovery_action": plan.recovery_action.value,
        "repository": None if task is None else task.repository,
        "issue_number": None if task is None else task.issue_number,
        "work_item_id": None if work_item is None else work_item.work_item_id,
        "turn_id": None if turn is None else turn.turn_id,
        "pr_number": None if pull_request is None else pull_request.number,
        "reason": plan.reason,
        "repository_recovery_receipt": (
            None
            if plan.repository_recovery_receipt is None
            else plan.repository_recovery_receipt.to_mapping()
        ),
        "repository_target_readback_verdict": (
            None
            if plan.repository_target_readback_verdict is None
            else plan.repository_target_readback_verdict.to_mapping()
        ),
        "database_preexisting": inspection.database_preexisting,
        "rejected": [
            {
                "repository": rejection.repository,
                "issue_number": rejection.issue_number,
                "code": rejection.code,
            }
            for rejection in plan.rejected
        ],
        "checks": [
            {
                "name": f"contract:{check.name}",
                "ok": check.ok,
                "detail": check.detail,
                "required_now": True,
            }
            for check in inspection.checks
        ],
    }


def _ssh_reconcile_absence(
    config_path: Path, *, repository: str, issue_number: int
) -> tuple[int, dict[str, object]]:
    if os.environ.get("CODEX_DISPATCHER_ENABLE_SSH_WRITES") != "1":
        return 1, {
            "ok": False,
            "error": "CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 is required",
        }
    token = _github_token()
    if token is None:
        return 1, {"ok": False, "error": "a recognized explicit GitHub token is required"}
    try:
        from codex_dispatcher.redaction import redact_text
        from codex_dispatcher.ssh_runtime import (
            load_protected_ssh_config,
            run_ssh_absence_reconciliation,
            validate_runtime_state_path,
        )

        config = load_protected_ssh_config(config_path)
        validate_runtime_state_path(config.scheduler.database_path)
        with StateStore(config.scheduler.database_path) as store:
            store.migrate()
            if store.integrity_check() != "ok":
                raise RuntimeError("state database integrity check failed")
            receipt = run_ssh_absence_reconciliation(
                config=config,
                store=store,
                github_token=token,
                repository=repository,
                issue_number=issue_number,
            )
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return 1, {
            "ok": False,
            "error": redact_text(str(exc), (token,)),
            "repository": repository,
            "issue_number": issue_number,
        }
    return 0, {
        "ok": True,
        "ssh_absence_reconciliation": True,
        "repository": repository,
        "issue_number": issue_number,
        "work_item_id": receipt.work_item_id,
        "expected_head_sha": receipt.expected_head_sha,
        "evidence_sha256": receipt.evidence_sha256,
        "observed_by": receipt.observed_by,
        "observed_at": receipt.observed_at,
    }


def _ssh_abandon_unknown_turn(
    config_path: Path, *, turn_id: str, apply: bool
) -> tuple[int, dict[str, object]]:
    if apply:
        if os.environ.get("CODEX_DISPATCHER_ENABLE_SSH_WRITES") != "1":
            return 1, {
                "ok": False,
                "error": "CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 is required",
            }
        if os.environ.get("CODEX_DISPATCHER_ENABLE_TURN_ABANDON") != "1":
            return 1, {
                "ok": False,
                "error": "CODEX_DISPATCHER_ENABLE_TURN_ABANDON=1 is required",
            }
    try:
        from codex_dispatcher.ssh_runtime import (
            load_protected_ssh_config,
            run_ssh_turn_abandonment,
            validate_runtime_state_path,
        )

        config = load_protected_ssh_config(config_path)
        validate_runtime_state_path(config.scheduler.database_path)
        with StateStore(
            config.scheduler.database_path, read_only=not apply
        ) as store:
            if apply:
                store.migrate()
            if store.integrity_check() != "ok":
                raise RuntimeError("state database integrity check failed")
            result = run_ssh_turn_abandonment(
                config=config,
                store=store,
                turn_id=turn_id,
                apply=apply,
            )
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return 1, {
            "ok": False,
            "ssh_turn_abandonment": True,
            "turn_id": turn_id,
            "error": str(exc),
        }
    inspection = result.inspection
    payload: dict[str, object] = {
        "ok": True,
        "ssh_turn_abandonment": True,
        "mode": "apply" if apply else "plan",
        "external_writes": apply,
        "state_writes": None if apply else 0,
        "authorizes_apply": False,
        "work_item_id": inspection.work_item_id,
        "turn_id": inspection.turn_id,
        "session_generation_id": inspection.session_generation_id,
        "session_generation": inspection.session_generation,
        "agent_policy_digest": inspection.agent_policy_digest,
        "session_id": inspection.session_id,
        "inactive_container_state": inspection.inactive_container_state.value,
        "inactive_observed_at": inspection.inactive_observed_at,
    }
    if result.receipt is not None:
        payload["recorded_at"] = result.receipt.recorded_at
        payload["committed_inactive_container_state"] = (
            result.receipt.inactive_container_state.value
        )
        payload["committed_inactive_observed_at"] = (
            result.receipt.inactive_observed_at
        )
    return 0, payload


def _slack_idempotency_fixture(
    *,
    workspace_id: str,
    channel_id: str,
    fixture_id: str,
    timeout_seconds: float,
) -> tuple[int, dict[str, object]]:
    if os.environ.get("CODEX_DISPATCHER_ENABLE_SLACK_FIXTURE_WRITES") != "1":
        return 1, {
            "ok": False,
            "error": (
                "CODEX_DISPATCHER_ENABLE_SLACK_FIXTURE_WRITES=1 is required"
            ),
        }
    token = _slack_bot_token()
    if token is None:
        return 1, {
            "ok": False,
            "error": "a recognized explicit Slack bot token is required",
        }
    try:
        from codex_dispatcher.redaction import redact_text
        from codex_dispatcher.slack_live_fixture import (
            run_slack_idempotency_fixture,
        )

        result = run_slack_idempotency_fixture(
            bot_token=token,
            workspace_id=workspace_id,
            channel_id=channel_id,
            fixture_id=fixture_id,
            timeout_seconds=timeout_seconds,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        return 1, {
            "ok": False,
            "error": redact_text(str(exc), (token,)),
            "fixture_id": fixture_id,
            "channel_id": channel_id,
        }
    receipt = result.receipt
    return 0, {
        "ok": True,
        "slack_idempotency_fixture": True,
        "workspace_id": result.workspace_id,
        "channel_id": result.channel_id,
        "fixture_id": result.fixture_id,
        "client_msg_id": result.client_msg_id,
        "message_ts": receipt.message_ts,
        "permalink": receipt.permalink,
        "exact_retry_receipt_match": True,
        "expected_visible_messages": 1,
        "manual_confirmation_required": True,
        "authorizes_runtime": False,
    }


def _emit(payload: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return

    print(f"ok: {str(payload.get('ok', False)).lower()}")
    for check in payload.get("checks", []):
        if isinstance(check, dict):
            marker = "PASS" if check.get("ok") else "WARN"
            print(f"{marker} {check.get('name')}: {check.get('detail')}")
    if "integrity" in payload and payload.get("lifecycle_health") is not True:
        print(f"integrity: {payload['integrity']}")
    if "error" in payload:
        print(f"error: {payload['error']}", file=sys.stderr)
    if payload.get("dry_run") is True:
        print(f"selected: {len(payload.get('selected', []))}")
        print(f"rejected: {len(payload.get('rejected', []))}")
    if payload.get("ssh_run") is True:
        print(f"status: {payload.get('status')}")
        if payload.get("repository") is not None:
            print(f"repository: {payload.get('repository')}")
            print(f"issue_number: {payload.get('issue_number')}")
    if payload.get("ssh_preflight") is True:
        print(f"status: {payload.get('status')}")
        print(f"recovery_action: {payload.get('recovery_action')}")
        if payload.get("repository") is not None:
            print(f"repository: {payload.get('repository')}")
            print(f"issue_number: {payload.get('issue_number')}")
    if payload.get("ssh_absence_reconciliation") is True:
        print(f"repository: {payload.get('repository')}")
        print(f"issue_number: {payload.get('issue_number')}")
        print(f"work_item_id: {payload.get('work_item_id')}")
        print(f"evidence_sha256: {payload.get('evidence_sha256')}")
    if payload.get("ssh_turn_abandonment") is True:
        print(f"mode: {payload.get('mode')}")
        print(f"work_item_id: {payload.get('work_item_id')}")
        print(f"turn_id: {payload.get('turn_id')}")
        print(
            "inactive_container_state: "
            f"{payload.get('inactive_container_state')}"
        )
    if payload.get("slack_idempotency_fixture") is True:
        print(f"workspace_id: {payload.get('workspace_id')}")
        print(f"channel_id: {payload.get('channel_id')}")
        print(f"fixture_id: {payload.get('fixture_id')}")
        print(f"permalink: {payload.get('permalink')}")
        print("manual_confirmation_required: true")
    if payload.get("state_backup") is True:
        print(f"path: {payload.get('path')}")
        print(f"size_bytes: {payload.get('size_bytes')}")
        print(f"integrity: {payload.get('integrity')}")
        print(f"retained_count: {payload.get('retained_count')}")
        print(f"deleted_count: {payload.get('deleted_count')}")
        print(f"deleted_bytes: {payload.get('deleted_bytes')}")
    if payload.get("state_restore_drill") is True:
        print(f"source_path: {payload.get('source_path')}")
        print(f"integrity: {payload.get('integrity')}")
        print(f"foreign_key_violations: {payload.get('foreign_key_violations')}")
        print(f"source_age_seconds: {payload.get('source_age_seconds')}")
    if payload.get("schema18_disaster_recovery") is True and payload.get("ok") is True:
        print(f"receipt_path: {payload.get('receipt_path')}")
        print(f"rto_milliseconds: {payload.get('rto_milliseconds')}")
        print(f"release_commit: {payload.get('release_commit')}")
    if payload.get("runner_capacity") is True:
        print(f"capacity_bytes: {payload.get('capacity_bytes')}")
        print(f"available_bytes: {payload.get('available_bytes')}")
        print(f"turn_admissible: {str(payload.get('turn_admissible')).lower()}")
        print(f"provision_admissible: {str(payload.get('provision_admissible')).lower()}")
        print(f"provision_shortfall_bytes: {payload.get('provision_shortfall_bytes')}")
    if payload.get("lifecycle_health") is True:
        print(f"integrity: {payload.get('integrity')}")
        print(f"active_turns: {payload.get('active_turns')}")
        print(f"blocked_work_items: {payload.get('blocked_work_items')}")
        print(f"abandoned_turns: {payload.get('abandoned_turns')}")
        print(f"pending_archives: {payload.get('pending_archives')}")
        print(f"alert_count: {payload.get('alert_count')}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "doctor":
        code, payload = _doctor(args.config)
        _emit(payload, args.json)
        return code
    if args.command == "status":
        code, payload = _status(args.database)
        _emit(payload, args.json)
        return code
    if args.command == "runner-capacity":
        code, payload = _runner_capacity(
            args.config,
            require_provision_admissible=args.require_provision_admissible,
        )
        _emit(payload, args.json)
        return code
    if args.command == "lifecycle-health":
        code, payload = _lifecycle_health(
            args.config,
            check_systemd=args.systemd,
            check_runner_capacity=args.runner_capacity,
            notify_slack=args.notify_slack,
        )
        _emit(payload, args.json)
        return code
    if args.command == "state-backup":
        code, payload = _state_backup(args.config)
        _emit(payload, args.json)
        return code
    if args.command == "state-restore-drill":
        code, payload = _state_restore_drill(args.config)
        _emit(payload, args.json)
        return code
    if args.command == "schema18-disaster-recovery":
        code, payload = _schema18_disaster_recovery(
            config_path=args.config,
            recovery_root=args.recovery_root,
            control_release=args.control_release,
            release_receipt=args.release_receipt,
            handoff_receipt=args.handoff_receipt,
            runner_snapshot_path=args.runner_snapshot,
        )
        _emit(payload, args.json)
        return code
    if args.command == "ssh-run-once":
        code, payload = _ssh_run_once(args.config)
        _emit(payload, args.json)
        return code
    if args.command == "ssh-preflight":
        code, payload = _ssh_preflight(args.config)
        _emit(payload, args.json)
        return code
    if args.command == "ssh-reconcile-absence":
        code, payload = _ssh_reconcile_absence(
            args.config,
            repository=args.repository,
            issue_number=args.issue_number,
        )
        _emit(payload, args.json)
        return code
    if args.command == "ssh-abandon-unknown-turn":
        code, payload = _ssh_abandon_unknown_turn(
            args.config,
            turn_id=args.turn_id,
            apply=args.apply,
        )
        _emit(payload, args.json)
        return code
    if args.command == "slack-idempotency-fixture":
        code, payload = _slack_idempotency_fixture(
            workspace_id=args.workspace_id,
            channel_id=args.channel_id,
            fixture_id=args.fixture_id,
            timeout_seconds=args.request_timeout_seconds,
        )
        _emit(payload, args.json)
        return code
    raise AssertionError(f"unhandled command: {args.command}")
