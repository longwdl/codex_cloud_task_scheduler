"""Outbound-only Slack report models; this module intentionally has no input adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

from codex_dispatcher.redaction import redact_text
from codex_dispatcher.work_items import validate_turn_id, validate_work_item_id


MAX_SLACK_TEXT_CHARS = 3_000
_CHANNEL_RE = re.compile(r"[A-Z0-9]{2,32}")
_MESSAGE_TS_RE = re.compile(r"[0-9]{1,20}\.[0-9]{1,20}")
_PERMALINK_PATH_RE = re.compile(
    r"/archives/([A-Z0-9]{2,32})/p([0-9]{2,40})"
)


class SlackReportKind(StrEnum):
    ROOT = "root"
    STATUS = "status"
    RESULT = "result"
    QUESTION = "question"
    FAILURE = "failure"


class SlackDeliveryState(StrEnum):
    PREPARED = "prepared"
    DELIVERED = "delivered"


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
        validate_slack_channel_id(self.channel_id)
        if self.thread_ts is not None:
            _validate_message_ts(self.thread_ts, "thread_ts")
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


@dataclass(frozen=True, slots=True)
class SlackDeliveryReceipt:
    """Provider receipt returned for one idempotently published report."""

    deduplication_key: str
    channel_id: str
    message_ts: str
    thread_ts: str
    permalink: str

    def __post_init__(self) -> None:
        _validate_deduplication_key(self.deduplication_key)
        validate_slack_channel_id(self.channel_id)
        _validate_message_ts(self.message_ts, "message_ts")
        _validate_message_ts(self.thread_ts, "thread_ts")
        validate_slack_permalink(
            self.permalink,
            channel_id=self.channel_id,
            message_ts=self.message_ts,
            thread_ts=self.thread_ts,
        )


@dataclass(frozen=True, slots=True)
class SlackDeliveryRecord:
    """Durable outbox identity without persisted report content."""

    deduplication_key: str
    work_item_id: str
    kind: SlackReportKind
    channel_id: str
    payload_sha256: str
    state: SlackDeliveryState
    created_at: str
    updated_at: str
    turn_id: str | None = None
    thread_ts: str | None = None
    message_ts: str | None = None
    permalink: str | None = None

    def __post_init__(self) -> None:
        validate_work_item_id(self.work_item_id)
        if not isinstance(self.kind, SlackReportKind):
            raise ValueError("Slack delivery kind is invalid")
        if not isinstance(self.state, SlackDeliveryState):
            raise ValueError("Slack delivery state is invalid")
        validate_slack_channel_id(self.channel_id)
        if re.fullmatch(r"[0-9a-f]{64}", self.payload_sha256) is None:
            raise ValueError("Slack delivery payload hash is invalid")
        if any(
            not isinstance(value, str)
            or not value
            or len(value) > 64
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            for value in (self.created_at, self.updated_at)
        ):
            raise ValueError("Slack delivery timestamps are required")
        expected_key = slack_deduplication_key(
            self.work_item_id,
            kind=self.kind,
            turn_id=self.turn_id,
        )
        if self.deduplication_key != expected_key:
            raise ValueError("Slack delivery key conflicts with its identity")
        if self.kind is SlackReportKind.ROOT:
            if self.turn_id is not None or self.thread_ts is not None:
                raise ValueError("Slack root delivery cannot target a Turn or thread")
        else:
            if self.turn_id is None or self.thread_ts is None:
                raise ValueError("Slack Turn delivery requires a Turn and thread")
            _validate_message_ts(self.thread_ts, "thread_ts")
        delivered = self.state is SlackDeliveryState.DELIVERED
        if (self.message_ts is None) != (self.permalink is None) or delivered != (
            self.message_ts is not None
        ):
            raise ValueError("Slack delivery receipt fields conflict with its state")
        if self.message_ts is not None:
            _validate_message_ts(self.message_ts, "message_ts")
            assert self.permalink is not None
            validate_slack_permalink(
                self.permalink,
                channel_id=self.channel_id,
                message_ts=self.message_ts,
                thread_ts=self.thread_ts or self.message_ts,
            )


class SlackPublisher(Protocol):
    """Publish only outbound reports, idempotently by ``deduplication_key``.

    Repeating the exact same report key and payload after an ambiguous response
    must return the original message receipt and must not create another message.
    Implementations must send plain text with markup, mention expansion, link
    unfurling, and reply broadcast disabled. Implementations that cannot prove
    those behaviors are not safe to enable.
    """

    def publish(self, report: SlackReport) -> SlackDeliveryReceipt: ...


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
    if not isinstance(kind, SlackReportKind):
        raise TypeError("kind must be a SlackReportKind")
    if kind is SlackReportKind.ROOT:
        if turn_id is not None:
            raise ValueError("Slack root deduplication does not accept a Turn")
        return f"slack:{work_item_id}:root"
    if turn_id is None:
        raise ValueError("Slack Turn deduplication requires a Turn")
    validate_turn_id(turn_id)
    return f"slack:{work_item_id}:{turn_id}:{kind.value}"


def validate_slack_permalink(
    permalink: str,
    *,
    channel_id: str,
    message_ts: str | None = None,
    thread_ts: str | None = None,
) -> str:
    validate_slack_channel_id(channel_id)
    if (
        not isinstance(permalink, str)
        or len(permalink) > 2_048
        or "\x00" in permalink
    ):
        raise ValueError("Slack permalink is invalid")
    parsed = urlsplit(permalink)
    host = parsed.hostname
    path_match = _PERMALINK_PATH_RE.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or host is None
        or not host.endswith(".slack.com")
        or host == ".slack.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.fragment
        or path_match is None
        or path_match.group(1) != channel_id
    ):
        raise ValueError("Slack permalink is invalid")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(
        key not in {"thread_ts", "cid"}
        or not query_value
        or (key == "cid" and query_value != channel_id)
        or (
            key == "thread_ts"
            and _MESSAGE_TS_RE.fullmatch(query_value) is None
        )
        for key, query_value in query
    ) or len({key for key, _ in query}) != len(query):
        raise ValueError("Slack permalink query is invalid")
    if message_ts is not None:
        _validate_message_ts(message_ts, "message_ts")
        if path_match.group(2) != message_ts.replace(".", ""):
            raise ValueError("Slack permalink message conflicts with its receipt")
    if thread_ts is not None:
        _validate_message_ts(thread_ts, "thread_ts")
        expected_query = (
            {}
            if thread_ts == message_ts
            else {"thread_ts": thread_ts, "cid": channel_id}
        )
        if dict(query) != expected_query:
            raise ValueError("Slack permalink thread conflicts with its receipt")
    return permalink


def validate_slack_channel_id(value: str) -> str:
    if not isinstance(value, str) or _CHANNEL_RE.fullmatch(value) is None:
        raise ValueError("channel_id is invalid")
    return value


def _validate_message_ts(value: str, field: str) -> str:
    if not isinstance(value, str) or _MESSAGE_TS_RE.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _validate_deduplication_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or re.fullmatch(r"[A-Za-z0-9:._-]+", value) is None
    ):
        raise ValueError("Slack deduplication key is invalid")
    return value
