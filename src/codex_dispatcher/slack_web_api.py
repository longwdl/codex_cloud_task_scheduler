"""Strict outbound-only Slack Web API publisher.

The adapter deliberately exposes no Slack read, event, interaction, or command
surface.  It uses a deterministic ``client_msg_id`` for provider deduplication,
but callers must not enable it until that behavior has been proven for the
target Slack app/workspace by a controlled live fixture.
"""

from __future__ import annotations

import json
import re
import ssl
from dataclasses import dataclass
from typing import Mapping, Protocol
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import NAMESPACE_URL, uuid5

from codex_dispatcher.slack_reporting import (
    SlackDeliveryReceipt,
    SlackOutboundMessage,
    SlackReport,
    validate_slack_permalink,
)


_API_ORIGIN = "https://slack.com"
_POST_MESSAGE_URL = f"{_API_ORIGIN}/api/chat.postMessage"
_GET_PERMALINK_URL = f"{_API_ORIGIN}/api/chat.getPermalink"
_MAX_RESPONSE_BYTES = 64 * 1024
_ERROR_CODE_RE = re.compile(r"[a-z0-9_]{1,64}")
_MESSAGE_TS_RE = re.compile(r"[0-9]{1,20}\.[0-9]{1,20}")
_CLIENT_MESSAGE_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "urn:codex-dispatcher:slack-delivery:v1",
)
_AMBIGUOUS_POST_ERRORS = frozenset(
    {
        "duplicate_channel_not_found",
        "duplicate_message_not_found",
        "fatal_error",
        "internal_error",
        "request_timeout",
        "service_unavailable",
    }
)


def _same_receipt_permalink(
    receipt: SlackDeliveryReceipt, actual_permalink: object
) -> bool:
    """Allow Slack's redundant thread query for a root that later gained replies."""
    if actual_permalink == receipt.permalink:
        return True
    if (
        not isinstance(actual_permalink, str)
        or receipt.thread_ts != receipt.message_ts
    ):
        return False
    try:
        validate_slack_permalink(
            actual_permalink,
            channel_id=receipt.channel_id,
            message_ts=receipt.message_ts,
        )
    except ValueError:
        return False
    expected = urlsplit(receipt.permalink)
    actual = urlsplit(actual_permalink)
    if (
        (actual.scheme, actual.netloc, actual.path)
        != (expected.scheme, expected.netloc, expected.path)
        or expected.query
    ):
        return False
    return dict(parse_qsl(actual.query, keep_blank_values=True)) == {
        "thread_ts": receipt.message_ts,
        "cid": receipt.channel_id,
    }


class SlackWebApiError(RuntimeError):
    """Base class for redacted Slack publisher failures."""


class SlackPublishRejected(SlackWebApiError):
    """Slack definitively rejected a bounded outbound request."""


class SlackPublishAmbiguous(SlackWebApiError):
    """Slack may have accepted a write, but no complete receipt was proven."""


@dataclass(frozen=True, slots=True)
class SlackHttpResponse:
    status: int
    url: str
    body: bytes


class SlackHttpTransport(Protocol):
    """Execute one exact HTTPS request without applying automatic retries."""

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> SlackHttpResponse: ...


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


