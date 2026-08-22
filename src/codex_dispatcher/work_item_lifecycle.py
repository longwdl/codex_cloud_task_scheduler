"""Durable Control-side lifecycle evidence for completed Runner workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from codex_dispatcher.work_items import (
    validate_git_sha,
    validate_sha256,
    validate_work_item_id,
)


class WorkItemArchiveStatus(StrEnum):
    PREPARED = "prepared"
    AMBIGUOUS = "ambiguous"
    ARCHIVED = "archived"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class WorkItemArchive:
    work_item_id: str
    status: WorkItemArchiveStatus
    expected_head_sha: str
    eligible_at: str
    request_sha256: str
    response_json: str | None
    response_sha256: str | None
    reclaimed_bytes: int | None
    runner_archived_at: str | None
    error_code: str | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        validate_work_item_id(self.work_item_id)
        validate_git_sha(self.expected_head_sha, "expected_head_sha")
        validate_sha256(self.request_sha256, "request_sha256")
        if not isinstance(self.status, WorkItemArchiveStatus):
            raise ValueError("archive status is invalid")
        for value, field in (
            (self.eligible_at, "eligible_at"),
            (self.created_at, "created_at"),
            (self.updated_at, "updated_at"),
        ):
            _aware_datetime(value, field)
        if self.status is WorkItemArchiveStatus.ARCHIVED:
            if (
                not isinstance(self.response_json, str)
                or not self.response_json
                or self.response_sha256 is None
                or type(self.reclaimed_bytes) is not int
                or self.reclaimed_bytes < 0
                or self.runner_archived_at is None
                or self.error_code is not None
            ):
                raise ValueError("archived lifecycle record is incomplete")
            validate_sha256(self.response_sha256, "response_sha256")
            _aware_datetime(self.runner_archived_at, "runner_archived_at")
        elif any(
            value is not None
            for value in (
                self.response_json,
                self.response_sha256,
                self.reclaimed_bytes,
                self.runner_archived_at,
            )
        ):
            raise ValueError("unfinished lifecycle record contains archive receipt")
        if self.status is WorkItemArchiveStatus.BLOCKED:
            _error_code(self.error_code)
        elif self.error_code is not None:
            raise ValueError("non-blocked lifecycle record contains error_code")


def _aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be an aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be an aware ISO timestamp")
    return parsed


def validate_archive_error_code(value: object) -> str:
    return _error_code(value)


def _error_code(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("archive error_code must be bounded text")
    return value
