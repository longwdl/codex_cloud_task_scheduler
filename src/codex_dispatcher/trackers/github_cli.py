"""Fail-closed GitHub CLI tracker adapter.

The adapter implements the smallest Issue state and comment mutations needed
for controlled contract tests. It translates fixed JSON shapes requested from
``gh`` and rejects unexpected provider data rather than inferring a safe meaning.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import quote

from codex_dispatcher.command_runner import CommandResult, run_command
from codex_dispatcher.github_api_metrics import (
    GitHubApiMetrics,
    GitHubApiMetricsCollector,
)
from codex_dispatcher.trackers.base import (
    ClaimResult,
    DraftPullRequestRequest,
    PullRequest,
    PullRequestState,
    TaskState,
    TrackerComment,
    TrackerTask,
)
from codex_dispatcher.work_items import validate_branch, validate_git_sha


class GitHubCliTrackerError(RuntimeError):
    """Raised when a GitHub CLI read cannot be safely interpreted."""


class GitHubCliReadOnlyError(GitHubCliTrackerError):
    """Raised when a write operation is deliberately unavailable."""


class GitHubCliUnsupportedReadError(GitHubCliTrackerError):
    """Raised for tracker reads not implemented by the current adapter."""


_RUN_COMMENT_PREFIX = "<!-- codex-dispatcher:"


_STATUS_LABELS = {f"agent:{state.value}": state for state in TaskState}
_ISSUE_FIELDS = "id,number,title,body,labels,createdAt,updatedAt,state"
_PR_FIELDS = (
    "number,url,headRefName,headRefOid,baseRefName,title,isDraft,state,isCrossRepository"
)
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
        metrics_collector: GitHubApiMetricsCollector | None = None,
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
        self._metrics = metrics_collector or GitHubApiMetricsCollector()

    def collect_api_metrics(self) -> GitHubApiMetrics:
        """Return bounded command counters plus a best-effort provider budget snapshot."""
        rate_values: dict[str, int] | None = None
        rate_error: str | None = None
        try:
            payload = self._json_command(
                (
                    self._gh_path,
                    "api",
                    "--method",
                    "GET",
                    "-H",
                    "Accept: application/vnd.github+json",
                    "/rate_limit",
                )
            )
            rate_values = _parse_rate_limit(payload)
        except (GitHubCliTrackerError, ValueError):
            rate_error = "github_rate_limit_unavailable"
        return self._metrics.snapshot(
            core_remaining=None if rate_values is None else rate_values["core_remaining"],
            core_limit=None if rate_values is None else rate_values["core_limit"],
            core_reset_epoch=(
                None if rate_values is None else rate_values["core_reset_epoch"]
            ),
            graphql_remaining=(
                None if rate_values is None else rate_values["graphql_remaining"]
            ),
            graphql_limit=None if rate_values is None else rate_values["graphql_limit"],
            graphql_reset_epoch=(
                None if rate_values is None else rate_values["graphql_reset_epoch"]
            ),
            rate_limit_error=rate_error,
        )

    def list_ready_tasks(self, repository: str) -> tuple[TrackerTask, ...]:
        """Return open issues currently labelled ``agent:ready``.

        Each issue is independently audited through its timeline.  Missing or
        malformed audit data produces ``ready_approved_by=None`` so the
        scheduler's maintainer allowlist rejects it.
        """
        return self.list_open_tasks(repository, TaskState.READY)

    def list_open_tasks(
        self, repository: str, state: TaskState
    ) -> tuple[TrackerTask, ...]:
        """Return open Issues having exactly the requested dispatcher state."""
        repository = _validate_repository(repository)
        if not isinstance(state, TaskState):
            raise TypeError("state must be a TaskState")
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
                f"agent:{state.value}",
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
            if task.is_open and task.state is state:
                tasks.append(
                    self._with_state_approvers(repository, task)
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
        return self._with_state_approvers(repository, task)

    def list_comments(
        self, repository: str, task_id: str
    ) -> tuple[TrackerComment, ...]:
        """Read all Issue comments as bounded immutable snapshots."""
        repository = _validate_repository(repository)
        issue_number = _validate_issue_id(task_id)
        pages = self._json_command(
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
        return _parse_comments(pages)

    def find_pr_by_branch(self, repository: str, branch_name: str) -> PullRequest | None:
        """Find the only same-repository PR ever created for a task branch."""
        repository = _validate_repository(repository)
        branch_name = validate_branch(branch_name)
        pull_requests = self._json_command(
            (
                self._gh_path,
                "pr",
                "list",
                "--repo",
                repository,
                "--state",
                "all",
                "--head",
                branch_name,
                "--limit",
                "100",
                "--json",
                _PR_FIELDS,
            )
        )
        if not isinstance(pull_requests, list):
            raise GitHubCliTrackerError("gh pull request list JSON must be an array")
        parsed = tuple(
            _parse_pull_request(value, repository, branch_name, f"pulls[{index}]")
            for index, value in enumerate(pull_requests)
        )
        if len(parsed) > 1:
            raise GitHubCliTrackerError("multiple pull requests use the task branch")
        return parsed[0] if parsed else None

    def get_branch_head(self, repository: str, branch_name: str) -> str | None:
        """Return the exact task-branch head without treating absence as an error."""
        repository = _validate_repository(repository)
        branch_name = validate_branch(branch_name)
        encoded = quote(branch_name, safe="")
        payload = self._json_command(
            (
                self._gh_path,
                "api",
                "--method",
                "GET",
                "-H",
                "Accept: application/vnd.github+json",
                f"/repos/{repository}/git/matching-refs/heads/{encoded}",
            )
        )
        if not isinstance(payload, list):
            raise GitHubCliTrackerError("GitHub matching refs response must be an array")
        exact_ref = f"refs/heads/{branch_name}"
        matches = [item for item in payload if isinstance(item, dict) and item.get("ref") == exact_ref]
        if len(matches) > 1:
            raise GitHubCliTrackerError("multiple exact GitHub branch refs returned")
        if not matches:
            return None
        target = matches[0].get("object")
        if not isinstance(target, dict) or target.get("type") != "commit":
            raise GitHubCliTrackerError("GitHub branch target is not a commit")
        try:
            return validate_git_sha(target.get("sha"), "branch head SHA")
        except (TypeError, ValueError) as exc:
            raise GitHubCliTrackerError("GitHub branch head SHA is invalid") from exc

    def delete_branch(
        self,
        repository: str,
        branch_name: str,
        expected_head_sha: str,
    ) -> None:
        """Delete only a branch whose immediately re-read head is exact."""
        repository = _validate_repository(repository)
        branch_name = validate_branch(branch_name)
        expected_head_sha = validate_git_sha(expected_head_sha, "expected_head_sha")
        current = self.get_branch_head(repository, branch_name)
        if current is None:
            return
        if current != expected_head_sha:
            raise GitHubCliTrackerError("GitHub branch head changed before deletion")
        encoded = quote(branch_name, safe="")
        output = self._text_command(
            (
                self._gh_path,
                "api",
                "--method",
                "DELETE",
                "-H",
                "Accept: application/vnd.github+json",
                f"/repos/{repository}/git/refs/heads/{encoded}",
            )
        )
        if output.strip():
            raise GitHubCliTrackerError("GitHub branch deletion returned unexpected output")

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

    def create_draft_pr(self, request: DraftPullRequestRequest) -> PullRequest:
        """Create one Draft PR using only the fixed request fields and explicit head."""
        if not isinstance(request, DraftPullRequestRequest):
            raise TypeError("request must be a DraftPullRequestRequest")
        repository = _validate_repository(request.repository)
        branch_name = validate_branch(request.branch_name)
        base_branch = validate_branch(request.base_branch)
        title = _validate_bounded_text(request.title, "title", maximum=256)
        body = _validate_bounded_text(request.body, "body", maximum=16_000)
        if branch_name in {base_branch, "main", "master"}:
            raise ValueError("Draft PR head must be a non-protected task branch")
        output = self._text_command(
            (
                self._gh_path,
                "pr",
                "create",
                "--repo",
                repository,
                "--head",
                branch_name,
                "--base",
                base_branch,
                "--title",
                title,
                "--body",
                body,
                "--draft",
                "--no-maintainer-edit",
            )
        )
        match = re.fullmatch(
            rf"https://github\.com/{re.escape(repository)}/pull/([1-9][0-9]*)",
            output.strip(),
        )
        if match is None:
            raise GitHubCliTrackerError("gh returned an invalid Draft PR URL")
        return PullRequest(
            number=int(match.group(1)),
            url=output.strip(),
            branch_name=branch_name,
            title=title,
            is_draft=True,
            base_branch=base_branch,
            state=PullRequestState.OPEN,
        )

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

    def _with_state_approvers(
        self, repository: str, task: TrackerTask
    ) -> TrackerTask:
        ready_approval = self._last_state_label_approval(
            repository, task.issue_number, TaskState.READY
        )
        ready_approver = ready_approval[0] if ready_approval is not None else None
        state_approval = ready_approval if task.state is TaskState.READY else None
        if task.state is TaskState.DISCARD:
            state_approval = self._last_state_label_approval(
                repository, task.issue_number, TaskState.DISCARD
            )
            current = self._read_issue_snapshot(repository, task.issue_number)
            if current != task:
                raise GitHubCliTrackerError(
                    "discard issue snapshot changed during authorization audit"
                )
        return _with_state_approvers(task, ready_approver, state_approval)

    def _read_issue_snapshot(
        self, repository: str, issue_number: int
    ) -> TrackerTask:
        issue = self._json_command(
            (
                self._gh_path,
                "issue",
                "view",
                str(issue_number),
                "--repo",
                repository,
                "--json",
                _ISSUE_FIELDS,
            )
        )
        return _parse_issue(issue, repository, "issue authorization recheck")

    def _last_state_label_approval(
        self,
        repository: str,
        issue_number: int,
        state: TaskState,
    ) -> tuple[str, str | None, str] | None:
        if state not in {TaskState.READY, TaskState.DISCARD}:
            raise ValueError("only trusted operator state labels can be audited")
        label_name = f"agent:{state.value}"
        events = self._json_lines_command(
            (
                self._gh_path, "api", "--method", "GET", "--paginate",
                "-H", "Accept: application/vnd.github+json",
                f"/repos/{repository}/issues/{issue_number}/timeline?per_page=100",
                "--jq",
                (
                    '.[] | select(.event == "labeled" and '
                    f'.label.name == "{label_name}") | '
                    "{id,event,created_at,actor:{login:.actor.login},"
                    "label:{name:.label.name}}"
                ),
            )
        )
        latest: tuple[datetime, int, str, str] | None = None
        for event in events:
            if not isinstance(event, dict):
                return None
            if event.get("event") != "labeled":
                return None
            label = event.get("label")
            if not isinstance(label, dict) or label.get("name") != label_name:
                return None
            actor = event.get("actor")
            created_at = event.get("created_at")
            if not isinstance(actor, dict):
                return None
            login = actor.get("login")
            event_id = event.get("id")
            if not isinstance(login, str) or not login or not isinstance(created_at, str):
                return None
            try:
                occurred_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                return None
            if occurred_at.tzinfo is None:
                return None
            if state is TaskState.DISCARD:
                if type(event_id) is not int or event_id <= 0:
                    return None
                numeric_event_id = event_id
            else:
                numeric_event_id = event_id if type(event_id) is int and event_id > 0 else 0
            candidate = (occurred_at, numeric_event_id, login, created_at)
            if latest is not None and occurred_at == latest[0] and login != latest[2]:
                return None
            if latest is None or candidate[:2] > latest[:2]:
                latest = candidate
        if latest is None:
            return None
        return latest[2], (str(latest[1]) if latest[1] else None), latest[3]

    def _json_lines_command(self, argv: tuple[str, ...]) -> tuple[Any, ...]:
        result = self._command(argv)
        values: list[Any] = []
        for line in result.stdout.splitlines():
            if not line:
                raise GitHubCliTrackerError("gh returned malformed JSON Lines")
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise GitHubCliTrackerError("gh returned malformed JSON Lines") from exc
        return tuple(values)

    def _json_command(self, argv: tuple[str, ...]) -> Any:
        result = self._command(argv)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubCliTrackerError("gh returned malformed JSON") from exc

    def _text_command(self, argv: tuple[str, ...]) -> str:
        return self._command(argv).stdout

    def _command(self, argv: tuple[str, ...]) -> CommandResult:
        started = time.monotonic()
        with TemporaryDirectory(prefix="codex-dispatcher-gh-") as config_directory:
            command_env = {
                "GH_CONFIG_DIR": config_directory,
                "GH_PROMPT_DISABLED": "1",
            }
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
        failed = (
            result.returncode != 0
            or result.timed_out
            or result.error is not None
            or result.stdout_truncated
            or result.stderr_truncated
        )
        self._metrics.record_command(
            read=_is_read_command(argv),
            failed=failed,
            elapsed_milliseconds=max(0, int((time.monotonic() - started) * 1000)),
        )
        if failed:
            raise GitHubCliTrackerError(_command_failure(result))
        return result


def _is_read_command(argv: tuple[str, ...]) -> bool:
    if len(argv) < 3:
        return False
    if argv[1] in {"issue", "pr"}:
        return argv[2] in {"list", "view"}
    if argv[1] != "api":
        return False
    try:
        method = argv[argv.index("--method") + 1]
    except (ValueError, IndexError):
        method = "GET"
    return method == "GET"


def _parse_rate_limit(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError("GitHub rate limit response is invalid")
    resources = value.get("resources")
    if not isinstance(resources, dict):
        raise ValueError("GitHub rate limit resources are invalid")
    result: dict[str, int] = {}
    for name in ("core", "graphql"):
        resource = resources.get(name)
        if not isinstance(resource, dict):
            raise ValueError("GitHub rate limit resource is invalid")
        for field in ("remaining", "limit", "reset"):
            item = resource.get(field)
            if type(item) is not int or item < 0:
                raise ValueError("GitHub rate limit value is invalid")
            result[f"{name}_{field if field != 'reset' else 'reset_epoch'}"] = item
        if result[f"{name}_remaining"] > result[f"{name}_limit"]:
            raise ValueError("GitHub rate limit remaining exceeds limit")
    return result


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


def _validate_bounded_text(value: str, field: str, *, maximum: int) -> str:
    value = _validate_text(value, field)
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} exceeds its safe size boundary")
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


def _parse_pull_request(
    value: Any,
    repository: str,
    branch_name: str,
    path: str,
) -> PullRequest:
    if not isinstance(value, dict) or set(value) != {
        "number",
        "url",
        "headRefName",
        "headRefOid",
        "baseRefName",
        "title",
        "isDraft",
        "state",
        "isCrossRepository",
    }:
        raise GitHubCliTrackerError(f"{path} has unexpected JSON fields")
    number = value["number"]
    url = value["url"]
    head = value["headRefName"]
    head_sha = value["headRefOid"]
    base = value["baseRefName"]
    title = value["title"]
    is_draft = value["isDraft"]
    state = value["state"]
    cross_repository = value["isCrossRepository"]
    if (
        type(number) is not int
        or number <= 0
        or url != f"https://github.com/{repository}/pull/{number}"
        or head != branch_name
        or not isinstance(base, str)
        or not isinstance(title, str)
        or type(is_draft) is not bool
        or type(cross_repository) is not bool
        or cross_repository
    ):
        raise GitHubCliTrackerError(f"{path} has invalid values")
    try:
        base = validate_branch(base)
        head_sha = validate_git_sha(head_sha, "pull_request_head_sha")
        parsed_state = PullRequestState(str(state).lower())
    except (TypeError, ValueError) as exc:
        raise GitHubCliTrackerError(f"{path} has invalid values") from exc
    return PullRequest(
        number,
        url,
        head,
        title,
        is_draft,
        base,
        parsed_state,
        cross_repository,
        head_sha,
    )


def _parse_issue(value: Any, repository: str, path: str) -> TrackerTask:
    if not isinstance(value, dict):
        raise GitHubCliTrackerError(f"{path} must be an object")
    expected = {
        "id", "number", "title", "body", "labels", "createdAt", "updatedAt", "state"
    }
    if set(value) != expected:
        raise GitHubCliTrackerError(f"{path} has unexpected JSON fields")
    number = value["number"]
    issue_node_id = value["id"]
    title = value["title"]
    body = value["body"]
    created_at = value["createdAt"]
    updated_at = value["updatedAt"]
    state = value["state"]
    if type(number) is not int or number <= 0:
        raise GitHubCliTrackerError(f"{path}.number must be a positive integer")
    if not all(
        isinstance(item, str)
        for item in (issue_node_id, title, body, created_at, updated_at)
    ):
        raise GitHubCliTrackerError(f"{path} has invalid string field")
    if (
        not issue_node_id
        or not title
        or not created_at
        or not updated_at
        or state not in {"OPEN", "CLOSED"}
    ):
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
        issue_node_id=issue_node_id,
        updated_at=updated_at,
    )


def _with_state_approvers(
    task: TrackerTask,
    ready_approver: str | None,
    state_approval: tuple[str, str | None, str] | None,
) -> TrackerTask:
    return TrackerTask(
        repository=task.repository,
        task_id=task.task_id,
        issue_number=task.issue_number,
        title=task.title,
        body=task.body,
        state=task.state,
        labels=task.labels,
        created_at=task.created_at,
        ready_approved_by=ready_approver,
        is_open=task.is_open,
        has_unresolved_dependencies=task.has_unresolved_dependencies,
        branch_name=task.branch_name,
        issue_node_id=task.issue_node_id,
        updated_at=task.updated_at,
        state_approved_by=(state_approval[0] if state_approval is not None else None),
        state_approval_event_id=(
            state_approval[1] if state_approval is not None else None
        ),
        state_approved_at=(state_approval[2] if state_approval is not None else None),
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


def _parse_comments(value: Any) -> tuple[TrackerComment, ...]:
    if not isinstance(value, list):
        raise GitHubCliTrackerError("gh issue comments JSON must be an array of pages")
    comments: list[TrackerComment] = []
    seen_ids: set[str] = set()
    for page_index, page in enumerate(value):
        if not isinstance(page, list):
            raise GitHubCliTrackerError("gh issue comments page must be an array")
        for index, raw in enumerate(page):
            path = f"comments[{page_index}][{index}]"
            if not isinstance(raw, dict):
                raise GitHubCliTrackerError(f"{path} must be an object")
            required = {"node_id", "user", "body", "created_at", "updated_at"}
            if not required <= set(raw):
                raise GitHubCliTrackerError(f"{path} is missing required fields")
            comment_id = raw["node_id"]
            user = raw["user"]
            body = raw["body"]
            created_at = raw["created_at"]
            updated_at = raw["updated_at"]
            if (
                not isinstance(comment_id, str)
                or not comment_id
                or len(comment_id) > 256
                or not isinstance(user, dict)
                or not isinstance(user.get("login"), str)
                or not user["login"]
                or not isinstance(body, str)
                or len(body) > 65_536
                or "\x00" in body
            ):
                raise GitHubCliTrackerError(f"{path} has invalid values")
            _parse_timestamp(created_at, f"{path}.created_at")
            _parse_timestamp(updated_at, f"{path}.updated_at")
            if comment_id in seen_ids:
                raise GitHubCliTrackerError("gh issue comments contain duplicate node IDs")
            seen_ids.add(comment_id)
            comments.append(
                TrackerComment(
                    comment_id,
                    user["login"],
                    body,
                    created_at,
                    updated_at,
                )
            )
    return tuple(comments)


def _parse_timestamp(value: Any, path: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise GitHubCliTrackerError(f"{path} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitHubCliTrackerError(f"{path} must be a timezone-aware timestamp") from exc
    if parsed.tzinfo is None:
        raise GitHubCliTrackerError(f"{path} must be a timezone-aware timestamp")
    return parsed