class UrllibSlackHttpTransport:
    """Standard-library HTTPS transport pinned to direct TLS and no redirects."""

    def __init__(self) -> None:
        self._opener = build_opener(
            ProxyHandler({}),
            _RejectRedirects(),
            HTTPSHandler(context=ssl.create_default_context()),
        )

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> SlackHttpResponse:
        request = Request(
            url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            response = self._opener.open(request, timeout=timeout_seconds)
        except HTTPError as exc:
            response = exc
        except OSError as exc:
            raise SlackPublishAmbiguous(
                "Slack HTTPS request outcome is ambiguous"
            ) from exc
        try:
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(payload) > _MAX_RESPONSE_BYTES:
                raise SlackPublishAmbiguous("Slack response exceeded the size limit")
            return SlackHttpResponse(
                status=int(response.status),
                url=response.geturl(),
                body=payload,
            )
        finally:
            response.close()


class SlackWebApiPublisher:
    """Publish one report through ``chat.postMessage`` and get its permalink."""

    def __init__(
        self,
        *,
        bot_token: str,
        timeout_seconds: float,
        transport: SlackHttpTransport | None = None,
    ) -> None:
        self._bot_token = _validate_bot_token(bot_token)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 60
        ):
            raise ValueError("Slack timeout_seconds must be between 0 and 60")
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport or UrllibSlackHttpTransport()

    def publish(self, report: SlackReport | SlackOutboundMessage) -> SlackDeliveryReceipt:
        if not isinstance(report, (SlackReport, SlackOutboundMessage)):
            raise TypeError("report must be a validated Slack outbound message")
        message = self._post_message(report)
        message_ts = message.get("ts")
        channel_id = message.get("channel")
        message_body = message.get("message")
        if (
            channel_id != report.channel_id
            or not isinstance(message_ts, str)
            or _MESSAGE_TS_RE.fullmatch(message_ts) is None
            or not isinstance(message_body, dict)
            or message_body.get("ts") != message_ts
        ):
            raise SlackPublishAmbiguous(
                "Slack chat.postMessage returned a conflicting receipt"
            )
        expected_thread = report.thread_ts
        response_thread = message_body.get("thread_ts")
        if expected_thread is None:
            if response_thread is not None:
                raise SlackPublishAmbiguous(
                    "Slack root message unexpectedly targeted a thread"
                )
            thread_ts = message_ts
        else:
            if response_thread != expected_thread:
                raise SlackPublishAmbiguous(
                    "Slack reply receipt conflicts with its target thread"
                )
            thread_ts = expected_thread

        permalink_result = self._get_permalink(
            channel_id=report.channel_id,
            message_ts=message_ts,
        )
        if permalink_result.get("channel") != report.channel_id:
            raise SlackPublishAmbiguous(
                "Slack chat.getPermalink returned a conflicting channel"
            )
        permalink = permalink_result.get("permalink")
        if not isinstance(permalink, str):
            raise SlackPublishAmbiguous(
                "Slack chat.getPermalink returned an invalid receipt"
            )
        try:
            return SlackDeliveryReceipt(
                deduplication_key=report.deduplication_key,
                channel_id=report.channel_id,
                message_ts=message_ts,
                thread_ts=thread_ts,
                permalink=permalink,
            )
        except ValueError as exc:
            raise SlackPublishAmbiguous(
                "Slack permalink conflicts with the posted message"
            ) from exc

    def verify_receipt(self, receipt: SlackDeliveryReceipt) -> SlackDeliveryReceipt:
        """Read back one exact delivered message without creating or changing Slack state."""
        if not isinstance(receipt, SlackDeliveryReceipt):
            raise TypeError("receipt must be a SlackDeliveryReceipt")
        result = self._get_permalink(
            channel_id=receipt.channel_id,
            message_ts=receipt.message_ts,
        )
        if result.get("channel") != receipt.channel_id or not _same_receipt_permalink(
            receipt, result.get("permalink")
        ):
            raise SlackPublishAmbiguous(
                "Slack receipt read-back conflicts with durable state"
            )
        return receipt

    def _post_message(
        self, report: SlackReport | SlackOutboundMessage
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "channel": report.channel_id,
            "text": _escape_slack_control_sequences(report.text),
            "client_msg_id": slack_client_message_id(report),
            "mrkdwn": False,
            "link_names": False,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if report.thread_ts is not None:
            payload["thread_ts"] = report.thread_ts
            payload["reply_broadcast"] = False
        result = self._request_json(
            method="POST",
            url=_POST_MESSAGE_URL,
            body=json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )
        return result

    def _get_permalink(
        self,
        *,
        channel_id: str,
        message_ts: str,
    ) -> dict[str, object]:
        query = urlencode(
            {
                "channel": channel_id,
                "message_ts": message_ts,
            }
        )
        try:
            return self._request_json(
                method="GET",
                url=f"{_GET_PERMALINK_URL}?{query}",
                body=None,
            )
        except SlackWebApiError as exc:
            raise SlackPublishAmbiguous(
                "Slack message was posted but its permalink receipt is unavailable"
            ) from exc

    def _request_json(
        self,
        *,
        method: str,
        url: str,
        body: bytes | None,
    ) -> dict[str, object]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._bot_token}",
            "User-Agent": "codex-dispatcher-slack/1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            response = self._transport(
                method=method,
                url=url,
                headers=headers,
                body=body,
                timeout_seconds=self._timeout_seconds,
            )
        except SlackWebApiError:
            raise
        except Exception as exc:
            raise SlackPublishAmbiguous("Slack HTTPS request failed") from exc
        if not isinstance(response, SlackHttpResponse):
            raise SlackPublishAmbiguous("Slack HTTPS transport returned an invalid response")
        expected_url = url
        if response.url != expected_url or response.status != 200:
            raise SlackPublishAmbiguous(
                "Slack HTTPS response was not a proven success"
            )
        try:
            decoded = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SlackPublishAmbiguous(
                "Slack returned an invalid JSON response"
            ) from exc
        if not isinstance(decoded, dict) or type(decoded.get("ok")) is not bool:
            raise SlackPublishAmbiguous("Slack returned an invalid API response")
        if decoded["ok"] is not True:
            code = _safe_error_code(decoded.get("error"))
            if code in _AMBIGUOUS_POST_ERRORS:
                raise SlackPublishAmbiguous(
                    f"Slack API outcome is ambiguous: {code}"
                )
            raise SlackPublishRejected(f"Slack API rejected request: {code}")
        return decoded


def slack_client_message_id(report: SlackReport | SlackOutboundMessage) -> str:
    """Return the stable provider key for one validated outbox identity."""
    if not isinstance(report, (SlackReport, SlackOutboundMessage)):
        raise TypeError("report must be a validated Slack outbound message")
    return str(uuid5(_CLIENT_MESSAGE_NAMESPACE, report.deduplication_key))


def _validate_bot_token(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("xoxb-")
        or not 16 <= len(value) <= 512
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError("a recognized Slack bot token is required")
    return value


def _safe_error_code(value: object) -> str:
    if isinstance(value, str) and _ERROR_CODE_RE.fullmatch(value) is not None:
        return value
    return "unknown_error"


def _escape_slack_control_sequences(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
