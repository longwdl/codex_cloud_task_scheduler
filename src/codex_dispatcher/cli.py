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
from codex_dispatcher.config import Config, load_config
from codex_dispatcher.domain import Run
from codex_dispatcher.scheduler import DryRunPlan, build_dry_run_plan
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.trackers.base import Tracker


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
    doctor.add_argument(
        "--contract",
        action="store_true",
        help="Run read-only exact-version and Codex Cloud environment checks.",
    )
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

    run_once = subparsers.add_parser(
        "run-once", help="Plan one scheduler sweep without external writes."
    )
    run_once.add_argument("--config", required=True, type=Path)
    run_once.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
        help="Required safety flag; only tracker reads are allowed.",
    )
    run_once.add_argument("--json", action="store_true", help="Emit machine-readable output.")

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


def run_once_dry_run(
    config: Config, tracker: Tracker, active_runs: Sequence[Run] = ()
) -> DryRunPlan:
    """Dependency-injected entry point for a read-only scheduling sweep."""
    return build_dry_run_plan(config, tracker, active_runs)


def _doctor(
    config_path: Path | None, *, contract: bool = False
) -> tuple[int, dict[str, object]]:
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

    config: Config | None = None
    if config_path is not None:
        try:
            config = load_config(config_path)
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

    if contract:
        if config is None:
            checks.append(
                {
                    "name": "contract",
                    "ok": False,
                    "detail": "--contract requires a valid --config",
                    "required_now": True,
                }
            )
        else:
            locations = {tool: shutil.which(tool) for tool in ("git", "gh", "codex")}
            if any(location is None for location in locations.values()):
                checks.append(
                    {
                        "name": "contract",
                        "ok": False,
                        "detail": "git, gh, and codex executables are required",
                        "required_now": True,
                    }
                )
            else:
                from codex_dispatcher.contract import run_contract_checks

                contract_checks = run_contract_checks(
                    pins=config.tools,
                    git_path=Path(locations["git"] or ""),
                    gh_path=Path(locations["gh"] or ""),
                    codex_path=Path(locations["codex"] or ""),
                    cloud_environment_ids=tuple(
                        repository.cloud_environment_id
                        for repository in config.repositories
                    ),
                )
                checks.extend(
                    {
                        "name": f"contract:{check.name}",
                        "ok": check.ok,
                        "detail": check.detail,
                        "required_now": True,
                    }
                    for check in contract_checks
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
            active_runs = store.list_active_runs()
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return 1, {"ok": False, "error": str(exc), "path": str(database_path)}

    return 0, {
        "ok": integrity == "ok",
        "integrity": integrity,
        "active_runs": [run.run_id for run in active_runs],
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
    config_path: Path, *, check_systemd: bool, notify_slack: bool = False
) -> tuple[int, dict[str, object]]:
    slack_token: str | None = None
    try:
        from codex_dispatcher.health_alert_delivery import (
            HealthAlertDeliveryCoordinator,
        )
        from codex_dispatcher.lifecycle_health import (
            MAX_REPORTED_ALERTS,
            inspect_lifecycle_health,
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
            combined_alerts = tuple(snapshot.alerts) + tuple(systemd_alerts)
            alerts_truncated = snapshot.alerts_truncated or (
                len(combined_alerts) > MAX_REPORTED_ALERTS
            )
            combined_alerts = combined_alerts[:MAX_REPORTED_ALERTS]
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
                    channel_id=config.slack_runtime.channel_id,
                ).reconcile(
                    combined_alerts,
                    checked_at=snapshot.checked_at,
                    alerts_truncated=alerts_truncated,
                )
        payload = snapshot.to_mapping()
        payload["alerts"] = [alert.to_mapping() for alert in combined_alerts]
        payload["alert_count"] = len(combined_alerts)
        payload["alerts_truncated"] = alerts_truncated
        payload["ok"] = snapshot.ok and not systemd_alerts
        payload["systemd_checked"] = check_systemd
        if check_systemd:
            payload["systemd_units"] = [state.to_mapping() for state in unit_states]
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


def _run_once(config_path: Path) -> tuple[int, dict[str, object]]:
    gh_path = shutil.which("gh")
    if gh_path is None:
        return 1, {"ok": False, "error": "gh executable not found"}

    try:
        from codex_dispatcher.trackers.github_cli import GitHubCliTracker

        config = load_config(config_path)
        active_runs: Sequence[Run] = ()
        if config.scheduler.database_path.is_file():
            with StateStore(config.scheduler.database_path, read_only=True) as store:
                if store.integrity_check() != "ok":
                    return 1, {"ok": False, "error": "state database integrity check failed"}
                active_runs = store.list_active_runs()
        token = _github_token()
        tracker = GitHubCliTracker(gh_path=Path(gh_path), token=token)
        plan = run_once_dry_run(config, tracker, active_runs)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return 1, {"ok": False, "error": str(exc)}

    return 0, {
        "ok": True,
        "dry_run": True,
        "selected": [
            {
                "repository": task.repository,
                "issue_number": task.issue_number,
                "title": task.title,
            }
            for task in plan.selected
        ],
        "rejected": [
            {
                "repository": rejection.repository,
                "issue_number": rejection.issue_number,
                "code": rejection.code,
            }
            for rejection in plan.rejected
        ],
    }


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
        print(f"active_runs: {len(payload.get('active_runs', []))}")
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
        print(f"pending_archives: {payload.get('pending_archives')}")
        print(f"alert_count: {payload.get('alert_count')}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "doctor":
        code, payload = _doctor(args.config, contract=args.contract)
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
    if args.command == "run-once":
        code, payload = _run_once(args.config)
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
