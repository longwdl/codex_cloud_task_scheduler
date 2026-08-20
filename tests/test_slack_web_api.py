from __future__ import annotations

import json
import unittest
from collections.abc import Mapping

from codex_dispatcher.slack_reporting import SlackReport, SlackReportKind, build_slack_report
from codex_dispatcher.slack_web_api import (
    SlackHttpResponse,
    SlackPublishAmbiguous,
    SlackPublishRejected,
    SlackWebApiPublisher,
    slack_client_message_id,
)


TOKEN = "xoxb-1234567890-fixture"
CHANNEL = "C0BR2D0MS8Y"
WORK_ITEM = "wi_" + "a" * 24
TURN = "turn_" + "b" * 32
ROOT_TS = "1700000000.000001"
REPLY_TS = "1700000000.000002"


class _ScriptedTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> SlackHttpResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        scripted = self.responses.pop(0)
        if isinstance(scripted, BaseException):
            raise scripted
        status, response_body, response_url = scripted  # type: ignore[misc]
        return SlackHttpResponse(
            status=status,
            url=url if response_url is None else response_url,
            body=json.dumps(response_body, separators=(",", ":")).encode("utf-8"),
        )


def _root_report(text: str = "hello <@U123> & <!here>") -> SlackReport:
    return build_slack_report(
        work_item_id=WORK_ITEM,
        kind=SlackReportKind.ROOT,
        channel_id=CHANNEL,
        text=text,
    )


def _post_success(*, message_ts: str, thread_ts: str | None = None) -> dict[str, object]:
    message: dict[str, object] = {"ts": message_ts, "text": "fixture"}
    if thread_ts is not None:
        message["thread_ts"] = thread_ts
    return {
        "ok": True,
        "channel": CHANNEL,
        "ts": message_ts,
        "message": message,
    }


