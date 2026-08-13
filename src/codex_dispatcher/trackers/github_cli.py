"""Fail-closed GitHub CLI tracker adapter.

The adapter implements the smallest Issue state and comment mutations needed
for controlled contract tests. It translates fixed JSON shapes requested from
``gh`` and rejects unexpected provider data rather than inferring a safe meaning.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Never

from codex_dispatcher.command_runner import CommandResult, run_command
from codex_dispatcher.trackers.base import (
    ClaimResult,
    DraftPullRequestRequest,
    PullRequest,
    TaskState,
    TrackerTask,
)


class GitHubCliTrackerError(RuntimeError):
    """Raised when a GitHub CLI read cannot be safely interpreted."""


class GitHubCliReadOnlyError(GitHubCliTrackerError):
    """Raised when a write operation is deliberately unavailable."""


class GitHubCliUnsupportedReadError(GitHubCliTrackerError):
    """Raised for tracker reads not implemented by the current adapter."""


_RUN_COMMENT_PREFIX = "<!-- codex-dispatcher:"


_STATUS_LABELS = {f"agent:{state.value}": state for state in TaskState}
_ISSUE_FIELDS = "number,title,body,labels,createdAt,state"
_REPOSITORY_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})"
)


class GitHubCliTracker:
    """Read and safely update Issues using an absolute ``gh`` executable path."""

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

    def claim(
        self,
        repository: str,
        task_id: str,
        claimant: str,
        *,
        approved_by: tuple[str, ...] | None = None,
    ) -> ClaimResult:
        """Atomically-as-possible claim a ready issue after re-reading it."""
        repository = _validate_repository(repository)
        issue_number = _validate_issue_id(task_id)
        _validate_text(claimant, "claimant")
        task = self.get_task(repository, task_id)
        if task is None or not task.is_open or task.state is not TaskState.READY:
            return ClaimResult(False, task, "task is not open and ready")
        if approved_by is None or task.ready_approved_by not in approved_by:
            return ClaimResult(False, task, "ready approval is not trusted")
        self._replace_state_label(repository, issue_number, task.labels, TaskState.DISPATCHING)
        claimed = self.get_task(repository, task_id)
        if claimed is None or claimed.state is not TaskState.DISPATCHING:
            raise GitHubCliTrackerError("claim verification failed")
        return ClaimResult(True, claimed)

    def set_state(
        self, repository: str, task_id: str, state: TaskState
    ) -> TrackerTask:
        """Replace the single current agent state and verify the result."""
        repository = _validate_repository(repository)
        issue_number = _validate_issue_id(task_id)
        if not isinstance(state, TaskState):
            raise TypeError("state must be a TaskState")
        task = self.get_task(repository, task_id)
        if task is None:
            raise GitHubCliTrackerError("task does not exist")
        self._replace_state_label(repository, issue_number, task.labels, state)
        updated = self.get_task(repository, task_id)
        if updated is None or updated.state is not state:
            raise GitHubCliTrackerError("state update verification failed")
        return updated

    def upsert_run_comment(
        self, repository: str, task_id: str, marker: str, body: str
    ) -> None:
        """Create or edit the one dispatcher comment identified by *marker*."""
        repository = _validate_repository(repository)
        issue_number = _validate_issue_id(task_id)
        marker = _validate_comment_marker(marker)
        body = _validate_text(body, "body")
        signature = f"{_RUN_COMMENT_PREFIX}{marker} -->"
        rendered = f"{signature}\n{body}"
        comments = self._json_command(
            (
                self._gh_path,
                "api",
                "--method",
                "GET",
                "--paginate",
                "--slurp",
                "-H",
                "Accept: application/vnd.github+json",
                f"/repos/{repository}/issues/{issue_number}/comments?per_page=100",
            )
        )
        comment_ids = _matching_comment_ids(comments, signature)
        if len(comment_ids) > 1:
            raise GitHubCliTrackerError("multiple dispatcher comments match marker")
        if comment_ids:
            endpoint = f"/repos/{repository}/issues/comments/{comment_ids[0]}"
            method = "PATCH"
        else:
            endpoint = f"/repos/{repository}/issues/{issue_number}/comments"
            method = "POST"
        self._json_command(
            (
                self._gh_path,
                "api",
                "--method",
                method,
                "-H",
                "Accept: application/vnd.github+json",
                endpoint,
                "-f",
                f"body={rendered}",
            )
        )

    def create_draft_pr(self, request: DraftPullRequestRequest) -> Never:
        raise GitHubCliReadOnlyError("draft pull request creation is not implemented")

    def _replace_state_label(
        self,
        repository: str,
        issue_number: int,
        labels: tuple[str, ...],
        state: TaskState,
    ) -> None:
        status_labels = tuple(label for label in labels if label.startswith("agent:"))
        if len(status_labels) != 1:
            raise GitHubCliTrackerError("task must have exactly one agent state label")
        if len(set(labels)) != len(labels):
            raise GitHubCliTrackerError("task labels must not contain duplicates")
        current = status_labels[0]
        requested = f"agent:{state.value}"
        if current == requested:
            return
        updated_labels = tuple(label for label in labels if label != current) + (requested,)
        label_arguments = tuple(
            argument
            for label in updated_labels
            for argument in ("-f", f"labels[]={label}")
        )
        self._json_command(
            (
                self._gh_path,
                "api",
                "--method",
                "PATCH",
                "-H",
                "Accept: application/vnd.github+json",
                f"/repos/{repository}/issues/{issue_number}",
                *label_arguments,
            )
        )

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


def _validate_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{field} must be a non-empty string without NUL")
    return value


def _validate_comment_marker(marker: str) -> str:
    marker = _validate_text(marker, "marker")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", marker) is None:
        raise ValueError("marker contains unsupported characters")
    return marker


def _matching_comment_ids(value: Any, signature: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise GitHubCliTrackerError("gh issue comments JSON must be an array of pages")
    matches: list[int] = []
    seen: set[int] = set()
    for page in value:
        if not isinstance(page, list):
            raise GitHubCliTrackerError("gh issue comments page must be an array")
        for comment in page:
            if not isinstance(comment, dict) or set(comment) < {"id", "body"}:
                raise GitHubCliTrackerError("gh issue comment has invalid fields")
            comment_id = comment["id"]
            body = comment["body"]
            if type(comment_id) is not int or comment_id <= 0 or not isinstance(body, str):
                raise GitHubCliTrackerError("gh issue comment has invalid values")
            if comment_id in seen:
                raise GitHubCliTrackerError("gh issue comments contain duplicate ids")
            seen.add(comment_id)
            if body.startswith(signature):
                matches.append(comment_id)
    return tuple(matches)


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
