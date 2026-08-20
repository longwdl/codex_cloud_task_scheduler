"""Bounded live proof for Slack ``client_msg_id`` deduplication."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable
from uuid import UUID

from codex_dispatcher.slack_reporting import (
    SlackDeliveryReceipt,
    SlackPublisher,
    SlackReportKind,
    build_slack_report,
    validate_slack_channel_id,
)
from codex_dispatcher.slack_web_api import (
    SlackHttpResponse,
    SlackHttpTransport,
    SlackWebApiPublisher,
    UrllibSlackHttpTransport,
    slack_client_message_id,
)


_AUTH_TEST_URL = "https://slack.com/api/auth.test"
_WORKSPACE_ID_RE = re.compile(r"T[A-Z0-9]{1,31}")
_BOT_ID_RE = re.compile(r"B[A-Z0-9]{1,31}")
_RETRY_SPACING_SECONDS = 1.1


class SlackLiveFixtureError(RuntimeError):
    """The target Slack installation did not prove the required contract."""


@dataclass(frozen=True, slots=True)
class SlackLiveFixtureResult:
    workspace_id: str
    channel_id: str
    fixture_id: str
    client_msg_id: str
    receipt: SlackDeliveryReceipt


def run_slack_idempotency_fixture(
    *,
    bot_token: str,
    workspace_id: str,
    channel_id: str,
    fixture_id: str,
    timeout_seconds: float,
    transport: SlackHttpTransport | None = None,
    publisher: SlackPublisher | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> SlackLiveFixtureResult:
    """Send one exact report twice and require the original provider receipt."""
    workspace_id = _validate_workspace_id(workspace_id)
    channel_id = validate_slack_channel_id(channel_id)
    fixture_id = _validate_fixture_id(fixture_id)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= 60
    ):
        raise ValueError("Slack fixture timeout_seconds must be between 0 and 60")

    live_transport = transport or UrllibSlackHttpTransport()
    validated_publisher = SlackWebApiPublisher(
        bot_token=bot_token,
        timeout_seconds=timeout_seconds,
        transport=live_transport,
    )
    _verify_workspace(
        transport=live_transport,
        bot_token=bot_token,
        workspace_id=workspace_id,
        timeout_seconds=float(timeout_seconds),
    )
    live_publisher = publisher or validated_publisher
    work_item_id = _fixture_work_item_id(
        workspace_id=workspace_id,
        channel_id=channel_id,
        fixture_id=fixture_id,
    )
    report = build_slack_report(
        work_item_id=work_item_id,
        kind=SlackReportKind.ROOT,
        channel_id=channel_id,
        text=(
            "Codex Dispatcher Slack idempotency live fixture "
            f"{fixture_id}. Expected exactly one visible message. No action required."
        ),
        explicit_secrets=(bot_token,),
    )

    try:
        first = live_publisher.publish(report)
        sleeper(_RETRY_SPACING_SECONDS)
        retry = live_publisher.publish(report)
    except Exception as exc:
        if isinstance(exc, SlackLiveFixtureError):
            raise
        raise SlackLiveFixtureError(
            "Slack live fixture did not return two proven receipts"
        ) from exc
    if (
        first.deduplication_key != report.deduplication_key
        or first.channel_id != channel_id
    ):
        raise SlackLiveFixtureError(
            "Slack first publish returned a conflicting message receipt"
        )
    if first != retry:
        raise SlackLiveFixtureError(
            "Slack exact retry returned a different message receipt"
        )
    return SlackLiveFixtureResult(
        workspace_id=workspace_id,
        channel_id=channel_id,
        fixture_id=fixture_id,
        client_msg_id=slack_client_message_id(report),
        receipt=first,
    )


def verify_slack_workspace(
    *,
    bot_token: str,
    workspace_id: str,
    timeout_seconds: float,
    transport: SlackHttpTransport | None = None,
) -> None:
    """Read only ``auth.test`` and require one exact installed bot workspace."""
    workspace_id = _validate_workspace_id(workspace_id)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= 60
    ):
        raise ValueError("Slack fixture timeout_seconds must be between 0 and 60")
    live_transport = transport or UrllibSlackHttpTransport()
    # Constructing the publisher validates the token shape without sending.
    SlackWebApiPublisher(
        bot_token=bot_token,
        timeout_seconds=timeout_seconds,
        transport=live_transport,
    )
    _verify_workspace(
        transport=live_transport,
        bot_token=bot_token,
        workspace_id=workspace_id,
        timeout_seconds=float(timeout_seconds),
    )


def _verify_workspace(
    *,
    transport: SlackHttpTransport,
    bot_token: str,
    workspace_id: str,
    timeout_seconds: float,
) -> None:
    try:
        response = transport(
            method="GET",
            url=_AUTH_TEST_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {bot_token}",
                "User-Agent": "codex-dispatcher-slack-fixture/1",
            },
            body=None,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        raise SlackLiveFixtureError("Slack auth.test could not be proven") from exc
    if not isinstance(response, SlackHttpResponse):
        raise SlackLiveFixtureError("Slack auth.test returned an invalid response")
    if response.status != 200 or response.url != _AUTH_TEST_URL:
        raise SlackLiveFixtureError("Slack auth.test was not a proven success")
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SlackLiveFixtureError("Slack auth.test returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise SlackLiveFixtureError("Slack auth.test rejected the fixture token")
    if payload.get("team_id") != workspace_id:
        raise SlackLiveFixtureError("Slack token belongs to a different workspace")
    bot_id = payload.get("bot_id")
    if not isinstance(bot_id, str) or _BOT_ID_RE.fullmatch(bot_id) is None:
        raise SlackLiveFixtureError("Slack token does not identify an installed bot")


def _validate_workspace_id(value: str) -> str:
    if not isinstance(value, str) or _WORKSPACE_ID_RE.fullmatch(value) is None:
        raise ValueError("Slack workspace_id is invalid")
    return value


def _validate_fixture_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Slack fixture_id is invalid")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("Slack fixture_id is invalid") from exc
    canonical = str(parsed)
    if value != canonical or parsed.version != 4:
        raise ValueError("Slack fixture_id must be a canonical UUIDv4")
    return canonical


def _fixture_work_item_id(
    *, workspace_id: str, channel_id: str, fixture_id: str
) -> str:
    identity = f"{workspace_id}:{channel_id}:{fixture_id}".encode("ascii")
    return "wi_" + sha256(identity).hexdigest()[:24]