class SlackWebApiPublisherTests(unittest.TestCase):
    def test_root_publish_uses_exact_safe_requests_and_returns_receipt(self) -> None:
        report = _root_report()
        permalink = (
            f"https://fixture.slack.com/archives/{CHANNEL}/"
            f"p{ROOT_TS.replace('.', '')}"
        )
        transport = _ScriptedTransport(
            [
                (200, _post_success(message_ts=ROOT_TS), None),
                (
                    200,
                    {"ok": True, "channel": CHANNEL, "permalink": permalink},
                    None,
                ),
            ]
        )
        publisher = SlackWebApiPublisher(
            bot_token=TOKEN,
            timeout_seconds=10,
            transport=transport,
        )

        receipt = publisher.publish(report)

        self.assertEqual(report.deduplication_key, receipt.deduplication_key)
        self.assertEqual(ROOT_TS, receipt.message_ts)
        self.assertEqual(ROOT_TS, receipt.thread_ts)
        self.assertEqual(permalink, receipt.permalink)
        self.assertEqual(2, len(transport.calls))
        post, get_permalink = transport.calls
        self.assertEqual("POST", post["method"])
        self.assertEqual(
            "https://slack.com/api/chat.postMessage",
            post["url"],
        )
        self.assertEqual(f"Bearer {TOKEN}", post["headers"]["Authorization"])
        payload = json.loads(post["body"])
        self.assertEqual(CHANNEL, payload["channel"])
        self.assertEqual("hello &lt;@U123&gt; &amp; &lt;!here&gt;", payload["text"])
        self.assertEqual(slack_client_message_id(report), payload["client_msg_id"])
        self.assertIs(False, payload["mrkdwn"])
        self.assertIs(False, payload["link_names"])
        self.assertIs(False, payload["unfurl_links"])
        self.assertIs(False, payload["unfurl_media"])
        self.assertNotIn("reply_broadcast", payload)
        self.assertNotIn("thread_ts", payload)
        self.assertNotIn(TOKEN, post["body"].decode("utf-8"))
        self.assertEqual("GET", get_permalink["method"])
        self.assertEqual(
            "https://slack.com/api/chat.getPermalink"
            f"?channel={CHANNEL}&message_ts={ROOT_TS}",
            get_permalink["url"],
        )
        self.assertIsNone(get_permalink["body"])

    def test_thread_reply_targets_exact_root_and_validates_permalink(self) -> None:
        report = build_slack_report(
            work_item_id=WORK_ITEM,
            turn_id=TURN,
            kind=SlackReportKind.RESULT,
            channel_id=CHANNEL,
            thread_ts=ROOT_TS,
            text="completed",
        )
        permalink = (
            f"https://fixture.slack.com/archives/{CHANNEL}/"
            f"p{REPLY_TS.replace('.', '')}?thread_ts={ROOT_TS}&cid={CHANNEL}"
        )
        transport = _ScriptedTransport(
            [
                (
                    200,
                    _post_success(message_ts=REPLY_TS, thread_ts=ROOT_TS),
                    None,
                ),
                (
                    200,
                    {"ok": True, "channel": CHANNEL, "permalink": permalink},
                    None,
                ),
            ]
        )

        receipt = SlackWebApiPublisher(
            bot_token=TOKEN,
            timeout_seconds=10,
            transport=transport,
        ).publish(report)

        self.assertEqual(ROOT_TS, receipt.thread_ts)
        self.assertEqual(REPLY_TS, receipt.message_ts)
        payload = json.loads(transport.calls[0]["body"])
        self.assertEqual(ROOT_TS, payload["thread_ts"])
        self.assertIs(False, payload["reply_broadcast"])

    def test_provider_key_is_stable_for_one_outbox_identity(self) -> None:
        first = _root_report("first payload")
        same_identity = _root_report("first payload")
        turn = build_slack_report(
            work_item_id=WORK_ITEM,
            turn_id=TURN,
            kind=SlackReportKind.FAILURE,
            channel_id=CHANNEL,
            thread_ts=ROOT_TS,
            text="failed",
        )

        self.assertEqual(
            slack_client_message_id(first),
            slack_client_message_id(same_identity),
        )
        self.assertNotEqual(
            slack_client_message_id(first),
            slack_client_message_id(turn),
        )

    def test_exact_retry_reuses_client_message_id_and_original_receipt(self) -> None:
        report = _root_report("stable payload")
        permalink = (
            f"https://fixture.slack.com/archives/{CHANNEL}/"
            f"p{ROOT_TS.replace('.', '')}"
        )
        successful_pair = [
            (200, _post_success(message_ts=ROOT_TS), None),
            (
                200,
                {"ok": True, "channel": CHANNEL, "permalink": permalink},
                None,
            ),
        ]
        transport = _ScriptedTransport(successful_pair + successful_pair)
        publisher = SlackWebApiPublisher(
            bot_token=TOKEN,
            timeout_seconds=10,
            transport=transport,
        )

        first = publisher.publish(report)
        retry = publisher.publish(report)

        self.assertEqual(first, retry)
        first_payload = json.loads(transport.calls[0]["body"])
        retry_payload = json.loads(transport.calls[2]["body"])
        self.assertEqual(first_payload, retry_payload)
        self.assertEqual(
            slack_client_message_id(report),
            retry_payload["client_msg_id"],
        )

    def test_ambiguous_post_never_calls_permalink_or_exposes_provider_body(self) -> None:
        for scripted, expected in (
            (TimeoutError(f"network failed {TOKEN}"), "HTTPS request failed"),
            (
                (200, {"ok": False, "error": "internal_error", "detail": TOKEN}, None),
                "internal_error",
            ),
            (
                (302, {"ok": False, "error": "redirected", "detail": TOKEN}, None),
                "not a proven success",
            ),
            (
                (
                    200,
                    _post_success(message_ts=ROOT_TS),
                    "https://attacker.example/capture",
                ),
                "not a proven success",
            ),
        ):
            with self.subTest(expected=expected):
                transport = _ScriptedTransport([scripted])
                publisher = SlackWebApiPublisher(
                    bot_token=TOKEN,
                    timeout_seconds=10,
                    transport=transport,
                )
                with self.assertRaisesRegex(SlackPublishAmbiguous, expected) as caught:
                    publisher.publish(_root_report())
                self.assertEqual(1, len(transport.calls))
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_definitive_api_error_is_redacted_and_rejected(self) -> None:
        transport = _ScriptedTransport(
            [
                (
                    200,
                    {
                        "ok": False,
                        "error": "channel_not_found",
                        "response_metadata": {"messages": [TOKEN]},
                    },
                    None,
                )
            ]
        )
        publisher = SlackWebApiPublisher(
            bot_token=TOKEN,
            timeout_seconds=10,
            transport=transport,
        )

        with self.assertRaisesRegex(
            SlackPublishRejected,
            "channel_not_found",
        ) as caught:
            publisher.publish(_root_report())

        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertEqual(1, len(transport.calls))

    def test_conflicting_success_receipts_fail_closed(self) -> None:
        wrong_channel = _post_success(message_ts=ROOT_TS)
        wrong_channel["channel"] = "C0ATTACKER"
        cases = (
            ([(200, wrong_channel, None)], 1),
            (
                [
                    (200, _post_success(message_ts=ROOT_TS), None),
                    (
                        200,
                        {
                            "ok": True,
                            "channel": CHANNEL,
                            "permalink": (
                                "https://attacker.example/archives/"
                                f"{CHANNEL}/p{ROOT_TS.replace('.', '')}"
                            ),
                        },
                        None,
                    ),
                ],
                2,
            ),
        )
        for responses, expected_calls in cases:
            with self.subTest(expected_calls=expected_calls):
                transport = _ScriptedTransport(responses)
                publisher = SlackWebApiPublisher(
                    bot_token=TOKEN,
                    timeout_seconds=10,
                    transport=transport,
                )
                with self.assertRaises(SlackPublishAmbiguous):
                    publisher.publish(_root_report())
                self.assertEqual(expected_calls, len(transport.calls))

    def test_permalink_failure_after_confirmed_post_is_ambiguous(self) -> None:
        transport = _ScriptedTransport(
            [
                (200, _post_success(message_ts=ROOT_TS), None),
                (200, {"ok": False, "error": "message_not_found"}, None),
            ]
        )
        publisher = SlackWebApiPublisher(
            bot_token=TOKEN,
            timeout_seconds=10,
            transport=transport,
        )

        with self.assertRaisesRegex(SlackPublishAmbiguous, "permalink"):
            publisher.publish(_root_report())

        self.assertEqual(2, len(transport.calls))

    def test_rejects_non_bot_tokens_and_unbounded_timeouts_without_http(self) -> None:
        transport = _ScriptedTransport([])
        with self.assertRaisesRegex(ValueError, "bot token"):
            SlackWebApiPublisher(
                bot_token="xoxp-not-a-bot-token",
                timeout_seconds=10,
                transport=transport,
            )
        with self.assertRaisesRegex(ValueError, "between 0 and 60"):
            SlackWebApiPublisher(
                bot_token=TOKEN,
                timeout_seconds=61,
                transport=transport,
            )
        self.assertEqual([], transport.calls)


if __name__ == "__main__":
    unittest.main()
