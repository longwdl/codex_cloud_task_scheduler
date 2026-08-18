"""Standalone, triple-opt-in entry point for the dedicated live Fixture."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from codex_dispatcher.fixture_faults import (
    FIXTURE_REPOSITORY,
    FixtureFaultInjection,
    FixtureFaultPoint,
    FixtureFaultRejected,
    FixtureReceiptLost,
    validate_fixture_config,
    validate_fixture_preflight,
)
from codex_dispatcher.redaction import redact_text
from codex_dispatcher.ssh_runtime import (
    SshRuntimeError,
    build_ssh_fixture_fault_sweep,
    load_protected_ssh_config,
    run_ssh_preflight,
    validate_runtime_state_path,
)
from codex_dispatcher.state_store import StateStore


_FAULT_ENV = "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS"
_WRITE_ENV = "CODEX_DISPATCHER_ENABLE_SSH_WRITES"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m codex_dispatcher.fixture_fault_cli",
        description="Discard one successful receipt in the fixed private Fixture.",
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument(
        "--fault",
        required=True,
        choices=tuple(point.value for point in FixtureFaultPoint),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        required=True,
        help="Required acknowledgement that this performs a real Fixture write.",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def _github_token() -> str | None:
    for variable in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(variable)
        if token is not None and token.startswith(("github_pat_", "ghp_")):
            return token
    return None


def _create_backup(database_path: Path, fault: FixtureFaultPoint) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f"state.pre-{fault.value}-",
        suffix=".db",
        dir=database_path.parent,
    )
    os.close(descriptor)
    backup_path = Path(raw_path)
    os.chmod(backup_path, 0o600)
    try:
        with StateStore(database_path, read_only=True) as source:
            if source.integrity_check() != "ok":
                raise SshRuntimeError("state database integrity check failed before backup")
            source.backup(backup_path)
        with StateStore(backup_path, read_only=True) as backup:
            if backup.integrity_check() != "ok":
                raise SshRuntimeError("Fixture fault backup integrity check failed")
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def _run(
    config_path: Path,
    *,
    issue_number: int,
    fault: FixtureFaultPoint,
) -> tuple[int, dict[str, object]]:
    if os.environ.get(_WRITE_ENV) != "1":
        return 1, {"ok": False, "error": f"{_WRITE_ENV}=1 is required"}
    if os.environ.get(_FAULT_ENV) != FIXTURE_REPOSITORY:
        return 1, {
            "ok": False,
            "error": f"{_FAULT_ENV} must equal the fixed Fixture repository",
        }
    token = _github_token()
    if token is None:
        return 1, {"ok": False, "error": "a recognized explicit GitHub token is required"}
    if type(issue_number) is not int or issue_number <= 0:
        return 1, {"ok": False, "error": "fixture issue number must be positive"}

    backup_path: Path | None = None
    injection: FixtureFaultInjection | None = None
    try:
        config = load_protected_ssh_config(config_path)
        validate_runtime_state_path(config.scheduler.database_path)
        validate_fixture_config(config)
        if not config.scheduler.database_path.is_file():
            raise FixtureFaultRejected("Fixture fault requires an existing state database")

        inspection = run_ssh_preflight(config=config, github_token=token)
        validate_fixture_preflight(
            inspection.plan,
            fault=fault,
            issue_number=issue_number,
        )
        backup_path = _create_backup(config.scheduler.database_path, fault)
        injection = FixtureFaultInjection(fault=fault, issue_number=issue_number)
        try:
            with StateStore(config.scheduler.database_path) as store:
                store.migrate()
                if store.integrity_check() != "ok":
                    raise SshRuntimeError("state database integrity check failed")
                sweep = build_ssh_fixture_fault_sweep(
                    config=config,
                    store=store,
                    github_token=token,
                    injection=injection,
                )
                result = sweep.run_once()
        except FixtureReceiptLost:
            result = None

        if not injection.triggered:
            raise FixtureFaultRejected("requested Fixture fault point was not reached")
        if fault is FixtureFaultPoint.PUBLISHER_RECEIPT:
            if result is None or result.status.value != "awaiting_publication":
                raise FixtureFaultRejected(
                    "Publisher receipt fault did not enter publication recovery"
                )
            status = result.status.value
            work_item_id = result.work_item_id
            turn_id = result.turn_id
        else:
            if result is not None:
                raise FixtureFaultRejected("Fixture receipt loss unexpectedly returned a sweep result")
            status = "receipt_lost"
            work_item_id = inspection.plan.work_item.work_item_id
            turn_id = None
        return 0, {
            "ok": True,
            "fixture_fault": True,
            "fault": fault.value,
            "fault_triggered": True,
            "recovery_required": True,
            "repository": FIXTURE_REPOSITORY,
            "issue_number": issue_number,
            "work_item_id": work_item_id,
            "turn_id": turn_id,
            "status": status,
            "backup_path": str(backup_path),
        }
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return 1, {
            "ok": False,
            "fixture_fault": True,
            "fault": fault.value,
            "fault_triggered": False if injection is None else injection.triggered,
            "repository": FIXTURE_REPOSITORY,
            "issue_number": issue_number,
            "error": redact_text(str(exc), (token,)),
            "backup_path": None if backup_path is None else str(backup_path),
        }


def _emit(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    print(f"ok: {str(payload.get('ok', False)).lower()}")
    if "error" in payload:
        print(f"error: {payload['error']}", file=sys.stderr)
    else:
        print(f"fault: {payload.get('fault')}")
        print(f"status: {payload.get('status')}")
        print(f"recovery_required: {str(payload.get('recovery_required', False)).lower()}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    code, payload = _run(
        args.config,
        issue_number=args.issue,
        fault=FixtureFaultPoint(args.fault),
    )
    _emit(payload, as_json=args.json)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
