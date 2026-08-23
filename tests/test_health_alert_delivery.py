from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_dispatcher.health_alert_delivery import (
    HealthAlertDeliveryCoordinator,
    health_alert_fingerprint,
)
from codex_dispatcher.lifecycle_health import LifecycleAlert
from codex_dispatcher.slack_reporting import (
    SlackDeliveryReceipt,
    SlackOutboundMessage,
)
from codex_dispatcher.state_store import StateStore


CHANNEL = "C0BR2D0MS8Y"


class _IdempotentPublisher:
    def __init__(self, *, lose_receipt_once: bool = False) -> None:
        self.lose_receipt_once = lose_receipt_once
        self.calls: list[SlackOutboundMessage] = []
        self.messages: dict[
            str, tuple[SlackOutboundMessage, SlackDeliveryReceipt]
        ] = {}

    def publish(self, report: SlackOutboundMessage) -> SlackDeliveryReceipt:
        self.calls.append(report)
        existing = self.messages.get(report.deduplication_key)
        if existing is None:
            message_ts = f"1700000000.{len(self.messages) + 1:06d}"
            thread_ts = report.thread_ts or message_ts
            query = (
                ""
                if report.thread_ts is None
                else f"?thread_ts={thread_ts}&cid={report.channel_id}"
            )
            receipt = SlackDeliveryReceipt(
                deduplication_key=report.deduplication_key,
                channel_id=report.channel_id,
                message_ts=message_ts,
                thread_ts=thread_ts,
                permalink=(
                    f"https://fixture.slack.com/archives/{report.channel_id}/"
                    f"p{message_ts.replace('.', '')}{query}"
                ),
            )
            self.messages[report.deduplication_key] = (report, receipt)
        else:
            original, receipt = existing
            if original != report:
                raise AssertionError("same key was reused with another payload")
        if self.lose_receipt_once:
            self.lose_receipt_once = False
            raise RuntimeError("lost Slack receipt")
        return receipt


class HealthAlertDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "state.db")
        self.store.migrate()

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def _coordinator(self, publisher: _IdempotentPublisher):
        return HealthAlertDeliveryCoordinator(
            store=self.store,
            publisher=publisher,
            channel_id=CHANNEL,
        )

    def test_repeated_stable_alert_delivers_once_and_age_does_not_change_identity(self) -> None:
        publisher = _IdempotentPublisher()
        coordinator = self._coordinator(publisher)
        first_alerts = (
            LifecycleAlert(
                "active_turn_too_long",
                work_item_id="wi_" + "a" * 24,
                repository="owner/repo",
                issue_number=1,
                age_seconds=4000,
            ),
        )
        aged_alerts = (
            LifecycleAlert(
                "active_turn_too_long",
                work_item_id="wi_" + "a" * 24,
                repository="owner/repo",
                issue_number=1,
                age_seconds=4900,
            ),
        )

        opened = coordinator.reconcile(
            first_alerts,
            checked_at="2026-08-23T01:00:00+00:00",
            alerts_truncated=False,
        )
        unchanged = coordinator.reconcile(
            aged_alerts,
            checked_at="2026-08-23T01:15:00+00:00",
            alerts_truncated=False,
        )

        self.assertEqual("alert_opened", opened.action)
        self.assertEqual("unchanged", unchanged.action)
        self.assertEqual(1, len(publisher.calls))
        self.assertEqual(
            health_alert_fingerprint(first_alerts),
            health_alert_fingerprint(aged_alerts),
        )

    def test_changed_alert_posts_update_then_recovery_replies_and_clears(self) -> None:
        publisher = _IdempotentPublisher()
        coordinator = self._coordinator(publisher)
        coordinator.reconcile(
            (LifecycleAlert("systemd_service_failed", unit="service-a"),),
            checked_at="2026-08-23T01:00:00+00:00",
            alerts_truncated=False,
        )
        updated = coordinator.reconcile(
            (LifecycleAlert("systemd_timer_not_active", unit="timer-a"),),
            checked_at="2026-08-23T01:15:00+00:00",
            alerts_truncated=False,
        )
        recovered = coordinator.reconcile(
            (),
            checked_at="2026-08-23T01:30:00+00:00",
            alerts_truncated=False,
        )
        healthy = coordinator.reconcile(
            (),
            checked_at="2026-08-23T01:45:00+00:00",
            alerts_truncated=False,
        )

        self.assertEqual("alert_updated", updated.action)
        self.assertEqual("recovered", recovered.action)
        self.assertEqual("healthy", healthy.action)
        self.assertEqual(3, len(publisher.messages))
        recovery_report = publisher.calls[-1]
        self.assertIsNotNone(recovery_report.thread_ts)
        self.assertIsNone(self.store.get_active_health_alert())

    def test_lost_alert_and_recovery_receipts_retry_exact_messages(self) -> None:
        publisher = _IdempotentPublisher(lose_receipt_once=True)
        coordinator = self._coordinator(publisher)
        alerts = (LifecycleAlert("database_foreign_key_violation"),)
        with self.assertRaisesRegex(RuntimeError, "lost Slack receipt"):
            coordinator.reconcile(
                alerts,
                checked_at="2026-08-23T01:00:00+00:00",
                alerts_truncated=False,
            )
        opened = coordinator.reconcile(
            alerts,
            checked_at="2026-08-23T01:15:00+00:00",
            alerts_truncated=False,
        )
        self.assertEqual("unchanged", opened.action)
        self.assertEqual(1, len(publisher.messages))

        publisher.lose_receipt_once = True
        with self.assertRaisesRegex(RuntimeError, "lost Slack receipt"):
            coordinator.reconcile(
                (),
                checked_at="2026-08-23T01:30:00+00:00",
                alerts_truncated=False,
            )
        recovered = coordinator.reconcile(
            (),
            checked_at="2026-08-23T01:45:00+00:00",
            alerts_truncated=False,
        )
        self.assertEqual("recovered", recovered.action)
        self.assertEqual(2, len(publisher.messages))
        self.assertEqual(4, len(publisher.calls))


if __name__ == "__main__":
    unittest.main()
