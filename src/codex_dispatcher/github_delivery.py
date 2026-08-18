"""Idempotent GitHub Draft PR and Issue projection for terminal WorkItems."""

from __future__ import annotations

from dataclasses import dataclass

from codex_dispatcher.state_store import StateStore
from codex_dispatcher.trackers.base import (
    DraftPullRequestRequest,
    PullRequest,
    PullRequestState,
    TaskState,
    Tracker,
    TrackerTask,
)
from codex_dispatcher.work_items import WorkItem, WorkItemState


class GitHubDeliveryRejected(RuntimeError):
    """Raised when remote PR state conflicts with the persisted WorkItem binding."""


@dataclass(frozen=True, slots=True)
class GitHubDeliveryResult:
    work_item: WorkItem
    pull_request: PullRequest | None


class GitHubDeliveryCoordinator:
    """Recover/create one PR, bind it, then upsert one fixed Issue projection."""

    def __init__(self, *, store: StateStore, tracker: Tracker) -> None:
        if not isinstance(store, StateStore):
            raise TypeError("store must be a StateStore")
        self._store = store
        self._tracker = tracker

    def reconcile(
        self,
        task: TrackerTask,
        *,
        work_item: WorkItem,
        desired_task_state: TaskState,
    ) -> GitHubDeliveryResult:
        self._validate_binding(task, work_item, desired_task_state)
        pull_request: PullRequest | None = None
        if work_item.last_published_sha is not None:
            work_item, pull_request = self._ensure_pull_request(task, work_item)
        elif work_item.pr_number is not None:
            raise GitHubDeliveryRejected(
                "WorkItem has a Draft PR without a published checkpoint"
            )
        self._tracker.upsert_run_comment(
            work_item.repository,
            str(work_item.issue_number),
            f"work-item:{work_item.work_item_id}:status",
            self._render_comment(
                work_item,
                pull_request,
                desired_task_state,
            ),
        )
        return GitHubDeliveryResult(work_item, pull_request)

    def _ensure_pull_request(
        self,
        task: TrackerTask,
        work_item: WorkItem,
    ) -> tuple[WorkItem, PullRequest]:
        observed = self._tracker.find_pr_by_branch(
            work_item.repository,
            work_item.task_branch,
        )
        if work_item.pr_number is not None:
            if observed is None:
                raise GitHubDeliveryRejected("persisted Draft PR is missing remotely")
            self._validate_pull_request(observed, work_item, require_draft=False)
            if observed.number != work_item.pr_number:
                raise GitHubDeliveryRejected(
                    "remote pull request differs from the persisted binding"
                )
            return work_item, observed

        if observed is None:
            created = self._tracker.create_draft_pr(
                self._build_request(work_item)
            )
            self._validate_pull_request(created, work_item, require_draft=True)
            observed = self._tracker.find_pr_by_branch(
                work_item.repository,
                work_item.task_branch,
            )
            if observed is None or observed.number != created.number:
                raise GitHubDeliveryRejected(
                    "Draft PR creation was not proven by branch read-back"
                )
        self._validate_pull_request(observed, work_item, require_draft=True)
        bound = self._store.bind_draft_pr(work_item.work_item_id, observed.number)
        return bound, observed

    @staticmethod
    def _build_request(
        work_item: WorkItem,
    ) -> DraftPullRequestRequest:
        return DraftPullRequestRequest(
            repository=work_item.repository,
            branch_name=work_item.task_branch,
            base_branch=work_item.base_branch,
            title=f"Codex work for Issue #{work_item.issue_number}",
            body=(
                f"Automated checkpoint for Issue #{work_item.issue_number}.\n\n"
                f"Work item: `{work_item.work_item_id}`\n"
                f"Task branch: `{work_item.task_branch}`\n\n"
                "Review and merge remain manual."
            ),
        )

    @staticmethod
    def _validate_binding(
        task: TrackerTask,
        work_item: WorkItem,
        desired_task_state: TaskState,
    ) -> None:
        if (
            not isinstance(task, TrackerTask)
            or not isinstance(work_item, WorkItem)
            or not isinstance(desired_task_state, TaskState)
        ):
            raise TypeError("delivery inputs must use dispatcher DTOs")
        expected_state = {
            WorkItemState.REVIEW: TaskState.REVIEW,
            WorkItemState.WAITING_INPUT: TaskState.NEEDS_INPUT,
            WorkItemState.BLOCKED: TaskState.BLOCKED,
        }.get(work_item.state)
        if (
            task.repository != work_item.repository
            or task.issue_number != work_item.issue_number
            or task.task_id != str(work_item.issue_number)
            or task.issue_node_id != work_item.issue_node_id
        ):
            raise GitHubDeliveryRejected("Issue identity conflicts with the WorkItem")
        if expected_state is None or desired_task_state is not expected_state:
            raise GitHubDeliveryRejected(
                "Issue delivery state conflicts with the terminal WorkItem state"
            )

    @staticmethod
    def _validate_pull_request(
        pull_request: PullRequest,
        work_item: WorkItem,
        *,
        require_draft: bool,
    ) -> None:
        if not isinstance(pull_request, PullRequest):
            raise TypeError("pull_request must be a dispatcher DTO")
        expected_url = (
            f"https://github.com/{work_item.repository}/pull/{pull_request.number}"
        )
        if (
            type(pull_request.number) is not int
            or pull_request.number <= 0
            or pull_request.url != expected_url
            or pull_request.branch_name != work_item.task_branch
            or pull_request.base_branch != work_item.base_branch
            or pull_request.state is not PullRequestState.OPEN
            or pull_request.is_cross_repository
            or (require_draft and not pull_request.is_draft)
        ):
            raise GitHubDeliveryRejected(
                "pull request conflicts with the WorkItem publication binding"
            )

    @staticmethod
    def _render_comment(
        work_item: WorkItem,
        pull_request: PullRequest | None,
        desired_task_state: TaskState,
    ) -> str:
        lines = [
            f"Codex work item `{work_item.work_item_id}`",
            "",
            f"- Task branch: `{work_item.task_branch}`",
            f"- Dispatcher state: `agent:{desired_task_state.value}`",
        ]
        if pull_request is None:
            lines.append("- Published checkpoint: none")
        else:
            assert work_item.last_published_sha is not None
            lines.append(
                f"- Published checkpoint: `{work_item.last_published_sha}`"
            )
            lines.append(f"- Pull request: {pull_request.url}")
        return "\n".join(lines)
