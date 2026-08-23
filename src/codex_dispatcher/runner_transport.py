"""Strict responses and the environment-independent SSH Runner transport port."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Protocol

from codex_dispatcher.runner_protocol import (
    NEXT_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    AgentResult,
    RunnerOperation,
    RunnerProtocolError,
    RunnerRequest,
    agent_result_to_json,
    agent_result_to_mapping,
    parse_agent_result,
)
from codex_dispatcher.codex_jsonl import CodexTurnUsage
from codex_dispatcher.delegation_evidence import (
    DelegationEvidenceError,
    DelegationReceipt,
    delegation_receipt_to_mapping,
    parse_delegation_receipt,
)
from codex_dispatcher.work_items import (
    validate_branch,
    validate_git_sha,
    validate_repository,
    validate_session_id,
    validate_session_generation_id,
    validate_sha256,
    validate_turn_id,
    validate_work_item_id,
)


MAX_RESPONSE_BYTES = 512 * 1024
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024


class RunnerTransportError(RuntimeError):
    """Base class for failures at the Runner transport boundary."""


class RunnerTransportInterrupted(RunnerTransportError):
    """The caller cannot know whether the Runner accepted the request."""


class RunnerTransportRejected(RunnerTransportError):
    """The Runner definitively rejected the request without a usable response."""


@dataclass(frozen=True, slots=True)
class RunnerWireOutput:
    payload: bytes
    artifact: bytes | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.payload, bytes) or not self.payload:
            raise RunnerProtocolError("Runner response payload must be non-empty bytes")
        if len(self.payload) > MAX_RESPONSE_BYTES or b"\x00" in self.payload:
            raise RunnerProtocolError("Runner response payload exceeds its safe boundary")
        if self.artifact is not None and (
            not isinstance(self.artifact, bytes)
            or not self.artifact
            or len(self.artifact) > MAX_ARTIFACT_BYTES
        ):
            raise RunnerProtocolError("Runner artifact exceeds its safe boundary")


class RunnerTransport(Protocol):
    def invoke(
        self,
        request: RunnerRequest,
        *,
        stdin: bytes = b"",
        source_artifact: bytes | None = None,
    ) -> RunnerWireOutput: ...


RUNNER_CAPACITY_SCOPE_ID = "wi_" + "0" * 24


@dataclass(frozen=True, slots=True)
class RunnerCapacityReply:
    capacity_bytes: int
    available_bytes: int
    image_size_bytes: int
    host_reserve_bytes: int
    turn_admissible: bool
    provision_admissible: bool
    provision_shortfall_bytes: int
    operation: RunnerOperation = RunnerOperation.CAPACITY
    work_item_id: str = RUNNER_CAPACITY_SCOPE_ID
    version: int = NEXT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.operation is not RunnerOperation.CAPACITY:
            raise RunnerProtocolError("operation does not return capacity evidence")
        if self.version != NEXT_PROTOCOL_VERSION:
            raise RunnerProtocolError("unsupported Runner capacity response version")
        if self.work_item_id != RUNNER_CAPACITY_SCOPE_ID:
            raise RunnerProtocolError("Runner capacity scope is invalid")
        numeric = (
            self.capacity_bytes,
            self.available_bytes,
            self.image_size_bytes,
            self.host_reserve_bytes,
            self.provision_shortfall_bytes,
        )
        if any(type(value) is not int or value < 0 for value in numeric):
            raise RunnerProtocolError("Runner capacity values are invalid")
        if (
            self.capacity_bytes <= 0
            or self.available_bytes > self.capacity_bytes
            or self.image_size_bytes <= 0
            or self.host_reserve_bytes <= 0
            or type(self.turn_admissible) is not bool
            or type(self.provision_admissible) is not bool
        ):
            raise RunnerProtocolError("Runner capacity values are inconsistent")
        shortfall = max(
            0,
            self.image_size_bytes
            + self.host_reserve_bytes
            - self.available_bytes,
        )
        turn_admissible = (
            self.available_bytes >= self.host_reserve_bytes
            and self.available_bytes * 100 >= self.capacity_bytes * 15
        )
        if (
            self.provision_shortfall_bytes != shortfall
            or self.provision_admissible != (shortfall == 0)
            or self.turn_admissible != turn_admissible
        ):
            raise RunnerProtocolError("Runner capacity admission flags are inconsistent")

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": self.version,
            "op": self.operation.value,
            "work_item_id": self.work_item_id,
            "state": "ok",
            "capacity_bytes": self.capacity_bytes,
            "available_bytes": self.available_bytes,
            "image_size_bytes": self.image_size_bytes,
            "host_reserve_bytes": self.host_reserve_bytes,
            "turn_admissible": self.turn_admissible,
            "provision_admissible": self.provision_admissible,
            "provision_shortfall_bytes": self.provision_shortfall_bytes,
        }

    def to_json(self) -> str:
        return _dump(self.to_mapping())


def parse_runner_capacity_reply(value: str | bytes) -> RunnerCapacityReply:
    payload = _load(value)
    expected = {
        "version",
        "op",
        "work_item_id",
        "state",
        "capacity_bytes",
        "available_bytes",
        "image_size_bytes",
        "host_reserve_bytes",
        "turn_admissible",
        "provision_admissible",
        "provision_shortfall_bytes",
    }
    if set(payload) != expected or payload.get("state") != "ok":
        raise RunnerProtocolError("Runner capacity response fields are invalid")
    try:
        return RunnerCapacityReply(
            operation=RunnerOperation(payload["op"]),
            work_item_id=payload["work_item_id"],
            version=payload["version"],
            capacity_bytes=payload["capacity_bytes"],
            available_bytes=payload["available_bytes"],
            image_size_bytes=payload["image_size_bytes"],
            host_reserve_bytes=payload["host_reserve_bytes"],
            turn_admissible=payload["turn_admissible"],
            provision_admissible=payload["provision_admissible"],
            provision_shortfall_bytes=payload["provision_shortfall_bytes"],
        )
    except (TypeError, ValueError) as exc:
        raise RunnerProtocolError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class RunnerAck:
    operation: RunnerOperation
    work_item_id: str
    version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.operation not in {
            RunnerOperation.PREPARE,
            RunnerOperation.STOP,
            RunnerOperation.ARCHIVE,
        }:
            raise RunnerProtocolError("operation does not return an acknowledgement")
        if type(self.version) is not int or self.version != PROTOCOL_VERSION:
            raise RunnerProtocolError("unsupported Runner response version")
        validate_work_item_id(self.work_item_id)

    def to_json(self) -> str:
        return _dump(
            {
                "version": self.version,
                "op": self.operation.value,
                "work_item_id": self.work_item_id,
                "state": "ok",
            }
        )


class RunnerArchiveState(StrEnum):
    ACTIVE = "active"
    ARCHIVING = "archiving"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class RunnerArchiveReply:
    operation: RunnerOperation
    work_item_id: str
    expected_head_sha: str
    state: RunnerArchiveState
    archived_at: str | None = None
    reclaimed_bytes: int | None = None
    version: int = NEXT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.operation not in {
            RunnerOperation.ARCHIVE,
            RunnerOperation.ARCHIVE_STATUS,
        }:
            raise RunnerProtocolError("operation does not return an archive reply")
        if self.version != NEXT_PROTOCOL_VERSION:
            raise RunnerProtocolError("unsupported Runner archive response version")
        validate_work_item_id(self.work_item_id)
        validate_git_sha(self.expected_head_sha, "expected_head_sha")
        if not isinstance(self.state, RunnerArchiveState):
            raise RunnerProtocolError("Runner archive state is invalid")
        if self.state is RunnerArchiveState.ARCHIVED:
            if not isinstance(self.archived_at, str):
                raise RunnerProtocolError("archived Runner reply requires archived_at")
            try:
                parsed = datetime.fromisoformat(self.archived_at)
            except ValueError as exc:
                raise RunnerProtocolError("Runner archived_at is invalid") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise RunnerProtocolError("Runner archived_at must include a timezone")
            if (
                type(self.reclaimed_bytes) is not int
                or self.reclaimed_bytes < 0
            ):
                raise RunnerProtocolError(
                    "archived Runner reply requires non-negative reclaimed_bytes"
                )
        elif self.archived_at is not None or self.reclaimed_bytes is not None:
            raise RunnerProtocolError(
                "non-archived Runner reply cannot contain archive receipt fields"
            )
        if (
            self.operation is RunnerOperation.ARCHIVE
            and self.state is not RunnerArchiveState.ARCHIVED
        ):
            raise RunnerProtocolError("ARCHIVE must return a completed archive receipt")

    def to_json(self) -> str:
        payload: dict[str, object] = {
            "version": self.version,
            "op": self.operation.value,
            "work_item_id": self.work_item_id,
            "expected_head_sha": self.expected_head_sha,
            "state": self.state.value,
        }
        if self.archived_at is not None:
            payload["archived_at"] = self.archived_at
        if self.reclaimed_bytes is not None:
            payload["reclaimed_bytes"] = self.reclaimed_bytes
        return _dump(payload)


def parse_runner_archive_reply(value: str | bytes) -> RunnerArchiveReply:
    payload = _load(value)
    base = {
        "version",
        "op",
        "work_item_id",
        "expected_head_sha",
        "state",
    }
    try:
        operation = RunnerOperation(payload.get("op"))
        state = RunnerArchiveState(payload.get("state"))
    except (TypeError, ValueError) as exc:
        raise RunnerProtocolError("Runner archive response identity is invalid") from exc
    expected = (
        base | {"archived_at", "reclaimed_bytes"}
        if state is RunnerArchiveState.ARCHIVED
        else base
    )
    if set(payload) != expected:
        raise RunnerProtocolError("Runner archive response fields are invalid")
    try:
        return RunnerArchiveReply(
            operation=operation,
            work_item_id=payload["work_item_id"],
            expected_head_sha=payload["expected_head_sha"],
            state=state,
            archived_at=payload.get("archived_at"),
            reclaimed_bytes=payload.get("reclaimed_bytes"),
            version=payload["version"],
        )
    except (TypeError, ValueError) as exc:
        raise RunnerProtocolError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class RunnerAbsenceReply:
    """Runner-persisted proof that one exact WorkItem has no reclaimable state."""

    work_item_id: str
    repository: str
    issue_number: int
    task_branch: str
    expected_head_sha: str
    archive_request_sha256: str
    request_sha256: str
    observed_at: str
    operation: RunnerOperation = RunnerOperation.PROVE_ABSENCE
    version: int = NEXT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.operation is not RunnerOperation.PROVE_ABSENCE:
            raise RunnerProtocolError("operation does not return an absence reply")
        if self.version != NEXT_PROTOCOL_VERSION:
            raise RunnerProtocolError("unsupported Runner absence response version")
        validate_work_item_id(self.work_item_id)
        validate_repository(self.repository)
        if type(self.issue_number) is not int or self.issue_number <= 0:
            raise RunnerProtocolError("issue_number must be a positive integer")
        validate_branch(self.task_branch)
        validate_git_sha(self.expected_head_sha, "expected_head_sha")
        validate_sha256(self.archive_request_sha256, "archive_request_sha256")
        validate_sha256(self.request_sha256, "request_sha256")
        if not isinstance(self.observed_at, str):
            raise RunnerProtocolError("observed_at must be an aware timestamp")
        try:
            parsed = datetime.fromisoformat(self.observed_at)
        except ValueError as exc:
            raise RunnerProtocolError("observed_at is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise RunnerProtocolError("observed_at must include a timezone")

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": self.version,
            "op": self.operation.value,
            "work_item_id": self.work_item_id,
            "repository": self.repository,
            "issue_number": self.issue_number,
            "task_branch": self.task_branch,
            "expected_head_sha": self.expected_head_sha,
            "archive_request_sha256": self.archive_request_sha256,
            "request_sha256": self.request_sha256,
            "state": "absent",
            "observed_at": self.observed_at,
        }

    def to_json(self) -> str:
        return _dump(self.to_mapping())


def parse_runner_absence_reply(value: str | bytes) -> RunnerAbsenceReply:
    payload = _load(value)
    expected = {
        "version",
        "op",
        "work_item_id",
        "repository",
        "issue_number",
        "task_branch",
        "expected_head_sha",
        "archive_request_sha256",
        "request_sha256",
        "state",
        "observed_at",
    }
    if set(payload) != expected or payload.get("state") != "absent":
        raise RunnerProtocolError("Runner absence response fields are invalid")
    try:
        return RunnerAbsenceReply(
            operation=RunnerOperation(payload["op"]),
            work_item_id=payload["work_item_id"],
            repository=payload["repository"],
            issue_number=payload["issue_number"],
            task_branch=payload["task_branch"],
            expected_head_sha=payload["expected_head_sha"],
            archive_request_sha256=payload["archive_request_sha256"],
            request_sha256=payload["request_sha256"],
            observed_at=payload["observed_at"],
            version=payload["version"],
        )
    except (TypeError, ValueError) as exc:
        raise RunnerProtocolError(str(exc)) from exc


def parse_runner_ack(value: str | bytes) -> RunnerAck:
    payload = _load(value)
    if set(payload) != {"version", "op", "work_item_id", "state"}:
        raise RunnerProtocolError("Runner acknowledgement fields are invalid")
    if payload["state"] != "ok":
        raise RunnerProtocolError("Runner acknowledgement is not successful")
    try:
        return RunnerAck(
            operation=RunnerOperation(payload["op"]),
            work_item_id=payload["work_item_id"],
            version=payload["version"],
        )
    except (TypeError, ValueError) as exc:
        raise RunnerProtocolError(str(exc)) from exc


class RunnerTurnRemoteState(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RunnerInactiveContainerState(StrEnum):
    STOPPED = "stopped"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class RunnerTurnReply:
    operation: RunnerOperation
    work_item_id: str
    turn_id: str
    state: RunnerTurnRemoteState
    session_id: str | None = None
    head_sha: str | None = None
    output_sha256: str | None = None
    result: AgentResult | None = None
    error_code: str | None = None
    failure_head_sha: str | None = None
    worktree_clean: bool | None = None
    session_generation_id: str | None = None
    session_generation: int | None = None
    agent_policy_digest: str | None = None
    usage: CodexTurnUsage | None = None
    delegation_receipt: DelegationReceipt | None = None
    inactive_container_state: RunnerInactiveContainerState | None = None
    inactive_observed_at: str | None = None
    version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.operation not in {
            RunnerOperation.START,
            RunnerOperation.RESUME,
            RunnerOperation.STATUS,
            RunnerOperation.STOP,
        }:
            raise RunnerProtocolError("operation does not return a Turn reply")
        if type(self.version) is not int or self.version not in {
            PROTOCOL_VERSION,
            NEXT_PROTOCOL_VERSION,
        }:
            raise RunnerProtocolError("unsupported Runner response version")
        validate_work_item_id(self.work_item_id)
        validate_turn_id(self.turn_id)
        if not isinstance(self.state, RunnerTurnRemoteState):
            raise RunnerProtocolError("Runner Turn state is invalid")
        if self.session_id is not None:
            validate_session_id(self.session_id)
        if self.head_sha is not None:
            validate_git_sha(self.head_sha, "head_sha")
        if self.output_sha256 is not None:
            validate_sha256(self.output_sha256, "output_sha256")
        if self.error_code is not None and (
            not isinstance(self.error_code, str)
            or not self.error_code
            or len(self.error_code) > 128
            or any(ord(character) < 32 or ord(character) == 127 for character in self.error_code)
        ):
            raise RunnerProtocolError("Runner error_code is invalid")
        if self.failure_head_sha is not None:
            validate_git_sha(self.failure_head_sha, "failure_head_sha")
        if self.worktree_clean is not None and type(self.worktree_clean) is not bool:
            raise RunnerProtocolError("worktree_clean must be a bool or None")
        if (self.failure_head_sha is None) != (self.worktree_clean is None):
            raise RunnerProtocolError(
                "failed checkpoint evidence fields must be recorded together"
            )
        if (self.inactive_container_state is None) != (
            self.inactive_observed_at is None
        ):
            raise RunnerProtocolError(
                "inactive container evidence fields must be recorded together"
            )
        if self.inactive_container_state is not None:
            if not isinstance(
                self.inactive_container_state, RunnerInactiveContainerState
            ):
                raise RunnerProtocolError("inactive container state is invalid")
            if not isinstance(self.inactive_observed_at, str):
                raise RunnerProtocolError(
                    "inactive_observed_at must be an aware timestamp"
                )
            try:
                inactive_observed_at = datetime.fromisoformat(
                    self.inactive_observed_at
                )
            except ValueError as exc:
                raise RunnerProtocolError(
                    "inactive_observed_at is invalid"
                ) from exc
            if (
                inactive_observed_at.tzinfo is None
                or inactive_observed_at.utcoffset() is None
            ):
                raise RunnerProtocolError(
                    "inactive_observed_at must include a timezone"
                )
        if self.version == NEXT_PROTOCOL_VERSION:
            if self.session_generation_id is None:
                raise RunnerProtocolError("v2 Runner Turn requires session_generation_id")
            try:
                validate_session_generation_id(self.session_generation_id)
            except ValueError as exc:
                raise RunnerProtocolError(str(exc)) from exc
            if type(self.session_generation) is not int or self.session_generation <= 0:
                raise RunnerProtocolError("v2 Runner Turn requires a positive session_generation")
            if self.agent_policy_digest is None:
                raise RunnerProtocolError("v2 Runner Turn requires agent_policy_digest")
            try:
                validate_sha256(self.agent_policy_digest, "agent_policy_digest")
            except ValueError as exc:
                raise RunnerProtocolError(str(exc)) from exc
        elif any(
            value is not None
            for value in (
                self.session_generation_id,
                self.session_generation,
                self.agent_policy_digest,
                self.usage,
                self.delegation_receipt,
                self.failure_head_sha,
                self.worktree_clean,
                self.inactive_container_state,
                self.inactive_observed_at,
            )
        ):
            raise RunnerProtocolError("v1 Runner Turn cannot contain v2 fields")

        if self.operation is RunnerOperation.STOP and (
            self.version != NEXT_PROTOCOL_VERSION
            or self.state is not RunnerTurnRemoteState.FAILED
            or self.error_code != "turn_abandoned_inactive"
        ):
            raise RunnerProtocolError(
                "STOP only returns an exact v2 inactive abandonment receipt"
            )

        final_fields = (self.head_sha, self.output_sha256, self.result)
        if self.state is not RunnerTurnRemoteState.FAILED and (
            self.failure_head_sha is not None or self.worktree_clean is not None
        ):
            raise RunnerProtocolError(
                "failed checkpoint evidence requires a failed Runner Turn"
            )
        if self.state is RunnerTurnRemoteState.FINISHED:
            if self.session_id is None or any(value is None for value in final_fields):
                raise RunnerProtocolError("finished Runner Turn is missing result fields")
            if self.error_code is not None:
                raise RunnerProtocolError("finished Runner Turn cannot contain error_code")
            assert self.result is not None
            canonical = agent_result_to_json(self.result)
            if sha256(canonical.encode("utf-8")).hexdigest() != self.output_sha256:
                raise RunnerProtocolError("Runner result hash does not match its canonical JSON")
            if self.version == NEXT_PROTOCOL_VERSION:
                _validate_usage(self.usage)
                if self.delegation_receipt is not None and (
                    not isinstance(self.delegation_receipt, DelegationReceipt)
                    or self.delegation_receipt.root_thread_id != self.session_id
                ):
                    raise RunnerProtocolError(
                        "Runner delegation receipt conflicts with the Codex session"
                    )
            elif self.usage is not None:
                raise RunnerProtocolError("v1 finished Runner Turn cannot contain usage")
        elif self.state is RunnerTurnRemoteState.RUNNING:
            if self.session_id is None or any(value is not None for value in final_fields):
                raise RunnerProtocolError("running Runner Turn fields are invalid")
            if self.error_code is not None:
                raise RunnerProtocolError("running Runner Turn cannot contain error_code")
            if self.usage is not None:
                raise RunnerProtocolError("running Runner Turn cannot contain usage")
            if self.delegation_receipt is not None:
                raise RunnerProtocolError(
                    "running Runner Turn cannot contain delegation evidence"
                )
        else:
            if any(value is not None for value in final_fields) or self.error_code is None:
                raise RunnerProtocolError("failed or unknown Runner Turn fields are invalid")
            if self.usage is not None:
                raise RunnerProtocolError("failed or unknown Runner Turn cannot contain usage")
            if self.delegation_receipt is not None:
                raise RunnerProtocolError(
                    "failed or unknown Runner Turn cannot contain delegation evidence"
                )
            if self.state is RunnerTurnRemoteState.UNKNOWN and (
                self.failure_head_sha is not None
                or self.worktree_clean is not None
            ):
                raise RunnerProtocolError(
                    "unknown Runner Turn cannot contain failed checkpoint evidence"
                )
            if self.version == PROTOCOL_VERSION and (
                self.failure_head_sha is not None
                or self.worktree_clean is not None
            ):
                raise RunnerProtocolError(
                    "v1 Runner Turn cannot contain failed checkpoint evidence"
                )
            if self.error_code == "session_context_failure_clean" and (
                self.failure_head_sha is None or self.worktree_clean is not True
            ):
                raise RunnerProtocolError(
                    "clean context failure requires exact checkpoint evidence"
                )
        if self.inactive_container_state is not None:
            allowed_inactive_evidence = (
                self.version == NEXT_PROTOCOL_VERSION
                and (
                    (
                        self.state is RunnerTurnRemoteState.UNKNOWN
                        and self.error_code == "turn_container_inactive"
                    )
                    or (
                        self.state is RunnerTurnRemoteState.FAILED
                        and self.error_code == "turn_abandoned_inactive"
                    )
                )
            )
            if not allowed_inactive_evidence:
                raise RunnerProtocolError(
                    "inactive container evidence conflicts with the Turn outcome"
                )
        elif self.error_code in {
            "turn_container_inactive",
            "turn_abandoned_inactive",
        }:
            raise RunnerProtocolError(
                "inactive Turn outcome requires exact container evidence"
            )

    def to_json(self) -> str:
        payload: dict[str, object] = {
            "version": self.version,
            "op": self.operation.value,
            "work_item_id": self.work_item_id,
            "turn_id": self.turn_id,
            "state": self.state.value,
        }
        optional: tuple[tuple[str, object | None], ...] = (
            ("session_id", self.session_id),
            ("head_sha", self.head_sha),
            ("output_sha256", self.output_sha256),
            (
                "result",
                agent_result_to_mapping(self.result) if self.result is not None else None,
            ),
            ("error_code", self.error_code),
            ("failure_head_sha", self.failure_head_sha),
            ("worktree_clean", self.worktree_clean),
            ("session_generation_id", self.session_generation_id),
            ("session_generation", self.session_generation),
            ("agent_policy_digest", self.agent_policy_digest),
            ("usage", _usage_to_mapping(self.usage) if self.usage is not None else None),
            (
                "delegation_receipt",
                delegation_receipt_to_mapping(self.delegation_receipt)
                if self.delegation_receipt is not None
                else None,
            ),
            (
                "inactive_container_state",
                self.inactive_container_state.value
                if self.inactive_container_state is not None
                else None,
            ),
            ("inactive_observed_at", self.inactive_observed_at),
        )
        payload.update((name, value) for name, value in optional if value is not None)
        return _dump(payload)


def parse_runner_turn_reply(value: str | bytes) -> RunnerTurnReply:
    payload = _load(value)
    version = payload.get("version")
    if type(version) is not int or version not in {
        PROTOCOL_VERSION,
        NEXT_PROTOCOL_VERSION,
    }:
        raise RunnerProtocolError("unsupported Runner response version")
    base = {"version", "op", "work_item_id", "turn_id", "state"}
    try:
        state = RunnerTurnRemoteState(payload.get("state"))
    except (TypeError, ValueError) as exc:
        raise RunnerProtocolError("Runner Turn response state is unsupported") from exc
    expected = {
        RunnerTurnRemoteState.RUNNING: base | {"session_id"},
        RunnerTurnRemoteState.FINISHED: base
        | {"session_id", "head_sha", "output_sha256", "result"},
        RunnerTurnRemoteState.FAILED: base | {"error_code"},
        RunnerTurnRemoteState.UNKNOWN: base | {"error_code"},
    }[state]
    if state in {RunnerTurnRemoteState.FAILED, RunnerTurnRemoteState.UNKNOWN} and (
        "session_id" in payload
    ):
        expected = expected | {"session_id"}
    if state is RunnerTurnRemoteState.FAILED and (
        "failure_head_sha" in payload or "worktree_clean" in payload
    ):
        expected = expected | {"failure_head_sha", "worktree_clean"}
    if version == NEXT_PROTOCOL_VERSION:
        expected = expected | {
            "session_generation_id",
            "session_generation",
            "agent_policy_digest",
        }
        if state is RunnerTurnRemoteState.FINISHED:
            expected = expected | {"usage"}
            if "delegation_receipt" in payload:
                expected = expected | {"delegation_receipt"}
        if (
            "inactive_container_state" in payload
            or "inactive_observed_at" in payload
        ):
            expected = expected | {
                "inactive_container_state",
                "inactive_observed_at",
            }
    if set(payload) != expected:
        raise RunnerProtocolError("Runner Turn response fields are invalid")
    result: AgentResult | None = None
    if "result" in payload:
        result = parse_agent_result(_dump(payload["result"]))
    try:
        return RunnerTurnReply(
            operation=RunnerOperation(payload["op"]),
            work_item_id=payload["work_item_id"],
            turn_id=payload["turn_id"],
            state=state,
            session_id=payload.get("session_id"),
            head_sha=payload.get("head_sha"),
            output_sha256=payload.get("output_sha256"),
            result=result,
            error_code=payload.get("error_code"),
            failure_head_sha=payload.get("failure_head_sha"),
            worktree_clean=payload.get("worktree_clean"),
            session_generation_id=payload.get("session_generation_id"),
            session_generation=payload.get("session_generation"),
            agent_policy_digest=payload.get("agent_policy_digest"),
            usage=_usage_from_mapping(payload.get("usage")) if "usage" in payload else None,
            delegation_receipt=(
                parse_delegation_receipt(payload["delegation_receipt"])
                if "delegation_receipt" in payload
                else None
            ),
            inactive_container_state=(
                RunnerInactiveContainerState(payload["inactive_container_state"])
                if "inactive_container_state" in payload
                else None
            ),
            inactive_observed_at=payload.get("inactive_observed_at"),
            version=version,
        )
    except (DelegationEvidenceError, TypeError, ValueError) as exc:
        raise RunnerProtocolError(str(exc)) from exc


def _usage_to_mapping(usage: CodexTurnUsage) -> dict[str, int]:
    _validate_usage(usage)
    return {
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "cache_write_input_tokens": usage.cache_write_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
    }


def _usage_from_mapping(value: object) -> CodexTurnUsage:
    if not isinstance(value, dict):
        raise RunnerProtocolError("Runner Turn usage is invalid")
    expected = {
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    }
    if set(value) != expected:
        raise RunnerProtocolError("Runner Turn usage is invalid")
    try:
        usage = CodexTurnUsage(**value)
    except TypeError as exc:
        raise RunnerProtocolError("Runner Turn usage is invalid") from exc
    _validate_usage(usage)
    return usage


def _validate_usage(usage: CodexTurnUsage | None) -> None:
    if not isinstance(usage, CodexTurnUsage):
        raise RunnerProtocolError("finished v2 Runner Turn requires usage")
    for value in (
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.cache_write_input_tokens,
        usage.output_tokens,
        usage.reasoning_output_tokens,
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RunnerProtocolError("Runner Turn usage is invalid")


@dataclass(frozen=True, slots=True)
class RunnerExportReply:
    work_item_id: str
    head_sha: str
    bundle_sha256: str
    size_bytes: int
    version: int = PROTOCOL_VERSION
    operation: RunnerOperation = RunnerOperation.EXPORT

    def __post_init__(self) -> None:
        if self.operation is not RunnerOperation.EXPORT:
            raise RunnerProtocolError("operation does not return an export reply")
        if type(self.version) is not int or self.version != PROTOCOL_VERSION:
            raise RunnerProtocolError("unsupported Runner response version")
        validate_work_item_id(self.work_item_id)
        validate_git_sha(self.head_sha, "head_sha")
        validate_sha256(self.bundle_sha256, "bundle_sha256")
        if type(self.size_bytes) is not int or not 1 <= self.size_bytes <= MAX_ARTIFACT_BYTES:
            raise RunnerProtocolError("Runner export size is invalid")

    def to_json(self) -> str:
        return _dump(
            {
                "version": self.version,
                "op": self.operation.value,
                "work_item_id": self.work_item_id,
                "head_sha": self.head_sha,
                "bundle_sha256": self.bundle_sha256,
                "size_bytes": self.size_bytes,
            }
        )

    def validate_artifact(self, artifact: bytes | None) -> bytes:
        if artifact is None or len(artifact) != self.size_bytes:
            raise RunnerProtocolError("Runner export artifact size does not match its manifest")
        if sha256(artifact).hexdigest() != self.bundle_sha256:
            raise RunnerProtocolError("Runner export artifact hash does not match its manifest")
        return artifact


def parse_runner_export_reply(value: str | bytes) -> RunnerExportReply:
    payload = _load(value)
    expected = {
        "version",
        "op",
        "work_item_id",
        "head_sha",
        "bundle_sha256",
        "size_bytes",
    }
    if set(payload) != expected or payload.get("op") != RunnerOperation.EXPORT.value:
        raise RunnerProtocolError("Runner export response fields are invalid")
    try:
        return RunnerExportReply(
            work_item_id=payload["work_item_id"],
            head_sha=payload["head_sha"],
            bundle_sha256=payload["bundle_sha256"],
            size_bytes=payload["size_bytes"],
            version=payload["version"],
        )
    except (TypeError, ValueError) as exc:
        raise RunnerProtocolError(str(exc)) from exc


def _load(value: str | bytes) -> dict[str, Any]:
    if isinstance(value, str):
        raw = value.encode("utf-8")
    elif isinstance(value, bytes):
        raw = value
    else:
        raise RunnerProtocolError("Runner response must be UTF-8 JSON")
    if not raw or len(raw) > MAX_RESPONSE_BYTES or b"\x00" in raw:
        raise RunnerProtocolError("Runner response exceeds its safe boundary")
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerProtocolError("Runner response must be valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise RunnerProtocolError("Runner response must be a JSON object")
    return parsed


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RunnerProtocolError(f"duplicate Runner response field: {key}")
        result[key] = value
    return result
