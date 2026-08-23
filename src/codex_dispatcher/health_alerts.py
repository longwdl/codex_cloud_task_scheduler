"""Durable state models for lifecycle-health Slack notifications."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from codex_dispatcher.slack_reporting import (
    MAX_SLACK_TEXT_CHARS,
    SlackDeliveryState,
    SlackOutboundMessage,
    validate_slack_channel_id,
)


class HealthAlertKind(StrEnum):
    ALERT = "alert"
    RECOVERY = "recovery"


@dataclass(frozen=True, slots=True)
class HealthAlertDelivery:
    delivery_key: str
    kind: HealthAlertKind
    fingerprint: str
    channel_id: str
    report_text: str
    payload_sha256: str
    state: SlackDeliveryState
    created_at: str
    updated_at: str
    thread_ts: str | None = None
    message_ts: str | None = None
    permalink: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, HealthAlertKind):
            raise ValueError("health alert kind is invalid")
        if not isinstance(self.state, SlackDeliveryState):
            raise ValueError("health alert delivery state is invalid")
        if re.fullmatch(r"slack-health:[A-Za-z0-9:._-]{1,220}", self.delivery_key) is None:
            raise ValueError("health alert delivery key is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.fingerprint) is None:
            raise ValueError("health alert fingerprint is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.payload_sha256) is None:
            raise ValueError("health alert payload hash is invalid")
        validate_slack_channel_id(self.channel_id)
        if not self.report_text or len(self.report_text) > MAX_SLACK_TEXT_CHARS:
            raise ValueError("health alert report text is invalid")
        if any(
            not isinstance(value, str)
            or not value
            or len(value) > 64
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            for value in (self.created_at, self.updated_at)
        ):
            raise ValueError("health alert timestamps are invalid")
        outbound = self.to_outbound_message()
        delivered = self.state is SlackDeliveryState.DELIVERED
        if delivered != (self.message_ts is not None and self.permalink is not None):
            raise ValueError("health alert receipt conflicts with its state")
        if self.kind is HealthAlertKind.ALERT and self.thread_ts is not None:
            raise ValueError("health alerts must be root messages")
        if self.kind is HealthAlertKind.RECOVERY and self.thread_ts is None:
            raise ValueError("health recoveries must target the alert thread")
        if delivered:
            from codex_dispatcher.slack_reporting import SlackDeliveryReceipt

            assert self.message_ts is not None
            assert self.permalink is not None
            SlackDeliveryReceipt(
                deduplication_key=self.delivery_key,
                channel_id=self.channel_id,
                message_ts=self.message_ts,
                thread_ts=outbound.thread_ts or self.message_ts,
                permalink=self.permalink,
            )

    def to_outbound_message(self) -> SlackOutboundMessage:
        return SlackOutboundMessage(
            deduplication_key=self.delivery_key,
            channel_id=self.channel_id,
            text=self.report_text,
            thread_ts=self.thread_ts,
        )


@dataclass(frozen=True, slots=True)
class ActiveHealthAlert:
    fingerprint: str
    delivery_key: str
    updated_at: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.fingerprint) is None:
            raise ValueError("active health alert fingerprint is invalid")
        if re.fullmatch(r"slack-health:[A-Za-z0-9:._-]{1,220}", self.delivery_key) is None:
            raise ValueError("active health alert delivery key is invalid")
        if not isinstance(self.updated_at, str) or not self.updated_at:
            raise ValueError("active health alert timestamp is invalid")
