"""Standalone triple-opt-in entry point for the fixed higher-value canary."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from codex_dispatcher.higher_value_canary import (
    HigherValueCanaryRejected,
    validate_higher_value_canary_config,
)
from codex_dispatcher.redaction import redact_text
from codex_dispatcher.repository_admission import (
    HIGHER_VALUE_CANARY_REPOSITORY,
    HigherValueCanaryTarget,
)
from codex_dispatcher.ssh_preflight import SshPreflightStatus
from codex_dispatcher.ssh_runtime import (
    SshRuntimeError,
    build_ssh_higher_value_canary_sweep,
    load_protected_ssh_config,
    run_ssh_higher_value_canary_preflight,
    validate_runtime_state_path,
)
from codex_dispatcher.state_store import StateStore


_CANARY_ENV = "CODEX_DISPATCHER_ENABLE_HIGHER_VALUE_CANARY"
_WRITE_ENV = "CODEX_DISPATCHER_ENABLE_SSH_WRITES"
_SLACK_WRITE_ENV = "CODEX_DISPATCHER_ENABLE_SLACK_WRITES"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m codex_dispatcher.higher_value_canary_cli",
        description=(
            "Run one guarded sweep for one exact Issue in the fixed private "
            "higher-value canary repository."
        ),
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--expected-issue-node-id", required=True)
    parser.add_argument("--expected-base-sha", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        required=True,
        help="Required acknowledgement that this may mutate the isolated canary.",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def _github_token() -> str | None:
    for variable in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(variable)
        if token is not None and token.startswith(("github_pat_", "ghp_")):
            return token
    return None


def _slack_bot_token() -> str | None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if (
        token is not None
        and token.startswith("xoxb-")
        and 16 <= len(token) <= 512
        and not any(character.isspace() or ord(character) < 32 for character in token)
    ):
        return token
    return None


def _create_backup(database_path: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix="state.pre-higher-value-canary-",
        suffix=".db",
        dir=database_path.parent,
    )
    os.close(descriptor)
    backup_path = Path(raw_path)
    os.chmod(backup_path, 0o600)
    try:
        with StateStore(database_path, read_only=True) as source:
            if source.integrity_check() != "ok":
                raise SshRuntimeError(
                    "higher-value canary state integrity check failed before backup"
                )
            source.backup(backup_path)
        with StateStore(backup_path, read_only=True) as backup:
            if backup.integrity_check() != "ok":
                raise SshRuntimeError("higher-value canary backup integrity check failed")
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def _validate_preflight(config, inspection, target: HigherValueCanaryTarget) -> None:
    plan = inspection.plan
    identities = []
    if plan.task is not None:
        identities.append(
            (
                plan.task.repository,
                plan.task.issue_number,
                plan.task.issue_node_id,
            )
        )
    if plan.work_item is not None:
        identities.append(
            (
                plan.work_item.repository,
                plan.work_item.issue_number,
                target.issue_node_id,
            )
        )
    if any(
        not target.matches_issue(
            repository=repository,
            issue_number=issue_number,
            issue_node_id=issue_node_id,
        )
        for repository, issue_number, issue_node_id in identities
    ):
        raise HigherValueCanaryRejected(
            "preflight selected state outside the exact higher-value canary target"
        )
    if plan.status is SshPreflightStatus.READY_CANDIDATE and plan.task is None:
        raise HigherValueCanaryRejected("higher-value canary candidate is missing")
    if plan.status is SshPreflightStatus.IDLE:
        database_path = config.scheduler.database_path
        existing = None
        if database_path.is_file():
            with StateStore(database_path, read_only=True) as store:
                existing = store.get_work_item_by_issue(
                    target.repository, target.issue_number
                )
        if existing is None:
            raise HigherValueCanaryRejected(
                "exact higher-value canary Issue is not an eligible candidate"
            )
    elif not identities:
        raise HigherValueCanaryRejected(
            "higher-value canary preflight did not bind an exact target"
        )


def _run(
    config_path: Path,
    *,
    issue_number: int,
    expected_issue_node_id: str,
    expected_base_sha: str,
) -> tuple[int, dict[str, object]]:
    if os.environ.get(_WRITE_ENV) != "1":
        return 1, {"ok": False, "error": f"{_WRITE_ENV}=1 is required"}
    if os.environ.get(_CANARY_ENV) != HIGHER_VALUE_CANARY_REPOSITORY:
        return 1, {
            "ok": False,
            "error": f"{_CANARY_ENV} must equal the fixed private canary repository",
        }
    token = _github_token()
    if token is None:
        return 1, {"ok": False, "error": "a recognized explicit GitHub token is required"}
    slack_token: str | None = None
    backup_path: Path | None = None
    try:
        target = HigherValueCanaryTarget(
            HIGHER_VALUE_CANARY_REPOSITORY,
            issue_number,
            expected_issue_node_id,
            expected_base_sha,
        )
        config = load_protected_ssh_config(config_path)
        validate_runtime_state_path(config.scheduler.database_path)
        validate_higher_value_canary_config(config, target)
        if config.slack_runtime is not None:
            if os.environ.get(_SLACK_WRITE_ENV) != "1":
                raise HigherValueCanaryRejected(
                    f"{_SLACK_WRITE_ENV}=1 is required for configured Slack output"
                )
            slack_token = _slack_bot_token()
            if slack_token is None:
                raise HigherValueCanaryRejected(
                    "a recognized explicit Slack bot token is required"
                )
        inspection = run_ssh_higher_value_canary_preflight(
            config=config,
            github_token=token,
            target=target,
        )
        _validate_preflight(config, inspection, target)
        if config.scheduler.database_path.is_file():
            backup_path = _create_backup(config.scheduler.database_path)
        with StateStore(config.scheduler.database_path) as store:
            store.migrate()
            if store.integrity_check() != "ok":
                raise SshRuntimeError("higher-value canary state integrity check failed")
            sweep = build_ssh_higher_value_canary_sweep(
                config=config,
                store=store,
                github_token=token,
                target=target,
                slack_token=slack_token,
            )
            result = sweep.run_once()
        return 0, {
            "ok": True,
            "higher_value_canary": True,
            "ordinary_higher_value_admission": False,
            "repository": result.repository or target.repository,
            "issue_number": result.issue_number or target.issue_number,
            "work_item_id": result.work_item_id,
            "turn_id": result.turn_id,
            "status": result.status.value,
            "reason": result.reason,
            "expected_base_sha": target.expected_base_sha,
            "backup_path": None if backup_path is None else str(backup_path),
        }
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        secrets = (token,) if slack_token is None else (token, slack_token)
        return 1, {
            "ok": False,
            "higher_value_canary": True,
            "ordinary_higher_value_admission": False,
            "repository": HIGHER_VALUE_CANARY_REPOSITORY,
            "issue_number": issue_number,
            "error": redact_text(str(exc), secrets),
            "backup_path": None if backup_path is None else str(backup_path),
        }


def _emit(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    print(f"ok: {str(payload.get('ok', False)).lower()}")
    if "error" in payload:
        print(f"error: {payload['error']}", file=sys.stderr)
        return
    print(f"status: {payload.get('status')}")
    print(f"work_item_id: {payload.get('work_item_id')}")
    print(f"turn_id: {payload.get('turn_id')}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    code, payload = _run(
        args.config,
        issue_number=args.issue,
        expected_issue_node_id=args.expected_issue_node_id,
        expected_base_sha=args.expected_base_sha,
    )
    _emit(payload, as_json=args.json)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
