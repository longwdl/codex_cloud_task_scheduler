from __future__ import annotations

import unittest

from codex_dispatcher.slack_reporting import (
    MAX_SLACK_TEXT_CHARS,
    SlackDeliveryReceipt,
    SlackReportKind,
    build_slack_report,
)


WORK_ITEM = "wi_" + "a" * 24
TURN = "turn_" + "b" * 32


class SlackReportingTests(unittest.TestCase):
    def test_root_and_turn_reports_have_deterministic_distinct_keys(self) -> None:
        root = build_slack_report(
            work_item_id=WORK_ITEM,
            kind=SlackReportKind.ROOT,
            channel_id="C0BR2D0MS8Y",
            text="Issue 42",
        )
        result = build_slack_report(
            work_item_id=WORK_ITEM,
            turn_id=TURN,
            kind=SlackReportKind.RESULT,
            channel_id="C0BR2D0MS8Y",
            thread_ts="1234567890.123456",
            text="Completed",
        )
        self.assertEqual(f"slack:{WORK_ITEM}:root", root.deduplication_key)
        self.assertEqual(
            f"slack:{WORK_ITEM}:{TURN}:result", result.deduplication_key
        )
        self.assertNotEqual(root.deduplication_key, result.deduplication_key)

    def test_output_is_redacted_and_bounded(self) -> None:
        report = build_slack_report(
            work_item_id=WORK_ITEM,
            turn_id=TURN,
            kind=SlackReportKind.FAILURE,
            channel_id="C0BR2D0MS8Y",
            thread_ts="1234567890.123456",
            text="token=secret-value\n" + "x" * (MAX_SLACK_TEXT_CHARS + 100),
        )
        self.assertIn("token=[REDACTED]", report.text)
        self.assertIn("[TRUNCATED]", report.text)
        self.assertLessEqual(len(report.text), MAX_SLACK_TEXT_CHARS)

    def test_root_cannot_target_thread_and_turn_report_requires_one(self) -> None:
        with self.assertRaises(ValueError):
            build_slack_report(
                work_item_id=WORK_ITEM,
                kind=SlackReportKind.ROOT,
                channel_id="C0BR2D0MS8Y",
                thread_ts="1234567890.123456",
                text="invalid",
            )
        with self.assertRaisesRegex(ValueError, "text"):
            build_slack_report(
                work_item_id=WORK_ITEM,
                kind=SlackReportKind.ROOT,
                channel_id="C0BR2D0MS8Y",
                text="bad\u0001text",
            )
        with self.assertRaises(ValueError):
            build_slack_report(
                work_item_id=WORK_ITEM,
                kind=SlackReportKind.RESULT,
                channel_id="C0BR2D0MS8Y",
                text="invalid",
            )
        with self.assertRaisesRegex(TypeError, "kind"):
            build_slack_report(
                work_item_id=WORK_ITEM,
                kind="root",  # type: ignore[arg-type]
                channel_id="C0BR2D0MS8Y",
                text="invalid",
            )

    def test_receipt_requires_exact_slack_message_permalink(self) -> None:
        with self.assertRaisesRegex(ValueError, "message conflicts"):
            SlackDeliveryReceipt(
                deduplication_key=f"slack:{WORK_ITEM}:root",
                channel_id="C0BR2D0MS8Y",
                message_ts="1700000000.000001",
                thread_ts="1700000000.000001",
                permalink=(
                    "https://fixture.slack.com/archives/C0BR2D0MS8Y/"
                    "p1700000000000002"
                ),
            )
        with self.assertRaisesRegex(ValueError, "Slack permalink"):
            SlackDeliveryReceipt(
                deduplication_key=f"slack:{WORK_ITEM}:root",
                channel_id="C0BR2D0MS8Y",
                message_ts="1700000000.000001",
                thread_ts="1700000000.000001",
                permalink=(
                    "https://attacker.example/archives/C0BR2D0MS8Y/"
                    "p1700000000000001"
                ),
            )


if __name__ == "__main__":
    unittest.main()
