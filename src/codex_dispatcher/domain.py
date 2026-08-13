"""Core, environment-independent domain types for dispatcher runs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Final
from uuid import uuid4


def utc_now_iso() -> str:
    """Return an RFC 3339 timestamp in UTC with an explicit ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class RunState(StrEnum):
    DISCOVERED = "discovered"
    CLAIMED = "claimed"
    BRANCH_PREPARED = "branch_prepared"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    RESULT_READY = "result_ready"
    APPLYING = "applying"
    VALIDATING = "validating"
    DELIVERING = "delivering"
    REVIEW = "review"
    NEEDS_INPUT = "needs_input"
    BLOCKED = "blocked"
    DISCARDED = "discarded"


TERMINAL_STATES: Final[frozenset[RunState]] = frozenset(
    {RunState.REVIEW, RunState.NEEDS_INPUT, RunState.BLOCKED, RunState.DISCARDED}
)

_NORMAL_TRANSITIONS: Final[dict[RunState, frozenset[RunState]]] = {
    RunState.DISCOVERED: frozenset({RunState.CLAIMED}),
    RunState.CLAIMED: frozenset({RunState.BRANCH_PREPARED}),
    RunState.BRANCH_PREPARED: frozenset({RunState.DISPATCHING}),
    RunState.DISPATCHING: frozenset({RunState.RUNNING}),
    RunState.RUNNING: frozenset({RunState.RESULT_READY}),
    RunState.RESULT_READY: frozenset({RunState.APPLYING}),
    RunState.APPLYING: frozenset({RunState.VALIDATING}),
    RunState.VALIDATING: frozenset({RunState.DELIVERING}),
    RunState.DELIVERING: frozenset({RunState.REVIEW}),
}


class InvalidStateTransition(ValueError):
    """Raised when a run is asked to make a transition outside its state machine."""


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_STATES


def is_active(state: RunState) -> bool:
    return not is_terminal(state)


def allowed_transitions(state: RunState) -> frozenset[RunState]:
    if is_terminal(state):
        return frozenset()
    return _NORMAL_TRANSITIONS.get(state, frozenset()) | (TERMINAL_STATES - {RunState.REVIEW})


@dataclass(frozen=True, slots=True)
class Run:
    """Recoverable state of one attempted issue dispatch."""

    run_id: str
    repository: str
    issue_number: int
    attempt_no: int
    state: RunState
    prompt_sha256: str
    base_branch: str
    cloud_environment_id: str
    created_at: str
    updated_at: str
    base_sha: str | None = None
    task_branch: str | None = None
    cloud_task_id: str | None = None
    cloud_task_url: str | None = None
    cloud_diff_sha256: str | None = None
    head_sha: str | None = None
    pr_number: int | None = None
    retry_count: int = 0
    last_seen_at: str | None = None
    last_error_code: str | None = None
    last_error_redacted: str | None = None

    def __post_init__(self) -> None:
        nonempty = {
            "run_id": self.run_id,
            "repository": self.repository,
            "base_branch": self.base_branch,
            "cloud_environment_id": self.cloud_environment_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        for field, value in nonempty.items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field} must be a non-empty string")
        if self.repository.count("/") != 1 or any(
            not component for component in self.repository.split("/")
        ):
            raise ValueError("repository must be in owner/repository form")
        if type(self.issue_number) is not int or self.issue_number <= 0:
            raise ValueError("issue_number must be a positive integer")
        if type(self.attempt_no) is not int or self.attempt_no <= 0:
            raise ValueError("attempt_no must be a positive integer")
        if type(self.retry_count) is not int or self.retry_count < 0:
            raise ValueError("retry_count must be a non-negative integer")
        if re.fullmatch(r"[0-9a-f]{64}", self.prompt_sha256) is None:
            raise ValueError("prompt_sha256 must be a lowercase SHA-256 digest")
        if self.cloud_diff_sha256 is not None and re.fullmatch(
            r"[0-9a-f]{64}", self.cloud_diff_sha256
        ) is None:
            raise ValueError("cloud_diff_sha256 must be a lowercase SHA-256 digest")
        if self.pr_number is not None and (
            type(self.pr_number) is not int or self.pr_number <= 0
        ):
            raise ValueError("pr_number must be a positive integer or None")

    @classmethod
    def new(
        cls,
        *,
        repository: str,
        issue_number: int,
        prompt_sha256: str,
        base_branch: str,
        cloud_environment_id: str,
        attempt_no: int = 1,
        run_id: str | None = None,
    ) -> "Run":
        now = utc_now_iso()
        return cls(
            run_id=run_id or str(uuid4()),
            repository=repository,
            issue_number=issue_number,
            attempt_no=attempt_no,
            state=RunState.DISCOVERED,
            prompt_sha256=prompt_sha256,
            base_branch=base_branch,
            cloud_environment_id=cloud_environment_id,
            created_at=now,
            updated_at=now,
        )

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.state)

    @property
    def is_active(self) -> bool:
        return is_active(self.state)

    def transition_to(self, next_state: RunState, *, at: str | None = None) -> "Run":
        if next_state not in allowed_transitions(self.state):
            raise InvalidStateTransition(
                f"cannot transition run {self.run_id} from {self.state.value} to {next_state.value}"
            )
        return replace(self, state=next_state, updated_at=at or utc_now_iso())
