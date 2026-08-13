"""Fail-closed, read-only GitHub CLI tracker adapter.

The adapter deliberately exposes no GitHub mutations.  It translates the
small, fixed JSON shapes requested from ``gh`` into tracker DTOs and rejects
unexpected provider data rather than attempting to infer a safe meaning.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Never

from codex_dispatcher.command_runner import CommandResult, run_command
from codex_dispatcher.trackers.base import (
    DraftPullRequestRequest,
    PullRequest,
    TaskState,
    TrackerTask,
)


class GitHubCliTrackerError(RuntimeError):
    """Raised when a GitHub CLI read cannot be safely interpreted."""


class GitHubCliReadOnlyError(GitHubCliTrackerError):
    """Raised before an attempt to use a write operation on this adapter."""


class GitHubCliUnsupportedReadError(GitHubCliTrackerError):
    """Raised for tracker reads not implemented by the Phase 2 adapter."""


_STATUS_LABELS = {f"agent:{state.value}": state for state in TaskState}
_ISSUE_FIELDS = "number,title,body,labels,createdAt,state"
_REPOSITORY_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})"
)


class GitHubCliTracker:
    """Read open GitHub issues using a pinned absolute ``gh`` executable path."""

    def __init__(
        self,
        *,
        gh_path: str | Path,
        token: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        candidate = str(gh_path)
        if not candidate or not Path(candidate).is_absolute():
            raise ValueError("gh_path must be a non-empty absolute path")
        if token is not None and (
            not isinstance(token, str) or not token or "\x00" in token
        ):
            raise ValueError("token must be a non-empty string without NUL or None")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._gh_path = candidate
        self._token = token
        self._timeout_seconds = timeout_seconds

    def list_ready_tasks(self, repository: str) -> tuple[TrackerTask, ...]:
        """Return open issues currently labelled ``agent:ready``.

        Each issue is independently audited through its timeline.  Missing or
        malformed audit data produces ``ready_approved_by=None`` so the
        scheduler's maintainer allowlist rejects it.
        """
        repository = _validate_repository(repository)
        issues = self._json_command(
            (
                self._gh_path,
                "issue",
                "list",
                "--repo",
                repository,
                "--state",
                "open",
                "--label",
                "agent:ready",
                "--limit",
                "1000",
                "--json",
                _ISSUE_FIELDS,
            )
        )
        if not isinstance(issues, list):
            raise GitHubCliTrackerError("gh issue list JSON must be an array")
        tasks: list[TrackerTask] = []
        for index, issue in enumerate(issues):
            task = _parse_issue(issue, repository, f"issues[{index}]")
            if task.is_open and task.state is TaskState.READY:
                tasks.append(
                    TrackerTask(
                        task.repository, task.task_id, task.issue_number, task.title, task.body,
                        task.state, task.labels, task.created_at,
                        self._last_ready_label_actor(repository, task.issue_number), task.is_open,
                        task.has_unresolved_dependencies, task.branch_name,
                    )
                )
        return tuple(tasks)

    def get_task(self, repository: str, task_id: str) -> TrackerTask | None:
        """Read one issue by its numeric GitHub issue identifier."""
        repository = _validate_repository(repository)
        issue_number = _validate_issue_id(task_id)
        issue = self._json_command(
            (
                self._gh_path, "issue", "view", str(issue_number), "--repo", repository,
                "--json", _ISSUE_FIELDS,
            )
        )
        task = _parse_issue(issue, repository, "issue")
        return TrackerTask(
            task.repository, task.task_id, task.issue_number, task.title, task.body, task.state,
            task.labels, task.created_at,
            self._last_ready_label_actor(repository, task.issue_number), task.is_open,
            task.has_unresolved_dependencies, task.branch_name,
        )

    def find_pr_by_branch(self, repository: str, branch_name: str) -> PullRequest | None:
        raise GitHubCliUnsupportedReadError(
            "find_pr_by_branch is not implemented by read-only adapter"
        )

    def claim(self, repository: str, task_id: str, claimant: str) -> Never:
        self._raise_read_only()

    def set_state(self, repository: str, task_id: str, state: TaskState) -> Never:
        self._raise_read_only()

    def upsert_run_comment(self, repository: str, task_id: str, marker: str, body: str) -> Never:
        self._raise_read_only()

    def create_draft_pr(self, request: DraftPullRequestRequest) -> Never:
        self._raise_read_only()

    def _raise_read_only(self) -> Never:
        raise GitHubCliReadOnlyError("GitHub CLI tracker is read-only")

    def _last_ready_label_actor(self, repository: str, issue_number: int) -> str | None:
        events = self._json_command(
            (
                self._gh_path, "api", "--method", "GET", "--paginate", "--slurp",
                "-H", "Accept: application/vnd.github+json",
                f"/repos/{repository}/issues/{issue_number}/timeline?per_page=100",
            )
        )
        if not isinstance(events, list):
            return None
        latest: tuple[datetime, str] | None = None
        for page in events:
            if not isinstance(page, list):
                return None
            for event in page:
                if not isinstance(event, dict):
                    return None
                if event.get("event") != "labeled":
                    continue
                label = event.get("label")
                if not isinstance(label, dict) or label.get("name") != "agent:ready":
                    continue
                actor = event.get("actor")
                created_at = event.get("created_at")
                if not isinstance(actor, dict):
                    return None
                login = actor.get("login")
                if not isinstance(login, str) or not login or not isinstance(created_at, str):
                    return None
                try:
                    occurred_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                except ValueError:
                    return None
                if occurred_at.tzinfo is None:
                    return None
                if latest is None or occurred_at > latest[0]:
                    latest = (occurred_at, login)
        return None if latest is None else latest[1]

    def _json_command(self, argv: tuple[str, ...]) -> Any:
        command_env = {"GH_PROMPT_DISABLED": "1"}
        secrets: tuple[str, ...] = ()
        if self._token is not None:
            command_env["GH_TOKEN"] = self._token
            secrets = (self._token,)
        result = run_command(
            argv,
            timeout_seconds=self._timeout_seconds,
            env=command_env,
            secrets=secrets,
        )
        if (
            result.returncode != 0
            or result.timed_out
            or result.error is not None
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise GitHubCliTrackerError(_command_failure(result))
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubCliTrackerError("gh returned malformed JSON") from exc


def _command_failure(result: CommandResult) -> str:
    if result.timed_out:
        return "gh command timed out"
    if result.error is not None:
        return "gh command failed to start"
    if result.stdout_truncated or result.stderr_truncated:
        return "gh command output was truncated"
    return f"gh command failed with exit code {result.returncode}"


def _validate_repository(repository: str) -> str:
    if (
        not isinstance(repository, str)
        or _REPOSITORY_RE.fullmatch(repository) is None
        or any(not part or part in {".", ".."} for part in repository.split("/"))
    ):
        raise ValueError("repository must be in owner/repository form")
    return repository


def _validate_issue_id(task_id: str) -> int:
    if not isinstance(task_id, str) or not task_id.isdecimal() or task_id.startswith("0"):
        raise ValueError("task_id must be a positive canonical issue number")
    value = int(task_id)
    if value <= 0:
        raise ValueError("task_id must be a positive canonical issue number")
    return value


def _parse_issue(value: Any, repository: str, path: str) -> TrackerTask:
    if not isinstance(value, dict):
        raise GitHubCliTrackerError(f"{path} must be an object")
    expected = {"number", "title", "body", "labels", "createdAt", "state"}
    if set(value) != expected:
        raise GitHubCliTrackerError(f"{path} has unexpected JSON fields")
    number = value["number"]
    title = value["title"]
    body = value["body"]
    created_at = value["createdAt"]
    state = value["state"]
    if type(number) is not int or number <= 0:
        raise GitHubCliTrackerError(f"{path}.number must be a positive integer")
    if not all(isinstance(item, str) for item in (title, body, created_at)):
        raise GitHubCliTrackerError(f"{path} has invalid string field")
    if not title or not created_at or state not in {"OPEN", "CLOSED"}:
        raise GitHubCliTrackerError(f"{path} has invalid issue state")
    labels = _parse_labels(value["labels"], f"{path}.labels")
    status_labels = [label for label in labels if label.startswith("agent:")]
    if len(status_labels) != 1 or status_labels[0] not in _STATUS_LABELS:
        raise GitHubCliTrackerError(f"{path} has invalid agent state label")
    return TrackerTask(
        repository=repository,
        task_id=str(number),
        issue_number=number,
        title=title,
        body=body,
        state=_STATUS_LABELS[status_labels[0]],
        labels=labels,
        created_at=created_at,
        ready_approved_by=None,
        is_open=state == "OPEN",
    )


def _parse_labels(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise GitHubCliTrackerError(f"{path} must be an array")
    labels: list[str] = []
    for index, label in enumerate(value):
        if not isinstance(label, dict) or set(label) != {"id", "name", "description", "color"}:
            raise GitHubCliTrackerError(f"{path}[{index}] has unexpected label fields")
        name = label["name"]
        if (
            not isinstance(label["id"], str)
            or not label["id"]
            or not isinstance(name, str)
            or not name
            or not isinstance(label["description"], (str, type(None)))
            or not isinstance(label["color"], str)
            or not label["color"]
        ):
            raise GitHubCliTrackerError(f"{path}[{index}] has invalid label fields")
        labels.append(name)
    if len(set(labels)) != len(labels):
        raise GitHubCliTrackerError(f"{path} must not contain duplicate labels")
    return tuple(labels)
