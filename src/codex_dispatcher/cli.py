"""Offline-safe command line entry points.

Networked adapters and write commands are deliberately absent from the first
implementation phase.
"""

from __future__ import annotations

import argparse
import json
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
        description="Fail-closed GitHub to Codex Cloud task dispatcher.",
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
    return parser


def run_once_dry_run(
    config: Config, tracker: Tracker, active_runs: Sequence[Run] = ()
) -> DryRunPlan:
    """Dependency-injected entry point for a read-only scheduling sweep."""
    return build_dry_run_plan(config, tracker, active_runs)


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
            active_runs = store.list_active_runs()
    except (OSError, ValueError, sqlite3.Error) as exc:
        return 1, {"ok": False, "error": str(exc), "path": str(database_path)}

    return 0, {
        "ok": integrity == "ok",
        "integrity": integrity,
        "active_runs": [run.run_id for run in active_runs],
        "path": str(database_path),
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
    if "integrity" in payload:
        print(f"integrity: {payload['integrity']}")
        print(f"active_runs: {len(payload.get('active_runs', []))}")
    if "error" in payload:
        print(f"error: {payload['error']}", file=sys.stderr)


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
    raise AssertionError(f"unhandled command: {args.command}")
