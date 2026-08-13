"""Fail-closed Codex Cloud CLI contract adapter.

The pinned CLI exposes structured JSON only for ``cloud list``. Submission
places the prompt in argv and has no JSON response, so every write remains
disabled. Structured task listing is implemented for pre-submit snapshots and
recovery, while unknown fields and statuses fail closed.
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
    RunStatus,
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
    """Read Cloud task snapshots without submitting or applying tasks."""

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
        self._parse_tasks(payload, environment_id)
        return PreflightResult(True, "environment is visible; submit remains disabled")

    def list_runs(self, environment_id: str) -> tuple[RemoteRun, ...]:
        environment_id = _validate_identifier(environment_id, "environment_id")
        payload = self._list_payload(environment_id, limit=20)
        return self._parse_tasks(payload, environment_id)

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

    def _parse_tasks(
        self, payload: dict[str, Any], requested_environment_id: str
    ) -> tuple[RemoteRun, ...]:
        tasks = payload["tasks"]
        assert isinstance(tasks, list)
        parsed: list[RemoteRun] = []
        seen_ids: set[str] = set()
        for task in tasks:
            if not isinstance(task, dict):
                raise CodexCloudCliError("codex cloud list task must be an object")
            required = {
                "id",
                "url",
                "title",
                "status",
                "updated_at",
                "environment_id",
                "environment_label",
                "summary",
                "is_review",
                "attempt_total",
            }
            if set(task) != required:
                raise CodexCloudCliError("codex cloud list task has unexpected JSON fields")
            task_id = _validate_identifier(_task_string(task, "id"), "external_task_id")
            environment_id = _task_string(task, "environment_id")
            if environment_id != requested_environment_id:
                raise CodexCloudCliError("codex cloud list returned a different environment")
            if task_id in seen_ids:
                raise CodexCloudCliError("codex cloud list returned duplicate task IDs")
            seen_ids.add(task_id)
            for field in ("url", "title", "updated_at", "environment_label"):
                _task_string(task, field)
            status = _task_string(task, "status")
            if task["summary"] is not None and not isinstance(task["summary"], str):
                raise CodexCloudCliError("codex cloud list task summary has an invalid type")
            if type(task["is_review"]) is not bool:
                raise CodexCloudCliError("codex cloud list task is_review has an invalid type")
            if type(task["attempt_total"]) is not int or task["attempt_total"] < 1:
                raise CodexCloudCliError("codex cloud list task attempt_total is invalid")
            parsed.append(
                RemoteRun(
                    task_id,
                    None,
                    environment_id,
                    _normalize_status(status),
                    "",
                    task["url"],
                    task["summary"],
                )
            )
        return tuple(parsed)


def _validate_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{field} has unsupported characters")
    return value


def _task_string(task: dict[str, Any], field: str) -> str:
    value = task[field]
    if not isinstance(value, str) or not value:
        raise CodexCloudCliError(f"codex cloud list task {field} must be non-empty")
    return value


def _normalize_status(value: str) -> RunStatus:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "queued": RunStatus.QUEUED,
        "pending": RunStatus.QUEUED,
        "running": RunStatus.RUNNING,
        "in_progress": RunStatus.RUNNING,
        "succeeded": RunStatus.SUCCEEDED,
        "completed": RunStatus.SUCCEEDED,
        "failed": RunStatus.FAILED,
        "cancelled": RunStatus.CANCELLED,
        "canceled": RunStatus.CANCELLED,
    }
    return mapping.get(normalized, RunStatus.UNKNOWN)


def _command_failure(result: CommandResult) -> str:
    if result.timed_out:
        return "codex cloud list timed out"
    if result.error is not None:
        return "codex cloud list failed to start"
    if result.stdout_truncated or result.stderr_truncated:
        return "codex cloud list output was truncated"
    return f"codex cloud list failed with exit code {result.returncode}"
