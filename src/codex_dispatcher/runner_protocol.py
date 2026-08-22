"""Versioned, strict offline contracts for the SSH Runner boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from codex_dispatcher.redaction import redact_text
from codex_dispatcher.task_spec import normalize_repo_path
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


PROTOCOL_VERSION = 1
NEXT_PROTOCOL_VERSION = 2
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESULT_BYTES = 256 * 1024
MAX_INPUT_ARTIFACT_BYTES = 100 * 1024 * 1024


class RunnerProtocolError(ValueError):
    """Raised when untrusted Runner protocol data violates the pinned contract."""


class RunnerOperation(StrEnum):
    PREPARE = "prepare"
    START = "start"
    RESUME = "resume"
    STATUS = "status"
    EXPORT = "export"
    STOP = "stop"
    ARCHIVE = "archive"


_V1_REQUEST_FIELDS = {
    RunnerOperation.PREPARE: frozenset(
        {
            "version",
            "op",
            "work_item_id",
            "repository",
            "issue_number",
            "task_branch",
            "base_sha",
            "source_bundle_sha256",
            "source_bundle_size",
        }
    ),
    RunnerOperation.START: frozenset(
        {
            "version",
            "op",
            "work_item_id",
            "turn_id",
            "prompt_sha256",
            "input_head_sha",
        }
    ),
    RunnerOperation.RESUME: frozenset(
        {
            "version",
            "op",
            "work_item_id",
            "turn_id",
            "session_id",
            "prompt_sha256",
            "input_head_sha",
        }
    ),
    RunnerOperation.STATUS: frozenset({"version", "op", "work_item_id", "turn_id"}),
    RunnerOperation.EXPORT: frozenset(
        {"version", "op", "work_item_id", "expected_head_sha"}
    ),
    RunnerOperation.STOP: frozenset({"version", "op", "work_item_id", "turn_id"}),
    RunnerOperation.ARCHIVE: frozenset({"version", "op", "work_item_id"}),
}


_V2_GENERATION_FIELDS = frozenset(
    {"session_generation_id", "session_generation", "agent_policy_digest"}
)
_V2_REQUEST_FIELDS = {
    RunnerOperation.START: _V1_REQUEST_FIELDS[RunnerOperation.START]
    | _V2_GENERATION_FIELDS,
    RunnerOperation.RESUME: _V1_REQUEST_FIELDS[RunnerOperation.RESUME]
    | _V2_GENERATION_FIELDS,
    RunnerOperation.STATUS: _V1_REQUEST_FIELDS[RunnerOperation.STATUS]
    | _V2_GENERATION_FIELDS,
    RunnerOperation.STOP: _V1_REQUEST_FIELDS[RunnerOperation.STOP]
    | _V2_GENERATION_FIELDS,
}


@dataclass(frozen=True, slots=True)
class RunnerRequest:
    operation: RunnerOperation
    work_item_id: str
    turn_id: str | None = None
    repository: str | None = None
    issue_number: int | None = None
    task_branch: str | None = None
    base_sha: str | None = None
    session_id: str | None = None
    prompt_sha256: str | None = None
    input_head_sha: str | None = None
    expected_head_sha: str | None = None
    source_bundle_sha256: str | None = None
    source_bundle_size: int | None = None
    session_generation_id: str | None = None
    session_generation: int | None = None
    agent_policy_digest: str | None = None
    version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.operation, RunnerOperation):
            raise RunnerProtocolError("operation must be a RunnerOperation")
        if type(self.version) is not int or self.version not in {
            PROTOCOL_VERSION,
            NEXT_PROTOCOL_VERSION,
        }:
            raise RunnerProtocolError("unsupported Runner protocol version")
        validate_work_item_id(self.work_item_id)
        payload = self.to_mapping()
        expected = _request_fields(self.version, self.operation)
        if set(payload) != expected:
            raise RunnerProtocolError("Runner request fields do not match the operation")
        if self.turn_id is not None:
            validate_turn_id(self.turn_id)
        if self.repository is not None:
            validate_repository(self.repository)
        if self.issue_number is not None and (
            type(self.issue_number) is not int or self.issue_number <= 0
        ):
            raise RunnerProtocolError("issue_number must be a positive integer")
        if self.task_branch is not None:
            validate_branch(self.task_branch)
        if self.base_sha is not None:
            validate_git_sha(self.base_sha, "base_sha")
        if self.session_id is not None:
            validate_session_id(self.session_id)
        if self.prompt_sha256 is not None:
            validate_sha256(self.prompt_sha256, "prompt_sha256")
        if self.input_head_sha is not None:
            validate_git_sha(self.input_head_sha, "input_head_sha")
        if self.expected_head_sha is not None:
            validate_git_sha(self.expected_head_sha, "expected_head_sha")
        if self.source_bundle_sha256 is not None:
            validate_sha256(self.source_bundle_sha256, "source_bundle_sha256")
        if self.source_bundle_size is not None and (
            type(self.source_bundle_size) is not int
            or not 1 <= self.source_bundle_size <= MAX_INPUT_ARTIFACT_BYTES
        ):
            raise RunnerProtocolError("source_bundle_size is invalid")
        if self.session_generation_id is not None:
            try:
                validate_session_generation_id(self.session_generation_id)
            except ValueError as exc:
                raise RunnerProtocolError(str(exc)) from exc
        if self.session_generation is not None and (
            type(self.session_generation) is not int or self.session_generation <= 0
        ):
            raise RunnerProtocolError("session_generation must be a positive integer")
        if self.agent_policy_digest is not None:
            try:
                validate_sha256(self.agent_policy_digest, "agent_policy_digest")
            except ValueError as exc:
                raise RunnerProtocolError(str(exc)) from exc

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": self.version,
            "op": self.operation.value,
            "work_item_id": self.work_item_id,
        }
        optional = (
            ("turn_id", self.turn_id),
            ("repository", self.repository),
            ("issue_number", self.issue_number),
            ("task_branch", self.task_branch),
            ("base_sha", self.base_sha),
            ("session_id", self.session_id),
            ("prompt_sha256", self.prompt_sha256),
            ("input_head_sha", self.input_head_sha),
            ("expected_head_sha", self.expected_head_sha),
            ("source_bundle_sha256", self.source_bundle_sha256),
            ("source_bundle_size", self.source_bundle_size),
            ("session_generation_id", self.session_generation_id),
            ("session_generation", self.session_generation),
            ("agent_policy_digest", self.agent_policy_digest),
        )
        payload.update((name, value) for name, value in optional if value is not None)
        return payload

    def to_json(self) -> str:
        return json.dumps(
            self.to_mapping(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )


def parse_runner_request(value: str | bytes) -> RunnerRequest:
    payload = _load_object(value, maximum=MAX_REQUEST_BYTES, description="Runner request")
    if set(payload) < {"version", "op", "work_item_id"}:
        raise RunnerProtocolError("Runner request is missing routing fields")
    try:
        operation = RunnerOperation(payload["op"])
    except (TypeError, ValueError) as exc:
        raise RunnerProtocolError("unsupported Runner operation") from exc
    version = payload.get("version")
    if type(version) is not int:
        raise RunnerProtocolError("unsupported Runner protocol version")
    if set(payload) != _request_fields(version, operation):
        raise RunnerProtocolError("Runner request has unexpected or missing fields")
    try:
        return RunnerRequest(
            version=payload["version"],
            operation=operation,
            work_item_id=payload["work_item_id"],
            turn_id=payload.get("turn_id"),
            repository=payload.get("repository"),
            issue_number=payload.get("issue_number"),
            task_branch=payload.get("task_branch"),
            base_sha=payload.get("base_sha"),
            session_id=payload.get("session_id"),
            prompt_sha256=payload.get("prompt_sha256"),
            input_head_sha=payload.get("input_head_sha"),
            expected_head_sha=payload.get("expected_head_sha"),
            source_bundle_sha256=payload.get("source_bundle_sha256"),
            source_bundle_size=payload.get("source_bundle_size"),
            session_generation_id=payload.get("session_generation_id"),
            session_generation=payload.get("session_generation"),
            agent_policy_digest=payload.get("agent_policy_digest"),
        )
    except (TypeError, ValueError) as exc:
        raise RunnerProtocolError(str(exc)) from exc


def _request_fields(version: int, operation: RunnerOperation) -> frozenset[str]:
    if version == PROTOCOL_VERSION:
        return _V1_REQUEST_FIELDS[operation]
    if version == NEXT_PROTOCOL_VERSION and operation in _V2_REQUEST_FIELDS:
        return _V2_REQUEST_FIELDS[operation]
    raise RunnerProtocolError("unsupported Runner protocol version or operation")


class AgentResultStatus(StrEnum):
    COMPLETED = "completed"
    NEEDS_INPUT = "needs_input"
    BLOCKED = "blocked"


class TestStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"


@dataclass(frozen=True, slots=True)
class TestResult:
    name: str
    status: TestStatus


@dataclass(frozen=True, slots=True)
class AgentResult:
    status: AgentResultStatus
    summary: str
    needs_input: tuple[str, ...]
    tests: tuple[TestResult, ...]
    changed_paths: tuple[str, ...]
    next_step: str


def agent_result_to_mapping(result: AgentResult) -> dict[str, object]:
    if not isinstance(result, AgentResult):
        raise RunnerProtocolError("result must be an AgentResult")
    return {
        "status": result.status.value,
        "summary": result.summary,
        "needs_input": list(result.needs_input),
        "tests": [
            {"name": test.name, "status": test.status.value} for test in result.tests
        ],
        "changed_paths": list(result.changed_paths),
        "next_step": result.next_step,
    }


def agent_result_to_json(result: AgentResult) -> str:
    mapping = agent_result_to_mapping(result)
    encoded = json.dumps(mapping, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if parse_agent_result(encoded) != result:
        raise RunnerProtocolError("AgentResult is not valid under the wire contract")
    return encoded


def parse_agent_result(
    value: str | bytes, *, explicit_secrets: tuple[str, ...] = ()
) -> AgentResult:
    payload = _load_object(value, maximum=MAX_RESULT_BYTES, description="Agent result")
    required = {"status", "summary", "needs_input", "tests", "changed_paths", "next_step"}
    if set(payload) != required:
        raise RunnerProtocolError("Agent result has unexpected or missing fields")
    try:
        status = AgentResultStatus(payload["status"])
    except (TypeError, ValueError) as exc:
        raise RunnerProtocolError("Agent result status is unsupported") from exc
    summary = _redacted_text(
        payload["summary"], "summary", maximum=8_000, explicit_secrets=explicit_secrets
    )
    next_step = _redacted_text(
        payload["next_step"],
        "next_step",
        maximum=2_000,
        explicit_secrets=explicit_secrets,
    )
    needs_input = _string_array(
        payload["needs_input"],
        "needs_input",
        maximum_items=20,
        maximum_text=1_000,
        explicit_secrets=explicit_secrets,
    )
    if status is AgentResultStatus.NEEDS_INPUT and not needs_input:
        raise RunnerProtocolError("needs_input status requires at least one question")
    if status is not AgentResultStatus.NEEDS_INPUT and needs_input:
        raise RunnerProtocolError("needs_input questions require needs_input status")

    raw_tests = payload["tests"]
    if not isinstance(raw_tests, list) or len(raw_tests) > 100:
        raise RunnerProtocolError("tests must be a bounded array")
    tests: list[TestResult] = []
    for item in raw_tests:
        if not isinstance(item, dict) or set(item) != {"name", "status"}:
            raise RunnerProtocolError("test result has unexpected or missing fields")
        name = _redacted_text(
            item["name"],
            "test name",
            maximum=500,
            explicit_secrets=explicit_secrets,
        )
        try:
            test_status = TestStatus(item["status"])
        except (TypeError, ValueError) as exc:
            raise RunnerProtocolError("test status is unsupported") from exc
        tests.append(TestResult(name, test_status))

    raw_paths = payload["changed_paths"]
    if not isinstance(raw_paths, list) or len(raw_paths) > 1_000:
        raise RunnerProtocolError("changed_paths must be a bounded array")
    try:
        changed_paths = tuple(normalize_repo_path(path) for path in raw_paths)
    except (TypeError, ValueError) as exc:
        raise RunnerProtocolError("changed_paths contains an unsafe path") from exc
    if len(set(changed_paths)) != len(changed_paths):
        raise RunnerProtocolError("changed_paths must not contain duplicates")
    return AgentResult(status, summary, needs_input, tuple(tests), changed_paths, next_step)


def _load_object(value: str | bytes, *, maximum: int, description: str) -> dict[str, Any]:
    if isinstance(value, str):
        raw = value.encode("utf-8")
    elif isinstance(value, bytes):
        raw = value
    else:
        raise RunnerProtocolError(f"{description} must be UTF-8 JSON")
    if not raw or len(raw) > maximum or b"\x00" in raw:
        raise RunnerProtocolError(f"{description} exceeds its safe size or encoding boundary")
    try:
        text = raw.decode("utf-8")
        parsed = json.loads(text, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerProtocolError(f"{description} must be valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise RunnerProtocolError(f"{description} must be a JSON object")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RunnerProtocolError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _string_array(
    value: Any,
    field: str,
    *,
    maximum_items: int,
    maximum_text: int,
    explicit_secrets: tuple[str, ...],
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise RunnerProtocolError(f"{field} must be a bounded array")
    return tuple(
        _redacted_text(
            item,
            f"{field} item",
            maximum=maximum_text,
            explicit_secrets=explicit_secrets,
        )
        for item in value
    )


def _bounded_text(value: Any, field: str, *, maximum: int) -> str:
    allowed_controls = {9, 10, 13}
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
        raise RunnerProtocolError(f"{field} must be non-empty bounded text")
    return value


def _redacted_text(
    value: Any,
    field: str,
    *,
    maximum: int,
    explicit_secrets: tuple[str, ...],
) -> str:
    bounded = _bounded_text(value, field, maximum=maximum)
    redacted = redact_text(bounded, explicit_secrets)
    if len(redacted) > maximum:
        raise RunnerProtocolError(f"{field} exceeds its bound after redaction")
    return redacted
