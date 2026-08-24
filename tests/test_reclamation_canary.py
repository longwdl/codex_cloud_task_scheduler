from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from codex_dispatcher.reclamation_canary import (
    build_reclamation_trigger_canary,
    run_local_reclamation_canary,
    run_reclamation_projection_canary,
)
from codex_dispatcher.slack_reporting import (
    SlackDeliveryReceipt,
    SlackOutboundMessage,
)


SYSTEM = "C0BS3LPG43G"
ISSUE = "C0BR2D0MS8Y"
FIXTURE = "rc_" + "a" * 32


def _canonical(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(raw.encode()).hexdigest()


class _Publisher:
    def __init__(self) -> None:
        self.messages: list[SlackOutboundMessage] = []

    def publish(self, report: SlackOutboundMessage) -> SlackDeliveryReceipt:
        self.messages.append(report)
        ts = f"170000000{len(self.messages)}.000001"
        thread_ts = report.thread_ts or ts
        query = (
            ""
            if report.thread_ts is None
            else f"?thread_ts={thread_ts}&cid={report.channel_id}"
        )
        return SlackDeliveryReceipt(
            report.deduplication_key,
            report.channel_id,
            ts,
            thread_ts,
            f"https://fixture.slack.com/archives/{report.channel_id}/"
            f"p{ts.replace('.', '')}{query}",
        )


class _AmbiguousPublisher(_Publisher):
    def __init__(self) -> None:
        super().__init__()
        self.receipts: dict[str, SlackDeliveryReceipt] = {}
        self.fail_once = True

    def publish(self, report: SlackOutboundMessage) -> SlackDeliveryReceipt:
        receipt = self.receipts.get(report.deduplication_key)
        if receipt is None:
            receipt = super().publish(report)
            self.receipts[report.deduplication_key] = receipt
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("ambiguous fixture receipt")
        return receipt


class ReclamationCanaryTests(unittest.TestCase):
    def test_local_plan_preserves_fixture_identity_and_receipt_hash(self) -> None:
        receipt = run_local_reclamation_canary(
            fixture_id=FIXTURE,
            system_channel_id=SYSTEM,
            issue_channel_id=ISSUE,
        )
        evidence = receipt.pop("evidence_sha256")
        self.assertEqual(FIXTURE, receipt["fixture_id"])
        self.assertEqual(_canonical(receipt), evidence)

    def test_four_independent_triggers_share_one_non_authorizing_exact_plan(self) -> None:
        result = build_reclamation_trigger_canary(
            now=datetime(2026, 8, 24, tzinfo=timezone.utc)
        )
        plan = result["plan"]
        self.assertIsInstance(plan, dict)
        assert isinstance(plan, dict)
        self.assertFalse(plan["authorizes_apply"])
        self.assertEqual(1, len(plan["release_targets"]))
        self.assertEqual(1, len(plan["image_targets"]))
        cases = result["threshold_cases"]
        self.assertIsInstance(cases, list)
        assert isinstance(cases, list)
        self.assertEqual(
            {
                "host_available_below_threshold",
                "release_count_above_limit",
                "unreferenced_images_present",
                "reclaimable_bytes_above_threshold",
            },
            {item["case"] for item in cases},
        )
        for item in cases:
            self.assertEqual([item["case"]], item["status"]["trigger_reasons"])
            self.assertEqual(plan["plan_sha256"], item["status"]["plan_sha256"])

    def test_projection_uses_only_system_channel_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            publisher = _Publisher()
            root = Path(raw) / "receipts"
            first = run_reclamation_projection_canary(
                fixture_id=FIXTURE,
                system_channel_id=SYSTEM,
                issue_channel_id=ISSUE,
                publisher=publisher,
                receipt_root=root,
                external_writes=True,
                now=datetime(2026, 8, 24, tzinfo=timezone.utc),
            )
            second = run_reclamation_projection_canary(
                fixture_id=FIXTURE,
                system_channel_id=SYSTEM,
                issue_channel_id=ISSUE,
                publisher=publisher,
                receipt_root=root,
                external_writes=True,
                now=datetime(2026, 8, 24, 1, tzinfo=timezone.utc),
            )

            self.assertEqual(first, second)
            self.assertEqual(2, len(publisher.messages))
            self.assertEqual({SYSTEM}, {item.channel_id for item in publisher.messages})
            self.assertEqual(0, first["issue_channel_writes"])
            self.assertFalse(first["online_state_modified"])
            self.assertFalse(first["authorizes_apply"])
            self.assertEqual(0, first["asset_deletions"])
            self.assertEqual(
                first["alert"]["message_ts"],  # type: ignore[index]
                first["recovery"]["thread_ts"],  # type: ignore[index]
            )
            with self.assertRaisesRegex(RuntimeError, "receipt is invalid"):
                run_reclamation_projection_canary(
                    fixture_id=FIXTURE,
                    system_channel_id=SYSTEM,
                    issue_channel_id=ISSUE,
                    publisher=publisher,
                    receipt_root=root,
                    external_writes=False,
                )

    def test_projection_reuses_durable_intent_after_ambiguous_publish(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            publisher = _AmbiguousPublisher()
            root = Path(raw) / "receipts"
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                run_reclamation_projection_canary(
                    fixture_id=FIXTURE,
                    system_channel_id=SYSTEM,
                    issue_channel_id=ISSUE,
                    publisher=publisher,
                    receipt_root=root,
                    external_writes=True,
                    now=datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            receipt = run_reclamation_projection_canary(
                fixture_id=FIXTURE,
                system_channel_id=SYSTEM,
                issue_channel_id=ISSUE,
                publisher=publisher,
                receipt_root=root,
                external_writes=True,
                now=datetime(2026, 8, 25, tzinfo=timezone.utc),
            )

            self.assertEqual(2, len(publisher.messages))
            self.assertEqual(2, len(publisher.receipts))
            self.assertEqual("passed", receipt["status"])


if __name__ == "__main__":
    unittest.main()
