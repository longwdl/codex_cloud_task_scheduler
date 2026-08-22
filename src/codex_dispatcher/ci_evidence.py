"""Provider-independent, exact-HEAD CI evidence contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from codex_dispatcher.work_items import (
    validate_branch,
    validate_git_sha,
    validate_repository,
)


class CiEvidenceError(RuntimeError):
    """Raised when external CI state cannot be attributed without ambiguity."""


class RequiredCheckStatus(StrEnum):
    NOT_OBSERVED = "not_observed"
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


_PENDING_RUN_STATUSES = frozenset(
    {"in_progress", "pending", "queued", "requested", "waiting"}
)
_COMPLETED_CONCLUSIONS = frozenset(
    {
        "action_required",
        "cancelled",
        "failure",
        "neutral",
        "skipped",
        "stale",
        "startup_failure",
        "success",
        "timed_out",
    }
)


@dataclass(frozen=True, slots=True)
class ActionsRunEvidence:
    """One bounded GitHub Actions workflow run for an exact task HEAD."""

    name: str
    workflow_id: int
    run_id: int
    run_attempt: int
    repository: str
    head_repository: str
    head_branch: str
    head_sha: str
    event: str
    status: str
    conclusion: str | None
    created_at: str
    updated_at: str
    html_url: str

    def __post_init__(self) -> None:
        _bounded_text(self.name, "workflow name", maximum=256)
        if type(self.workflow_id) is not int or self.workflow_id <= 0:
            raise ValueError("workflow_id must be a positive integer")
        if type(self.run_id) is not int or self.run_id <= 0:
            raise ValueError("run_id must be a positive integer")
        if type(self.run_attempt) is not int or self.run_attempt <= 0:
            raise ValueError("run_attempt must be a positive integer")
        validate_repository(self.repository)
        validate_repository(self.head_repository)
        validate_branch(self.head_branch)
        validate_git_sha(self.head_sha, "head_sha")
        _bounded_text(self.event, "Actions event", maximum=128)
        if self.status == "completed":
            if self.conclusion not in _COMPLETED_CONCLUSIONS:
                raise ValueError("completed Actions run has an invalid conclusion")
        elif self.status in _PENDING_RUN_STATUSES:
            if self.conclusion is not None:
                raise ValueError("incomplete Actions run must not have a conclusion")
        else:
            raise ValueError("Actions run has an unsupported status")
        created_at = _aware_iso8601(self.created_at, "created_at")
        updated_at = _aware_iso8601(self.updated_at, "updated_at")
        if updated_at < created_at:
            raise ValueError("Actions run updated_at precedes created_at")
        expected_url = f"https://github.com/{self.repository}/actions/runs/{self.run_id}"
        if self.html_url != expected_url:
            raise ValueError("Actions run URL conflicts with its repository or run id")

    @property
    def check_status(self) -> RequiredCheckStatus:
        if self.status != "completed":
            return RequiredCheckStatus.PENDING
        if self.conclusion == "success":
            return RequiredCheckStatus.PASSED
        return RequiredCheckStatus.FAILED

    @property
    def evidence_ref(self) -> str:
        return f"github-actions-run:{self.run_id}:attempt:{self.run_attempt}"


@dataclass(frozen=True, slots=True)
class RequiredCheckEvidence:
    """The unambiguous Actions run, if any, selected for one configured check."""

    name: str
    status: RequiredCheckStatus
    run: ActionsRunEvidence | None

    def __post_init__(self) -> None:
        _bounded_text(self.name, "required check name", maximum=256)
        if not isinstance(self.status, RequiredCheckStatus):
            raise TypeError("status must be a RequiredCheckStatus")
        if self.status is RequiredCheckStatus.NOT_OBSERVED:
            if self.run is not None:
                raise ValueError("not_observed check evidence cannot contain a run")
        elif self.run is None or self.run.name != self.name:
            raise ValueError("observed check evidence must contain its exact named run")
        elif self.run.check_status is not self.status:
            raise ValueError("required check status conflicts with its Actions run")


@dataclass(frozen=True, slots=True)
class ActionsEvidenceSnapshot:
    """One stable-ref GitHub Actions observation for all configured required checks."""

    repository: str
    task_branch: str
    head_sha: str
    remote_ref_sha: str
    observed_at: str
    required_checks: tuple[RequiredCheckEvidence, ...]

    def __post_init__(self) -> None:
        validate_repository(self.repository)
        validate_branch(self.task_branch)
        validate_git_sha(self.head_sha, "head_sha")
        validate_git_sha(self.remote_ref_sha, "remote_ref_sha")
        if self.remote_ref_sha != self.head_sha:
            raise ValueError("remote task ref does not equal the requested evidence HEAD")
        observed_at = _aware_iso8601(self.observed_at, "observed_at")
        if (
            not isinstance(self.required_checks, tuple)
            or not self.required_checks
            or len(self.required_checks) > 100
            or any(
                not isinstance(item, RequiredCheckEvidence)
                for item in self.required_checks
            )
        ):
            raise TypeError("required_checks must be a non-empty evidence tuple")
        names = tuple(item.name for item in self.required_checks)
        if len(set(names)) != len(names):
            raise ValueError("Actions evidence contains duplicate required check names")
        for item in self.required_checks:
            if item.run is not None and (
                item.run.repository != self.repository
                or item.run.head_repository != self.repository
                or item.run.head_branch != self.task_branch
                or item.run.head_sha != self.head_sha
            ):
                raise ValueError("Actions run does not belong to the exact evidence target")
            if item.run is not None and _aware_iso8601(
                item.run.updated_at, "run updated_at"
            ) > observed_at:
                raise ValueError("Actions run update occurs after its evidence observation")

    def check(self, name: str) -> RequiredCheckEvidence | None:
        return next((item for item in self.required_checks if item.name == name), None)


class CiEvidenceImporter(Protocol):
    """Read trusted provider facts for one exact, already-published task HEAD."""

    def import_for_head(
        self,
        *,
        repository: str,
        task_branch: str,
        head_sha: str,
        required_checks: tuple[str, ...],
        observed_at: str,
    ) -> ActionsEvidenceSnapshot: ...


def _bounded_text(value: object, field: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field} must be non-empty bounded text")
    return value


def _aware_iso8601(value: object, field: str) -> datetime:
    value = _bounded_text(value, field, maximum=64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed
