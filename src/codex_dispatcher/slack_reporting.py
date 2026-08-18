"""Outbound-only Slack report models; this module intentionally has no input adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from codex_dispatcher.redaction import redact_text
from codex_dispatcher.work_items import validate_turn_id, validate_work_item_id


MAX_SLACK_TEXT_CHARS = 3_000
_CHANNEL_RE = re.compile(r"[A-Z0-9]{2,32}")


class SlackReportKind(StrEnum):
    ROOT = "root"
    STATUS = "status"
    RESULT = "result"
    QUESTION = "question"
    FAILURE = "failure"


@dataclass(frozen=True, slots=True)
class SlackReport:
    deduplication_key: str
    work_item_id: str
    kind: SlackReportKind
    channel_id: str
    text: str
    turn_id: str | None = None
    thread_ts: str | None = None

    def __post_init__(self) -> None:
        validate_work_item_id(self.work_item_id)
        if self.turn_id is not None:
            validate_turn_id(self.turn_id)
        if not isinstance(self.channel_id, str) or _CHANNEL_RE.fullmatch(self.channel_id) is None:
            raise ValueError("channel_id is invalid")
        if self.thread_ts is not None and (
            not isinstance(self.thread_ts, str)
            or not self.thread_ts
            or len(self.thread_ts) > 128
            or any(not (character.isdigit() or character == ".") for character in self.thread_ts)
        ):
            raise ValueError("thread_ts is invalid")
        if (
            not isinstance(self.text, str)
            or not self.text
            or len(self.text) > MAX_SLACK_TEXT_CHARS
            or any(
                (ord(character) < 32 and ord(character) not in {9, 10, 13})
                or ord(character) == 127
                for character in self.text
            )
        ):
            raise ValueError("Slack report text is invalid")
        expected = slack_deduplication_key(
            self.work_item_id, kind=self.kind, turn_id=self.turn_id
        )
        if self.deduplication_key != expected:
            raise ValueError("Slack report deduplication key is not deterministic")
        if self.kind is SlackReportKind.ROOT and (
            self.turn_id is not None or self.thread_ts is not None
        ):
            raise ValueError("Slack root reports cannot target an existing thread or Turn")
        if self.kind is not SlackReportKind.ROOT and (
            self.turn_id is None or self.thread_ts is None
        ):
            raise ValueError("Slack Turn reports require a Turn and existing thread")


def build_slack_report(
    *,
    work_item_id: str,
    kind: SlackReportKind,
    channel_id: str,
    text: str,
    turn_id: str | None = None,
    thread_ts: str | None = None,
    explicit_secrets: tuple[str, ...] = (),
) -> SlackReport:
    redacted = redact_text(text, explicit_secrets)
    if len(redacted) > MAX_SLACK_TEXT_CHARS:
        redacted = redacted[: MAX_SLACK_TEXT_CHARS - 14] + "\n[TRUNCATED]"
    return SlackReport(
        deduplication_key=slack_deduplication_key(
            work_item_id, kind=kind, turn_id=turn_id
        ),
        work_item_id=work_item_id,
        kind=kind,
        channel_id=channel_id,
        text=redacted,
        turn_id=turn_id,
        thread_ts=thread_ts,
    )


def slack_deduplication_key(
    work_item_id: str, *, kind: SlackReportKind, turn_id: str | None
) -> str:
    validate_work_item_id(work_item_id)
    if kind is SlackReportKind.ROOT:
        if turn_id is not None:
            raise ValueError("Slack root deduplication does not accept a Turn")
        return f"slack:{work_item_id}:root"
    if turn_id is None:
        raise ValueError("Slack Turn deduplication requires a Turn")
    validate_turn_id(turn_id)
    return f"slack:{work_item_id}:{turn_id}:{kind.value}"
