from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codex_dispatcher.slack_delivery import (
    SlackDeliveryCoordinator,
    SlackDeliveryRejected,
)
from codex_dispatcher.slack_reporting import (
    SlackDeliveryReceipt,
    SlackDeliveryState,
    SlackReport,
    SlackReportKind,
    build_slack_report,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.trackers.base import TaskState
from codex_dispatcher.work_items import Turn, TurnState, WorkItem, WorkItemState
from tests.test_ssh_dispatch_planning import claimed_task


CHANNEL = "C0BR2D0MS8Y"
TURN_ID = "turn_" + "7" * 32


class _IdempotentSlackPublisher:
    def __init__(self, *, interrupt_kind_once: SlackReportKind | None = None) -> None:
        self.interrupt_kind_once = interrupt_kind_once
        self.calls: list[SlackReport] = []
        self.messages: dict[str, tuple[SlackReport, SlackDeliveryReceipt]] = {}

    def publish(self, report: SlackReport) -> SlackDeliveryReceipt:
        self.calls.append(report)
        existing = self.messages.get(report.deduplication_key)
        if existing is None:
            message_index = len(self.messages) + 1
            message_ts = f"1700000000.{message_index:06d}"
            thread_ts = report.thread_ts or message_ts
            query = (
                ""
                if report.kind is SlackReportKind.ROOT
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
                raise AssertionError("same Slack key was reused with another payload")
        if self.interrupt_kind_once is report.kind:
            self.interrupt_kind_once = None
            raise RuntimeError("fixture lost Slack receipt")
        return receipt


class SlackDeliveryCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "state.db")
        self.store.migrate()
        item = WorkItem.new(
            repository="owner/repo",
            issue_number=42,
            issue_node_id="I_kwDOFixture42",
            base_branch="main",
            base_sha="a" * 40,
            at="2026-08-18T00:00:00Z",
        )
        self.store.create_work_item(item)
        self.store.update_work_item_state(item.work_item_id, WorkItemState.PREPARING)
        self.item = self.store.update_work_item_state(
            item.work_item_id,
            WorkItemState.READY,
        )
        self.task = replace(
            claimed_task(),
            state=TaskState.DISPATCHING,
            labels=("agent:dispatching", "exec:ssh-cli", "priority:p1"),
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def _finalize_review(self) -> tuple[WorkItem, Turn]:
        running, turn = self.store.begin_turn(
            self.item.work_item_id,
            turn_id=TURN_ID,
            issue_revision="revision-1",
            prompt_sha256="b" * 64,
            input_head_sha="a" * 40,
        )
        self.assertEqual(WorkItemState.RUNNING, running.state)
        self.store.update_turn_state(turn.turn_id, TurnState.STARTING)
        self.store.record_turn_result(
            turn.turn_id,
            output_sha256="c" * 64,
            output_head_sha="b" * 40,
            result_status="completed",
            result_summary="Completed token=secret-value",
        )
        item, turn = self.store.finalize_turn(
            turn.turn_id,
            turn_state=TurnState.FINISHED,
            work_item_state=WorkItemState.REVIEW,
        )
        self.store.record_published_sha(
            item.work_item_id,
            previous_sha="a" * 40,
            head_sha="b" * 40,
        )
        item = self.store.bind_draft_pr(item.work_item_id, 9)
        return item, turn

    def test_lost_root_receipt_reuses_one_remote_message_and_binds_atomically(self) -> None:
        publisher = _IdempotentSlackPublisher(
            interrupt_kind_once=SlackReportKind.ROOT
        )
        coordinator = SlackDeliveryCoordinator(
            store=self.store,
            publisher=publisher,
            channel_id=CHANNEL,
        )

        with self.assertRaisesRegex(RuntimeError, "lost Slack receipt"):
            coordinator.ensure_root(self.task, work_item=self.item)

        interrupted = self.store.get_work_item(self.item.work_item_id)
        assert interrupted is not None
        self.assertIsNone(interrupted.slack_thread_ts)
        prepared = self.store.get_slack_delivery(
            f"slack:{self.item.work_item_id}:root"
        )
        assert prepared is not None
        self.assertIs(SlackDeliveryState.PREPARED, prepared.state)

        recovered = coordinator.ensure_root(self.task, work_item=interrupted)

        self.assertEqual("1700000000.000001", recovered.work_item.slack_thread_ts)
        self.assertEqual(1, len(publisher.messages))
        self.assertEqual(2, len(publisher.calls))
        delivered = self.store.get_slack_delivery(prepared.deduplication_key)
        assert delivered is not None
        self.assertIs(SlackDeliveryState.DELIVERED, delivered.state)

    def test_terminal_report_is_redacted_linked_and_not_republished(self) -> None:
        publisher = _IdempotentSlackPublisher()
        coordinator = SlackDeliveryCoordinator(
            store=self.store,
            publisher=publisher,
            channel_id=CHANNEL,
        )
        root = coordinator.ensure_root(self.task, work_item=self.item)
        item, turn = self._finalize_review()

        first = coordinator.reconcile_terminal(
            self.task,
            work_item=item,
            desired_task_state=TaskState.REVIEW,
            turn=turn,
        )
        second = coordinator.reconcile_terminal(
            self.task,
            work_item=first.work_item,
            desired_task_state=TaskState.REVIEW,
            turn=turn,
        )

        self.assertEqual(root.root_permalink, first.root_permalink)
        self.assertEqual(first, second)
        self.assertEqual(2, len(publisher.messages))
        self.assertEqual(2, len(publisher.calls))
        result_report = next(
            report
            for report in publisher.calls
            if report.kind is SlackReportKind.RESULT
        )
        self.assertIn("token=[REDACTED]", result_report.text)
        self.assertIn("https://github.com/owner/repo/pull/9", result_report.text)
        self.assertNotIn("secret-value", result_report.text)

    def test_lost_terminal_receipt_retries_without_another_thread_or_message(self) -> None:
        publisher = _IdempotentSlackPublisher(
            interrupt_kind_once=SlackReportKind.RESULT
        )
        coordinator = SlackDeliveryCoordinator(
            store=self.store,
            publisher=publisher,
            channel_id=CHANNEL,
        )
        coordinator.ensure_root(self.task, work_item=self.item)
        item, turn = self._finalize_review()

        with self.assertRaisesRegex(RuntimeError, "lost Slack receipt"):
            coordinator.reconcile_terminal(
                self.task,
                work_item=item,
                desired_task_state=TaskState.REVIEW,
                turn=turn,
            )
        recovered = coordinator.reconcile_terminal(
            self.task,
            work_item=item,
            desired_task_state=TaskState.REVIEW,
            turn=turn,
        )

        self.assertIsNotNone(recovered.terminal_delivery)
        self.assertEqual(2, len(publisher.messages))
        self.assertEqual(3, len(publisher.calls))

    def test_existing_untracked_thread_and_payload_key_conflict_fail_closed(self) -> None:
        publisher = _IdempotentSlackPublisher()
        coordinator = SlackDeliveryCoordinator(
            store=self.store,
            publisher=publisher,
            channel_id=CHANNEL,
        )
        bound = self.store.bind_slack_thread(
            self.item.work_item_id,
            channel_id=CHANNEL,
            thread_ts="1700000000.000001",
        )
        with self.assertRaisesRegex(SlackDeliveryRejected, "no durable root"):
            coordinator.ensure_root(self.task, work_item=bound)
        self.assertEqual([], publisher.calls)

        separate = WorkItem.new(
            repository="owner/repo",
            issue_number=43,
            issue_node_id="I_kwDOFixture43",
            base_branch="main",
            base_sha="a" * 40,
        )
        self.store.create_work_item(separate)
        first = build_slack_report(
            work_item_id=separate.work_item_id,
            kind=SlackReportKind.ROOT,
            channel_id=CHANNEL,
            text="first",
        )
        second = replace(first, text="different")
        prepared = self.store.prepare_slack_delivery(first)
        wrong_channel_receipt = SlackDeliveryReceipt(
            prepared.deduplication_key,
            "G0BR2D0MS8Y",
            "1700000000.000009",
            "1700000000.000009",
            (
                "https://fixture.slack.com/archives/G0BR2D0MS8Y/"
                "p1700000000000009"
            ),
        )
        with self.assertRaisesRegex(ValueError, "channel conflicts"):
            self.store.complete_slack_delivery(
                prepared.deduplication_key,
                wrong_channel_receipt,
            )
        unchanged = self.store.get_slack_delivery(prepared.deduplication_key)
        assert unchanged is not None
        self.assertIs(SlackDeliveryState.PREPARED, unchanged.state)
        unbound = self.store.get_work_item(separate.work_item_id)
        assert unbound is not None
        self.assertIsNone(unbound.slack_thread_ts)
        with self.assertRaisesRegex(ValueError, "different payload"):
            self.store.prepare_slack_delivery(second)


if __name__ == "__main__":
    unittest.main()
