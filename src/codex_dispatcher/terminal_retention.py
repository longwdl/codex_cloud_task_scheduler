"""Durable, exact-identity retention records for terminal GitHub branches."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256

from codex_dispatcher.work_items import (
    validate_branch,
    validate_git_sha,
    validate_repository,
    validate_sha256,
    validate_work_item_id,
)


class TerminalBranchCleanupState(StrEnum):
    PREPARED = "prepared"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class TerminalBranchCleanupOutcome(StrEnum):
    DELETED = "deleted"
    ALREADY_ABSENT = "already_absent"
    RECONCILED_ABSENT = "reconciled_absent"


@dataclass(frozen=True, slots=True)
class TerminalBranchCleanup:
    work_item_id: str
    repository: str
    branch_name: str
    expected_head_sha: str
    eligible_at: str
    request_sha256: str
    state: TerminalBranchCleanupState
    outcome: TerminalBranchCleanupOutcome | None
    error_code: str | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        validate_work_item_id(self.work_item_id)
        validate_repository(self.repository)
        validate_branch(self.branch_name)
        validate_git_sha(self.expected_head_sha, "expected_head_sha")
        validate_sha256(self.request_sha256, "request_sha256")
        for value in (self.eligible_at, self.created_at, self.updated_at):
            _aware_timestamp(value)
        if not isinstance(self.state, TerminalBranchCleanupState):
            raise TypeError("terminal branch cleanup state is invalid")
        if self.state is TerminalBranchCleanupState.COMPLETED:
            if not isinstance(self.outcome, TerminalBranchCleanupOutcome):
                raise ValueError("completed terminal branch cleanup requires an outcome")
            if self.error_code is not None:
                raise ValueError("completed terminal branch cleanup cannot have an error")
        elif self.state is TerminalBranchCleanupState.BLOCKED:
            if self.outcome is not None or not self.error_code:
                raise ValueError("blocked terminal branch cleanup requires only an error")
        elif self.outcome is not None or self.error_code is not None:
            raise ValueError("prepared terminal branch cleanup cannot have a result")


def terminal_branch_request_sha256(
    *, work_item_id: str, repository: str, branch_name: str, expected_head_sha: str
) -> str:
    validate_work_item_id(work_item_id)
    validate_repository(repository)
    validate_branch(branch_name)
    validate_git_sha(expected_head_sha, "expected_head_sha")
    payload = json.dumps(
        {
            "branch_name": branch_name,
            "expected_head_sha": expected_head_sha,
            "op": "delete_terminal_branch_v1",
            "repository": repository,
            "work_item_id": work_item_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _aware_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("terminal branch cleanup timestamp must be aware")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("terminal branch cleanup timestamp must be aware") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("terminal branch cleanup timestamp must be aware")
    return parsed
