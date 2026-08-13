"""Fail-closed Codex Cloud CLI contract adapter.

The pinned CLI exposes structured JSON only for ``cloud list``. Submission
places the prompt in argv and has no JSON response, so this adapter deliberately
keeps every write disabled until a later contract test proves a safe protocol.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Never

from codex_dispatcher.command_runner import CommandResult, run_command
from codex_dispatcher.executors.base import (
    DiffResult,
    PreflightResult,
    RemoteRun,
    SubmissionRequest,
)


class CodexCloudCliError(RuntimeError):
    """Raised when Codex Cloud CLI output cannot be safely interpreted."""


class CodexCloudCliWriteDisabledError(CodexCloudCliError):
    """Raised before an unsupported Cloud write can execute."""


class CodexCloudCliUnsupportedReadError(CodexCloudCliError):
    """Raised for reads without a structured contract in the pinned CLI."""


_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class CodexCloudCliExecutor:
    """Validate Cloud environment access without submitting or applying tasks."""

    def __init__(self, *, codex_path: str | Path, timeout_seconds: float = 30.0) -> None:
        candidate = str(codex_path)
        if not candidate or not Path(candidate).is_absolute():
            raise ValueError("codex_path must be a non-empty absolute path")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._codex_path = candidate
        self._timeout_seconds = timeout_seconds

    def preflight(self, environment_id: str) -> PreflightResult:
        """Prove the environment is visible through the pinned JSON list command."""
        environment_id = _validate_identifier(environment_id, "environment_id")
        payload = self._list_payload(environment_id, limit=1)
        tasks = payload["tasks"]
        assert isinstance(tasks, list)
        if tasks:
            return PreflightResult(
                False,
                "non-empty Codex Cloud task schema has not passed a contract test",
            )
        return PreflightResult(True, "environment is visible; submit remains disabled")

    def list_runs(self, environment_id: str) -> tuple[RemoteRun, ...]:
        environment_id = _validate_identifier(environment_id, "environment_id")
        payload = self._list_payload(environment_id, limit=20)
        tasks = payload["tasks"]
        assert isinstance(tasks, list)
        if tasks:
            raise CodexCloudCliError(
                "non-empty Codex Cloud task schema has not passed a contract test"
            )
        return ()

    def submit(self, request: SubmissionRequest) -> Never:
        raise CodexCloudCliWriteDisabledError(
            "Codex Cloud submit is disabled: the pinned CLI has no structured submit protocol"
        )

    def reconcile(self, external_task_id: str) -> RemoteRun | None:
        _validate_identifier(external_task_id, "external_task_id")
        raise CodexCloudCliUnsupportedReadError(
            "Codex Cloud status is not machine-readable in the pinned CLI"
        )

    def fetch_diff(self, external_task_id: str) -> DiffResult:
        _validate_identifier(external_task_id, "external_task_id")
        raise CodexCloudCliUnsupportedReadError(
            "Codex Cloud diff contract is not implemented"
        )

    def apply(self, external_task_id: str, worktree: Path) -> Never:
        raise CodexCloudCliWriteDisabledError("Codex Cloud apply is disabled")

    def _list_payload(self, environment_id: str, *, limit: int) -> dict[str, Any]:
        result = run_command(
            (
                self._codex_path,
                "cloud",
                "list",
                "--env",
                environment_id,
                "--limit",
                str(limit),
                "--json",
            ),
            timeout_seconds=self._timeout_seconds,
        )
        if (
            result.returncode != 0
            or result.timed_out
            or result.error is not None
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise CodexCloudCliError(_command_failure(result))
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CodexCloudCliError("codex cloud list returned malformed JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {"tasks", "cursor"}:
            raise CodexCloudCliError("codex cloud list returned unexpected JSON fields")
        if not isinstance(payload["tasks"], list):
            raise CodexCloudCliError("codex cloud list tasks must be an array")
        if payload["cursor"] is not None and not isinstance(payload["cursor"], str):
            raise CodexCloudCliError("codex cloud list cursor must be a string or null")
        return payload


def _validate_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{field} has unsupported characters")
    return value


def _command_failure(result: CommandResult) -> str:
    if result.timed_out:
        return "codex cloud list timed out"
    if result.error is not None:
        return "codex cloud list failed to start"
    if result.stdout_truncated or result.stderr_truncated:
        return "codex cloud list output was truncated"
    return f"codex cloud list failed with exit code {result.returncode}"
