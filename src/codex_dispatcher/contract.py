"""Read-only external CLI contract checks for pinned tool versions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from codex_dispatcher.config import ToolPins
from codex_dispatcher.executors.codex_cloud_cli import CodexCloudCliExecutor


@dataclass(frozen=True, slots=True)
class ContractCheck:
    name: str
    ok: bool
    detail: str


def run_contract_checks(
    *,
    pins: ToolPins,
    git_path: Path,
    gh_path: Path,
    codex_path: Path,
    cloud_environment_ids: tuple[str, ...],
) -> tuple[ContractCheck, ...]:
    """Check exact versions and visible Cloud environments without external writes."""
    checks = (
        _version_check("git", git_path, ("--version",), pins.git_version),
        _version_check("gh", gh_path, ("--version",), pins.gh_version),
        _version_check("codex", codex_path, ("--version",), pins.codex_version),
    )
    if not all(check.ok for check in checks):
        return checks + tuple(
            ContractCheck(
                f"codex-cloud:{environment_id}",
                False,
                "skipped because a tool version does not match its pin",
            )
            for environment_id in cloud_environment_ids
        )
    cloud = CodexCloudCliExecutor(codex_path=codex_path)
    environment_checks: list[ContractCheck] = []
    for environment_id in cloud_environment_ids:
        try:
            result = cloud.preflight(environment_id)
        except (OSError, ValueError, RuntimeError) as exc:
            environment_checks.append(
                ContractCheck(
                    f"codex-cloud:{environment_id}",
                    False,
                    f"preflight failed: {type(exc).__name__}",
                )
            )
        else:
            environment_checks.append(
                ContractCheck(
                    f"codex-cloud:{environment_id}",
                    result.ok,
                    result.detail or ("ok" if result.ok else "failed"),
                )
            )
    return checks + tuple(environment_checks)


def run_control_host_contract_checks(
    *,
    pins: ToolPins,
    git_path: Path,
    gh_path: Path,
    ssh_path: Path,
) -> tuple[ContractCheck, ...]:
    """Check only the Control Host tools used by the SSH dispatcher path."""
    if pins.ssh_version is None:
        return (
            _version_check("git", git_path, ("--version",), pins.git_version),
            _version_check("gh", gh_path, ("--version",), pins.gh_version),
            ContractCheck("ssh", False, "ssh_version pin is missing"),
        )
    return (
        _version_check("git", git_path, ("--version",), pins.git_version),
        _version_check("gh", gh_path, ("--version",), pins.gh_version),
        _version_check(
            "ssh",
            ssh_path,
            ("-V",),
            pins.ssh_version,
            output_field="stderr",
        ),
    )


def _version_check(
    name: str,
    executable: Path,
    arguments: tuple[str, ...],
    expected: str,
    *,
    output_field: str = "stdout",
) -> ContractCheck:
    from codex_dispatcher.command_runner import run_command

    result = run_command((str(executable), *arguments), timeout_seconds=10.0)
    if result.returncode != 0 or result.error is not None or result.timed_out:
        return ContractCheck(name, False, "version command failed")
    output = result.stderr if output_field == "stderr" else result.stdout
    first_line = output.strip().splitlines()[0] if output.strip() else ""
    match = re.search(r"(?<![0-9.])([0-9]+(?:\.[0-9]+)+)(?![0-9.])", first_line)
    actual = match.group(1) if match is not None else ""
    if actual != expected:
        return ContractCheck(name, False, f"expected {expected}, got {actual or 'empty'}")
    return ContractCheck(name, True, actual)
