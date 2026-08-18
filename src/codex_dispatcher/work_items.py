"""Stable work-item and turn identities for persistent Codex CLI sessions."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Final
from uuid import UUID, uuid4

from codex_dispatcher.domain import InvalidStateTransition, utc_now_iso


_REPOSITORY_COMPONENT_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_WORK_ITEM_ID_RE = re.compile(r"wi_[0-9a-f]{24}")
_TURN_ID_RE = re.compile(r"turn_[0-9a-f]{32}")
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40,64}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


class WorkItemState(StrEnum):
    DISCOVERED = "discovered"
    PREPARING = "preparing"
    READY = "ready"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    REVIEW = "review"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    PAUSED = "paused"


class TurnState(StrEnum):
    PLANNED = "planned"
    STARTING = "starting"
    RUNNING = "running"
    RECONCILING = "reconciling"
    CHECKPOINTING = "checkpointing"
    PUBLISHED = "published"
    FINISHED = "finished"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"


ACTIVE_TURN_STATES: Final[frozenset[TurnState]] = frozenset(
    {
        TurnState.PLANNED,
        TurnState.STARTING,
        TurnState.RUNNING,
        TurnState.RECONCILING,
        TurnState.CHECKPOINTING,
        TurnState.PUBLISHED,
    }
)

_WORK_ITEM_TRANSITIONS: Final[dict[WorkItemState, frozenset[WorkItemState]]] = {
    WorkItemState.DISCOVERED: frozenset(
        {WorkItemState.PREPARING, WorkItemState.BLOCKED, WorkItemState.PAUSED}
    ),
    WorkItemState.PREPARING: frozenset(
        {WorkItemState.READY, WorkItemState.BLOCKED, WorkItemState.PAUSED}
    ),
    WorkItemState.READY: frozenset(
        {WorkItemState.RUNNING, WorkItemState.BLOCKED, WorkItemState.PAUSED}
    ),
    WorkItemState.RUNNING: frozenset(
        {
            WorkItemState.WAITING_INPUT,
            WorkItemState.REVIEW,
            WorkItemState.BLOCKED,
            WorkItemState.PAUSED,
        }
    ),
    WorkItemState.WAITING_INPUT: frozenset(
        {WorkItemState.READY, WorkItemState.BLOCKED, WorkItemState.PAUSED}
    ),
    WorkItemState.REVIEW: frozenset(
        {
            WorkItemState.READY,
            WorkItemState.COMPLETED,
            WorkItemState.BLOCKED,
            WorkItemState.PAUSED,
        }
    ),
    WorkItemState.BLOCKED: frozenset({WorkItemState.READY, WorkItemState.PAUSED}),
    WorkItemState.PAUSED: frozenset({WorkItemState.READY, WorkItemState.BLOCKED}),
    WorkItemState.COMPLETED: frozenset(),
}

_TURN_TRANSITIONS: Final[dict[TurnState, frozenset[TurnState]]] = {
    TurnState.PLANNED: frozenset({TurnState.STARTING, TurnState.BLOCKED}),
    TurnState.STARTING: frozenset(
        {
            TurnState.RUNNING,
            TurnState.RECONCILING,
            TurnState.CHECKPOINTING,
            TurnState.FINISHED,
            TurnState.NEEDS_INPUT,
            TurnState.FAILED,
            TurnState.BLOCKED,
            TurnState.INTERRUPTED,
        }
    ),
    TurnState.RUNNING: frozenset(
        {
            TurnState.RECONCILING,
            TurnState.CHECKPOINTING,
            TurnState.FINISHED,
            TurnState.NEEDS_INPUT,
            TurnState.FAILED,
            TurnState.BLOCKED,
            TurnState.INTERRUPTED,
        }
    ),
    TurnState.RECONCILING: frozenset(
        {
            TurnState.RUNNING,
            TurnState.CHECKPOINTING,
            TurnState.FINISHED,
            TurnState.NEEDS_INPUT,
            TurnState.FAILED,
            TurnState.BLOCKED,
            TurnState.INTERRUPTED,
        }
    ),
    TurnState.CHECKPOINTING: frozenset(
        {TurnState.PUBLISHED, TurnState.FAILED, TurnState.BLOCKED, TurnState.INTERRUPTED}
    ),
    TurnState.PUBLISHED: frozenset(
        {TurnState.FINISHED, TurnState.NEEDS_INPUT, TurnState.FAILED, TurnState.BLOCKED}
    ),
    TurnState.FINISHED: frozenset(),
    TurnState.NEEDS_INPUT: frozenset(),
    TurnState.FAILED: frozenset(),
    TurnState.BLOCKED: frozenset(),
    TurnState.INTERRUPTED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class WorkItemIdentity:
    work_item_id: str
    task_branch: str
    runner_directory: str


def stable_work_item_identity(
    *,
    repository: str,
    issue_number: int,
    issue_node_id: str,
    runner_root: str = "/srv/codex-runner/work-items",
) -> WorkItemIdentity:
    """Derive stable identifiers from immutable Issue identity, never from an attempt."""
    repository = validate_repository(repository)
    _positive_int(issue_number, "issue_number")
    issue_node_id = _bounded_text(issue_node_id, "issue_node_id", maximum=256)
    root = _absolute_posix_path(runner_root, "runner_root")
    digest = _identity_digest(repository, issue_node_id)
    repository_key = repository.replace("/", "__")
    return WorkItemIdentity(
        work_item_id=f"wi_{digest[:24]}",
        task_branch=f"codex/issue-{issue_number}-{digest[:12]}",
        runner_directory=str(root / repository_key / f"issue-{issue_number}"),
    )


@dataclass(frozen=True, slots=True)
class WorkItem:
    work_item_id: str
    repository: str
    issue_number: int
    issue_node_id: str
    state: WorkItemState
    base_branch: str
    task_branch: str
    runner_directory: str
    base_sha: str
    created_at: str
    updated_at: str
    codex_session_id: str | None = None
    slack_channel_id: str | None = None
    slack_thread_ts: str | None = None
    pr_number: int | None = None
    last_published_sha: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, WorkItemState):
            raise ValueError("state must be a WorkItemState")
        validate_work_item_id(self.work_item_id)
        validate_repository(self.repository)
        _positive_int(self.issue_number, "issue_number")
        _bounded_text(self.issue_node_id, "issue_node_id", maximum=256)
        validate_branch(self.base_branch)
        validate_branch(self.task_branch)
        _absolute_posix_path(self.runner_directory, "runner_directory")
        validate_git_sha(self.base_sha, "base_sha")
        _bounded_text(self.created_at, "created_at", maximum=64)
        _bounded_text(self.updated_at, "updated_at", maximum=64)
        digest = _identity_digest(self.repository, self.issue_node_id)
        if self.work_item_id != f"wi_{digest[:24]}":
            raise ValueError("work_item_id does not match the immutable Issue identity")
        if self.task_branch != f"codex/issue-{self.issue_number}-{digest[:12]}":
            raise ValueError("task_branch must match the immutable Issue identity")
        if self.task_branch == self.base_branch:
            raise ValueError("task_branch must not equal base_branch")
        expected_tail = (
            self.repository.replace("/", "__"),
            f"issue-{self.issue_number}",
        )
        if PurePosixPath(self.runner_directory).parts[-2:] != expected_tail:
            raise ValueError("runner_directory must match the repository and Issue identity")
        if self.codex_session_id is not None:
            validate_session_id(self.codex_session_id)
        if (self.slack_channel_id is None) != (self.slack_thread_ts is None):
            raise ValueError("Slack channel and thread must be bound together")
        for field, value in (
            ("slack_channel_id", self.slack_channel_id),
            ("slack_thread_ts", self.slack_thread_ts),
        ):
            if value is not None:
                _bounded_text(value, field, maximum=128)
        if self.pr_number is not None:
            _positive_int(self.pr_number, "pr_number")
        if self.last_published_sha is not None:
            validate_git_sha(self.last_published_sha, "last_published_sha")

    @classmethod
    def new(
        cls,
        *,
        repository: str,
        issue_number: int,
        issue_node_id: str,
        base_branch: str,
        base_sha: str,
        runner_root: str = "/srv/codex-runner/work-items",
        at: str | None = None,
    ) -> "WorkItem":
        identity = stable_work_item_identity(
            repository=repository,
            issue_number=issue_number,
            issue_node_id=issue_node_id,
            runner_root=runner_root,
        )
        now = at or utc_now_iso()
        return cls(
            work_item_id=identity.work_item_id,
            repository=repository,
            issue_number=issue_number,
            issue_node_id=issue_node_id,
            state=WorkItemState.DISCOVERED,
            base_branch=base_branch,
            task_branch=identity.task_branch,
            runner_directory=identity.runner_directory,
            base_sha=base_sha,
            created_at=now,
            updated_at=now,
        )

    def transition_to(self, state: WorkItemState, *, at: str | None = None) -> "WorkItem":
        if state not in _WORK_ITEM_TRANSITIONS[self.state]:
            raise InvalidStateTransition(
                f"cannot transition work item {self.work_item_id} "
                f"from {self.state.value} to {state.value}"
            )
        return replace(self, state=state, updated_at=at or utc_now_iso())

    def bind_session(self, session_id: str, *, at: str | None = None) -> "WorkItem":
        session_id = validate_session_id(session_id)
        if self.codex_session_id is not None and self.codex_session_id != session_id:
            raise ValueError("work item is already bound to a different Codex session")
        if self.codex_session_id == session_id:
            return self
        return replace(self, codex_session_id=session_id, updated_at=at or utc_now_iso())

    def bind_slack_thread(
        self, channel_id: str, thread_ts: str, *, at: str | None = None
    ) -> "WorkItem":
        channel_id = _bounded_text(channel_id, "channel_id", maximum=128)
        thread_ts = _bounded_text(thread_ts, "thread_ts", maximum=128)
        current = (self.slack_channel_id, self.slack_thread_ts)
        requested = (channel_id, thread_ts)
        if current != (None, None) and current != requested:
            raise ValueError("work item is already bound to a different Slack thread")
        if current == requested:
            return self
        return replace(
            self,
            slack_channel_id=channel_id,
            slack_thread_ts=thread_ts,
            updated_at=at or utc_now_iso(),
        )


@dataclass(frozen=True, slots=True)
class Turn:
    turn_id: str
    work_item_id: str
    turn_number: int
    state: TurnState
    issue_revision: str
    prompt_sha256: str
    input_head_sha: str
    created_at: str
    updated_at: str
    output_sha256: str | None = None
    output_head_sha: str | None = None
    result_status: str | None = None
    result_summary: str | None = None
    error_code: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, TurnState):
            raise ValueError("state must be a TurnState")
        validate_turn_id(self.turn_id)
        validate_work_item_id(self.work_item_id)
        _positive_int(self.turn_number, "turn_number")
        _bounded_text(self.issue_revision, "issue_revision", maximum=256)
        validate_sha256(self.prompt_sha256, "prompt_sha256")
        validate_git_sha(self.input_head_sha, "input_head_sha")
        _bounded_text(self.created_at, "created_at", maximum=64)
        _bounded_text(self.updated_at, "updated_at", maximum=64)
        if self.output_sha256 is not None:
            validate_sha256(self.output_sha256, "output_sha256")
        if self.output_head_sha is not None:
            validate_git_sha(self.output_head_sha, "output_head_sha")
        if self.result_status is not None and self.result_status not in {
            "completed",
            "needs_input",
            "blocked",
        }:
            raise ValueError("result_status is unsupported")
        result_fields = (
            self.output_sha256,
            self.output_head_sha,
            self.result_status,
            self.result_summary,
        )
        if any(value is None for value in result_fields) and any(
            value is not None for value in result_fields
        ):
            raise ValueError("Turn result fields must be recorded together")
        if self.result_summary is not None:
            _bounded_text(
                self.result_summary, "result_summary", maximum=8_000, allow_newlines=True
            )
        if self.error_code is not None:
            _bounded_text(self.error_code, "error_code", maximum=128)
        for field, value in (("started_at", self.started_at), ("finished_at", self.finished_at)):
            if value is not None:
                _bounded_text(value, field, maximum=64)

    @classmethod
    def new(
        cls,
        *,
        work_item_id: str,
        turn_number: int,
        issue_revision: str,
        prompt_sha256: str,
        input_head_sha: str,
        turn_id: str | None = None,
        at: str | None = None,
    ) -> "Turn":
        now = at or utc_now_iso()
        return cls(
            turn_id=turn_id or f"turn_{uuid4().hex}",
            work_item_id=work_item_id,
            turn_number=turn_number,
            state=TurnState.PLANNED,
            issue_revision=issue_revision,
            prompt_sha256=prompt_sha256,
            input_head_sha=input_head_sha,
            created_at=now,
            updated_at=now,
        )

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_TURN_STATES

    def transition_to(self, state: TurnState, *, at: str | None = None) -> "Turn":
        if state not in _TURN_TRANSITIONS[self.state]:
            raise InvalidStateTransition(
                f"cannot transition turn {self.turn_id} "
                f"from {self.state.value} to {state.value}"
            )
        now = at or utc_now_iso()
        started_at = self.started_at
        finished_at = self.finished_at
        if state is TurnState.STARTING and started_at is None:
            started_at = now
        if state not in ACTIVE_TURN_STATES:
            finished_at = now
        return replace(
            self,
            state=state,
            started_at=started_at,
            finished_at=finished_at,
            updated_at=now,
        )


def validate_repository(value: str) -> str:
    if not isinstance(value, str) or value.count("/") != 1:
        raise ValueError("repository must be in owner/repository form")
    if any(_REPOSITORY_COMPONENT_RE.fullmatch(part) is None for part in value.split("/")):
        raise ValueError("repository must be in owner/repository form")
    return value


def validate_work_item_id(value: str) -> str:
    if not isinstance(value, str) or _WORK_ITEM_ID_RE.fullmatch(value) is None:
        raise ValueError("work_item_id must be a stable work-item identifier")
    return value


def validate_turn_id(value: str) -> str:
    if not isinstance(value, str) or _TURN_ID_RE.fullmatch(value) is None:
        raise ValueError("turn_id must be a stable turn identifier")
    return value


def validate_session_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("session_id must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("session_id must be a canonical UUID") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ValueError("session_id must be a canonical UUID")
    return value


def validate_git_sha(value: str, field: str = "git_sha") -> str:
    if not isinstance(value, str) or _GIT_SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase Git object ID")
    return value


def validate_sha256(value: str, field: str = "sha256") -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def validate_branch(value: str) -> str:
    if (
        not isinstance(value, str)
        or _BRANCH_RE.fullmatch(value) is None
        or value.startswith(("/", "."))
        or value.endswith(("/", ".", ".lock"))
        or ".." in value
        or "//" in value
        or "@{" in value
    ):
        raise ValueError("task_branch must be a safe Git branch name")
    return value


def _positive_int(value: int, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _bounded_text(
    value: str, field: str, *, maximum: int, allow_newlines: bool = False
) -> str:
    allowed_controls = {9, 10, 13} if allow_newlines else set()
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(
            (ord(character) < 32 and ord(character) not in allowed_controls)
            or ord(character) == 127
            for character in value
        )
    ):
        raise ValueError(f"{field} must be non-empty bounded text")
    return value


def _absolute_posix_path(value: str, field: str) -> PurePosixPath:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise ValueError(f"{field} must be an absolute normalized POSIX path")
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise ValueError(f"{field} must be an absolute normalized POSIX path")
    return path


def _identity_digest(repository: str, issue_node_id: str) -> str:
    material = f"codex-work-item-v1\0{repository.lower()}\0{issue_node_id}".encode()
    return sha256(material).hexdigest()
