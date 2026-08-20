from __future__ import annotations

import json
import unittest

from codex_dispatcher.slack_live_fixture import (
    SlackLiveFixtureError,
    run_slack_idempotency_fixture,
)
from codex_dispatcher.slack_reporting import SlackDeliveryReceipt, SlackReport
from codex_dispatcher.slack_web_api import SlackHttpResponse


TOKEN = "xoxb-1234567890-fixture"
WORKSPACE = "T0BQ60N9WH4"
CHANNEL = "C0BR2D0MS8Y"
FIXTURE_ID = "067190d4-6948-4b75-8c90-d39c964e4f0b"
MESSAGE_TS = "1700000000.000001"


class _AuthTransport:
    def __init__(self, *, workspace_id: str = WORKSPACE) -> None:
        self.workspace_id = workspace_id
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
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
        return SlackHttpResponse(
            status=200,
            url=url,
            body=json.dumps(
                {
                    "ok": True,
                    "team_id": self.workspace_id,
                    "bot_id": "B0123456789",
                }
            ).encode("utf-8"),
        )


class _Publisher:
    def __init__(self, receipts: list[SlackDeliveryReceipt | BaseException]) -> None:
        self.receipts = receipts
        self.reports: list[SlackReport] = []

    def publish(self, report: SlackReport) -> SlackDeliveryReceipt:
        self.reports.append(report)
        result = self.receipts.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _receipt(message_ts: str = MESSAGE_TS) -> SlackDeliveryReceipt:
    work_item_id = "wi_a905c73df787c9bcf7ca054e"
    deduplication_key = f"slack:{work_item_id}:root"
    return SlackDeliveryReceipt(
        deduplication_key=deduplication_key,
        channel_id=CHANNEL,
        message_ts=message_ts,
        thread_ts=message_ts,
        permalink=(
            f"https://fixture.slack.com/archives/{CHANNEL}/"
            f"p{message_ts.replace('.', '')}"
        ),
    )


class SlackLiveFixtureTests(unittest.TestCase):
    def test_exact_retry_requires_same_report_and_receipt(self) -> None:
        receipt = _receipt()
        publisher = _Publisher([receipt, receipt])
        transport = _AuthTransport()
        sleeps: list[float] = []

        result = run_slack_idempotency_fixture(
            bot_token=TOKEN,
            workspace_id=WORKSPACE,
            channel_id=CHANNEL,
            fixture_id=FIXTURE_ID,
            timeout_seconds=10,
            transport=transport,
            publisher=publisher,
            sleeper=sleeps.append,
        )

        self.assertEqual(receipt, result.receipt)
        self.assertEqual(WORKSPACE, result.workspace_id)
        self.assertEqual(FIXTURE_ID, result.fixture_id)
        self.assertEqual(2, len(publisher.reports))
        self.assertEqual(publisher.reports[0], publisher.reports[1])
        self.assertEqual(
            "Codex Dispatcher Slack idempotency live fixture "
            f"{FIXTURE_ID}. Expected exactly one visible message. No action required.",
            publisher.reports[0].text,
        )
        self.assertEqual([1.1], sleeps)
        self.assertEqual(1, len(transport.calls))
        auth = transport.calls[0]
        self.assertEqual("GET", auth["method"])
        self.assertEqual("https://slack.com/api/auth.test", auth["url"])
        self.assertEqual(f"Bearer {TOKEN}", auth["headers"]["Authorization"])
        self.assertIsNone(auth["body"])

    def test_workspace_mismatch_fails_before_publish(self) -> None:
        publisher = _Publisher([_receipt(), _receipt()])

        with self.assertRaisesRegex(
            SlackLiveFixtureError, "different workspace"
        ):
            run_slack_idempotency_fixture(
                bot_token=TOKEN,
                workspace_id=WORKSPACE,
                channel_id=CHANNEL,
                fixture_id=FIXTURE_ID,
                timeout_seconds=10,
                transport=_AuthTransport(workspace_id="T0000000000"),
                publisher=publisher,
                sleeper=lambda _: None,
            )

        self.assertEqual([], publisher.reports)

    def test_different_retry_receipt_fails_closed(self) -> None:
        publisher = _Publisher([_receipt(), _receipt("1700000000.000002")])

        with self.assertRaisesRegex(
            SlackLiveFixtureError, "different message receipt"
        ):
            run_slack_idempotency_fixture(
                bot_token=TOKEN,
                workspace_id=WORKSPACE,
                channel_id=CHANNEL,
                fixture_id=FIXTURE_ID,
                timeout_seconds=10,
                transport=_AuthTransport(),
                publisher=publisher,
                sleeper=lambda _: None,
            )

        self.assertEqual(2, len(publisher.reports))

    def test_first_publish_failure_is_not_retried(self) -> None:
        publisher = _Publisher([RuntimeError(TOKEN)])

        with self.assertRaisesRegex(
            SlackLiveFixtureError, "two proven receipts"
        ) as caught:
            run_slack_idempotency_fixture(
                bot_token=TOKEN,
                workspace_id=WORKSPACE,
                channel_id=CHANNEL,
                fixture_id=FIXTURE_ID,
                timeout_seconds=10,
                transport=_AuthTransport(),
                publisher=publisher,
                sleeper=lambda _: None,
            )

        self.assertEqual(1, len(publisher.reports))
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_rejects_noncanonical_fixture_id_without_network(self) -> None:
        transport = _AuthTransport()
        with self.assertRaisesRegex(ValueError, "canonical UUIDv4"):
            run_slack_idempotency_fixture(
                bot_token=TOKEN,
                workspace_id=WORKSPACE,
                channel_id=CHANNEL,
                fixture_id=FIXTURE_ID.upper(),
                timeout_seconds=10,
                transport=transport,
                publisher=_Publisher([_receipt(), _receipt()]),
            )
        self.assertEqual([], transport.calls)

    def test_rejects_non_bot_token_without_network(self) -> None:
        transport = _AuthTransport()
        with self.assertRaisesRegex(ValueError, "recognized Slack bot token"):
            run_slack_idempotency_fixture(
                bot_token="xoxp-not-a-bot-token",
                workspace_id=WORKSPACE,
                channel_id=CHANNEL,
                fixture_id=FIXTURE_ID,
                timeout_seconds=10,
                transport=transport,
                publisher=_Publisher([_receipt(), _receipt()]),
            )
        self.assertEqual([], transport.calls)


if __name__ == "__main__":
    unittest.main()
