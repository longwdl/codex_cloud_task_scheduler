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
        description="Fail-closed GitHub to Codex Cloud task dispatcher.",
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
    if payload.get("dry_run") is True:
        print(f"selected: {len(payload.get('selected', []))}")
        print(f"rejected: {len(payload.get('rejected', []))}")


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
    if args.command == "run-once":
        code, payload = _run_once(args.config)
        _emit(payload, args.json)
        return code
    raise AssertionError(f"unhandled command: {args.command}")
