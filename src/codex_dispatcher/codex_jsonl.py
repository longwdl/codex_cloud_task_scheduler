"""Strict, bounded interpretation of ``codex exec --json`` event streams."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from codex_dispatcher.redaction import redact_text
from codex_dispatcher.work_items import validate_session_id


MAX_JSONL_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
MAX_EVENTS = 10_000
MAX_FINAL_MESSAGE_CHARS = 64_000


class CodexJsonlError(ValueError):
    """Raised when Codex JSONL cannot prove one session and one terminal outcome."""

    def __init__(self, message: str, *, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


class CodexTerminalStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CodexTurnUsage:
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int


@dataclass(frozen=True, slots=True)
class CodexExecutionSummary:
    session_id: str
    status: CodexTerminalStatus
    event_count: int
    final_message: str | None
    errors: tuple[str, ...]
    usage: CodexTurnUsage | None = None


_CONTEXT_FAILURE_MARKERS = (
    "context_length_exceeded",
    "ran out of room in the context window",
    "remote compact failed",
    "compaction failure",
)


def is_context_failure(summary: CodexExecutionSummary) -> bool:
    """Classify only bounded known Codex context/compaction failure markers."""
    if not isinstance(summary, CodexExecutionSummary):
        raise TypeError("summary must be a CodexExecutionSummary")
    return any(
        marker in message.casefold()
        for message in summary.errors
        for marker in _CONTEXT_FAILURE_MARKERS
    )


def parse_codex_jsonl(
    value: str | bytes,
    *,
    expected_session_id: str | None = None,
    explicit_secrets: tuple[str, ...] = (),
) -> CodexExecutionSummary:
    """Parse one completed invocation without retaining raw command output."""
    if expected_session_id is not None:
        validate_session_id(expected_session_id)
    if isinstance(value, str):
        raw = value.encode("utf-8")
    elif isinstance(value, bytes):
        raw = value
    else:
        raise CodexJsonlError("Codex output must be UTF-8 JSONL", code="input_type")
    if not raw or len(raw) > MAX_JSONL_BYTES or b"\x00" in raw:
        raise CodexJsonlError(
            "Codex output exceeds its safe size or encoding boundary",
            code="size_or_encoding",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodexJsonlError(
            "Codex output must be UTF-8 JSONL", code="utf8"
        ) from exc

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or len(lines) > MAX_EVENTS:
        raise CodexJsonlError(
            "Codex output has an invalid event count", code="event_count"
        )
    session_id: str | None = None
    terminal: CodexTerminalStatus | None = None
    final_message: str | None = None
    usage: CodexTurnUsage | None = None
    errors: list[str] = []

    for index, line in enumerate(lines):
        if len(line.encode("utf-8")) > MAX_EVENT_BYTES:
            raise CodexJsonlError(
                "Codex event exceeds its safe size", code="event_size"
            )
        try:
            event = json.loads(line, object_pairs_hook=_unique_object)
        except json.JSONDecodeError as exc:
            raise CodexJsonlError(
                "Codex output contains malformed JSON", code="malformed_json"
            ) from exc
        if not isinstance(event, dict):
            raise CodexJsonlError(
                "Codex event must be a JSON object", code="event_not_object"
            )
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type or len(event_type) > 128:
            raise CodexJsonlError("Codex event type is invalid", code="event_type")

        if event_type == "thread.started":
            if terminal is not None:
                raise CodexJsonlError(
                    "Codex session started after the terminal event",
                    code="session_after_terminal",
                )
            if session_id is not None:
                raise CodexJsonlError(
                    "Codex output contains multiple thread.started events",
                    code="duplicate_thread_started",
                )
            candidate = event.get("thread_id")
            try:
                session_id = validate_session_id(candidate)
            except ValueError as exc:
                raise CodexJsonlError(
                    "Codex thread.started has an invalid session ID",
                    code="thread_id_invalid",
                ) from exc
        elif event_type in {"turn.completed", "turn.failed"}:
            if session_id is None:
                raise CodexJsonlError(
                    "Codex terminal event appeared before thread.started",
                    code="terminal_before_thread",
                )
            if terminal is not None:
                raise CodexJsonlError(
                    "Codex output contains multiple terminal Turn events",
                    code="duplicate_terminal",
                )
            if index != len(lines) - 1:
                raise CodexJsonlError(
                    "Codex output contains events after the terminal Turn event",
                    code="event_after_terminal",
                )
            terminal = (
                CodexTerminalStatus.COMPLETED
                if event_type == "turn.completed"
                else CodexTerminalStatus.FAILED
            )
            raw_usage = event.get("usage")
            if event_type == "turn.completed" and raw_usage is not None:
                usage = _parse_turn_usage(raw_usage)
                if usage is None:
                    raise CodexJsonlError(
                        "Codex turn usage must be a usage object", code="usage_invalid"
                    )
            elif event_type == "turn.failed" and "usage" in event:
                raise CodexJsonlError(
                    "failed Codex turns must not contain usage", code="usage_invalid"
                )
        elif event_type == "error":
            message = event.get("message")
            if isinstance(message, str) and message:
                errors.append(_bounded_redacted(message, explicit_secrets))
            else:
                errors.append("Codex reported an unspecified error")
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                message = item.get("text")
                if not isinstance(message, str) or not message:
                    raise CodexJsonlError(
                        "Codex agent_message text is invalid",
                        code="agent_message_invalid",
                    )
                final_message = _bounded_redacted(message, explicit_secrets)

    if session_id is None:
        raise CodexJsonlError(
            "Codex output is missing thread.started", code="missing_thread_started"
        )
    if expected_session_id is not None and session_id != expected_session_id:
        raise CodexJsonlError(
            "Codex resumed a different session", code="session_conflict"
        )
    if terminal is None:
        raise CodexJsonlError(
            "Codex output is missing a terminal Turn event", code="missing_terminal"
        )
    if terminal is CodexTerminalStatus.COMPLETED and final_message is None:
        raise CodexJsonlError(
            "completed Codex output is missing the final agent message",
            code="missing_final_message",
        )
    return CodexExecutionSummary(
        session_id=session_id,
        status=terminal,
        event_count=len(lines),
        final_message=final_message,
        errors=tuple(errors),
        usage=usage,
    )


def _parse_turn_usage(value: Any) -> CodexTurnUsage | None:
    if not isinstance(value, dict):
        return None
    required_keys = {
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    }
    if set(value.keys()) != required_keys:
        return None
    parsed: dict[str, int] = {}
    for key in required_keys:
        raw = value[key]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return None
        parsed[key] = raw
    return CodexTurnUsage(
        input_tokens=parsed["input_tokens"],
        cached_input_tokens=parsed["cached_input_tokens"],
        cache_write_input_tokens=parsed["cache_write_input_tokens"],
        output_tokens=parsed["output_tokens"],
        reasoning_output_tokens=parsed["reasoning_output_tokens"],
    )


def _bounded_redacted(value: str, explicit_secrets: tuple[str, ...]) -> str:
    allowed_controls = {9, 10, 13}
    if any(
        (ord(character) < 32 and ord(character) not in allowed_controls)
        or ord(character) == 127
        for character in value
    ):
        raise CodexJsonlError(
            "Codex message contains unsupported control characters",
            code="message_control_character",
        )
    redacted = redact_text(value, explicit_secrets)
    if len(redacted) > MAX_FINAL_MESSAGE_CHARS:
        raise CodexJsonlError(
            "Codex message exceeds its safe size", code="message_size"
        )
    return redacted


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CodexJsonlError(
                f"Codex event contains duplicate field: {key}", code="duplicate_field"
            )
        result[key] = value
    return result
