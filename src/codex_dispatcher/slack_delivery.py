"""Recoverable outbound-only Slack projection for one persistent WorkItem."""

from __future__ import annotations

from dataclasses import dataclass

from codex_dispatcher.slack_reporting import (
    SlackDeliveryRecord,
    SlackDeliveryState,
    SlackPublisher,
    SlackReportKind,
    build_slack_report,
    slack_deduplication_key,
    validate_slack_channel_id,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.trackers.base import TaskState, TrackerTask
from codex_dispatcher.work_items import Turn, TurnState, WorkItem, WorkItemState


class SlackDeliveryRejected(RuntimeError):
    """Raised when Slack delivery state conflicts with durable task identity."""


@dataclass(frozen=True, slots=True)
class SlackProjectionResult:
    work_item: WorkItem
    root_permalink: str
    terminal_delivery: SlackDeliveryRecord | None = None


class SlackDeliveryCoordinator:
    """Publish one root and at most one terminal report for each Turn."""

    def __init__(
        self,
        *,
        store: StateStore,
        publisher: SlackPublisher,
        channel_id: str,
    ) -> None:
        if not isinstance(store, StateStore):
            raise TypeError("store must be a StateStore")
        self._store = store
        self._publisher = publisher
        self._channel_id = validate_slack_channel_id(channel_id)

    def ensure_root(
        self,
        task: TrackerTask,
        *,
        work_item: WorkItem,
    ) -> SlackProjectionResult:
        self._validate_identity(task, work_item)
        report = build_slack_report(
            work_item_id=work_item.work_item_id,
            kind=SlackReportKind.ROOT,
            channel_id=self._channel_id,
            text=self._render_root(work_item),
        )
        existing = self._store.get_slack_delivery(report.deduplication_key)
        if work_item.slack_thread_ts is not None and existing is None:
            raise SlackDeliveryRejected(
                "WorkItem Slack thread has no durable root delivery record"
            )
        delivery = self._store.prepare_slack_delivery(report)
        if delivery.state is SlackDeliveryState.PREPARED:
            receipt = self._publisher.publish(report)
            delivery = self._store.complete_slack_delivery(
                report.deduplication_key,
                receipt,
            )
        refreshed = self._store.get_work_item(work_item.work_item_id)
        assert refreshed is not None
        if (
            delivery.state is not SlackDeliveryState.DELIVERED
            or delivery.message_ts is None
            or delivery.permalink is None
            or refreshed.slack_channel_id != delivery.channel_id
            or refreshed.slack_thread_ts != delivery.message_ts
        ):
            raise SlackDeliveryRejected(
                "Slack root delivery conflicts with the WorkItem binding"
            )
        return SlackProjectionResult(refreshed, delivery.permalink)

    def reconcile_terminal(
        self,
        task: TrackerTask,
        *,
        work_item: WorkItem,
        desired_task_state: TaskState,
        turn: Turn | None,
    ) -> SlackProjectionResult:
        self._validate_terminal(work_item, desired_task_state, turn)
        root = self.ensure_root(task, work_item=work_item)
        if turn is None:
            return root
        kind = {
            TaskState.REVIEW: SlackReportKind.RESULT,
            TaskState.NEEDS_INPUT: SlackReportKind.QUESTION,
            TaskState.BLOCKED: SlackReportKind.FAILURE,
        }[desired_task_state]
        report = build_slack_report(
            work_item_id=root.work_item.work_item_id,
            turn_id=turn.turn_id,
            kind=kind,
            channel_id=self._channel_id,
            thread_ts=root.work_item.slack_thread_ts,
            text=self._render_terminal(root.work_item, turn, desired_task_state),
        )
        delivery = self._store.prepare_slack_delivery(report)
        if delivery.state is SlackDeliveryState.PREPARED:
            receipt = self._publisher.publish(report)
            delivery = self._store.complete_slack_delivery(
                report.deduplication_key,
                receipt,
            )
        if delivery.state is not SlackDeliveryState.DELIVERED:
            raise SlackDeliveryRejected("Slack terminal report was not delivered")
        return SlackProjectionResult(
            root.work_item,
            root.root_permalink,
            delivery,
        )

    @staticmethod
    def _validate_identity(task: TrackerTask, work_item: WorkItem) -> None:
        if not isinstance(task, TrackerTask) or not isinstance(work_item, WorkItem):
            raise TypeError("Slack delivery inputs must use dispatcher DTOs")
        if (
            not task.is_open
            or task.state not in {TaskState.DISPATCHING, TaskState.RUNNING}
            or task.repository != work_item.repository
            or task.issue_number != work_item.issue_number
            or task.task_id != str(work_item.issue_number)
            or task.issue_node_id != work_item.issue_node_id
        ):
            raise SlackDeliveryRejected(
                "Issue identity or state conflicts with the Slack WorkItem"
            )

    @staticmethod
    def _validate_terminal(
        work_item: WorkItem,
        desired_task_state: TaskState,
        turn: Turn | None,
    ) -> None:
        if not isinstance(work_item, WorkItem):
            raise TypeError("work_item must be a WorkItem")
        if not isinstance(desired_task_state, TaskState):
            raise TypeError("desired_task_state must be a TaskState")
        expected_work_item_state = {
            TaskState.REVIEW: WorkItemState.REVIEW,
            TaskState.NEEDS_INPUT: WorkItemState.WAITING_INPUT,
            TaskState.BLOCKED: WorkItemState.BLOCKED,
        }.get(desired_task_state)
        if expected_work_item_state is None or work_item.state is not expected_work_item_state:
            raise SlackDeliveryRejected(
                "Slack terminal state conflicts with the WorkItem"
            )
        if turn is None:
            if desired_task_state is not TaskState.BLOCKED:
                raise SlackDeliveryRejected(
                    "Slack terminal projection requires a recorded Turn"
                )
            return
        if not isinstance(turn, Turn) or turn.work_item_id != work_item.work_item_id:
            raise SlackDeliveryRejected("Slack Turn conflicts with the WorkItem")
        expected_turn = {
            TaskState.REVIEW: (TurnState.FINISHED, "completed"),
            TaskState.NEEDS_INPUT: (TurnState.NEEDS_INPUT, "needs_input"),
            TaskState.BLOCKED: (TurnState.BLOCKED, "blocked"),
        }[desired_task_state]
        if (
            turn.state is not expected_turn[0]
            or turn.result_status != expected_turn[1]
            or turn.result_summary is None
        ):
            raise SlackDeliveryRejected(
                "Slack terminal projection conflicts with the recorded Turn result"
            )

    @staticmethod
    def _render_root(work_item: WorkItem) -> str:
        return "\n".join(
            (
                f"Codex work item `{work_item.work_item_id}`",
                "",
                (
                    "- GitHub Issue: "
                    f"https://github.com/{work_item.repository}/issues/"
                    f"{work_item.issue_number}"
                ),
                f"- Task branch: `{work_item.task_branch}`",
                "- Input channel: GitHub only; Slack replies are not read.",
            )
        )

    @staticmethod
    def _render_terminal(
        work_item: WorkItem,
        turn: Turn,
        desired_task_state: TaskState,
    ) -> str:
        lines = [
            f"Codex Turn {turn.turn_number}",
            "",
            f"- Dispatcher state: `agent:{desired_task_state.value}`",
            f"- Summary: {turn.result_summary}",
            f"- Task branch: `{work_item.task_branch}`",
        ]
        if work_item.last_published_sha is not None:
            lines.append(
                f"- Published checkpoint: `{work_item.last_published_sha}`"
            )
        if work_item.pr_number is not None:
            lines.append(
                "- Pull request: "
                f"https://github.com/{work_item.repository}/pull/{work_item.pr_number}"
            )
        return "\n".join(lines)


def root_delivery_key(work_item_id: str) -> str:
    return slack_deduplication_key(
        work_item_id,
        kind=SlackReportKind.ROOT,
        turn_id=None,
    )
