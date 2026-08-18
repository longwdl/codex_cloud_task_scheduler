"""Provider-independent coordination for one claimed SSH CLI Issue Turn."""

from __future__ import annotations

from typing import Iterable

from codex_dispatcher.config import Config, RepositoryConfig
from codex_dispatcher.scheduler import DryRunPlan, build_ssh_dry_run_plan
from codex_dispatcher.source_bundle import SourceBundle
from codex_dispatcher.ssh_dispatch_planning import (
    SshDispatchPlanningError,
    WorkItemAction,
    build_ssh_turn_plan,
    resolve_ssh_work_item,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.trackers.base import Tracker, TrackerTask
from codex_dispatcher.turn_orchestration import OfflineTurnOrchestrator, TurnProgress
from codex_dispatcher.work_items import WorkItem, WorkItemState


class OfflineSshDispatchService:
    """Join selection, WorkItem recovery, Prompt planning, and the fixed Runner port.

    GitHub claim/state writes and Publisher/Slack delivery remain outside this
    service. Callers must pass the verified post-claim Issue snapshot.
    """

    def __init__(
        self,
        *,
        config: Config,
        store: StateStore,
        orchestrator: OfflineTurnOrchestrator,
    ) -> None:
        if not isinstance(config, Config):
            raise TypeError("config must be a Config")
        self._config = config
        self._store = store
        self._orchestrator = orchestrator
        self._repositories = {item.slug: item for item in config.repositories}

    def plan_candidates(self, tracker: Tracker) -> DryRunPlan:
        """Perform one SSH candidate sweep using tracker reads only."""
        return build_ssh_dry_run_plan(
            self._config,
            tracker,
            active_turn_exists=self._store.get_active_turn() is not None,
        )

    def resolve_and_prepare(
        self,
        task: TrackerTask,
        *,
        base_sha: str,
        source_bundle: SourceBundle | None = None,
        runner_root: str = "/srv/codex-runner/work-items",
        created_at: str | None = None,
    ) -> WorkItem:
        """Persist/recover the unique WorkItem and idempotently prepare its Runner repo."""
        repository = self._repository(task.repository)
        existing = self._store.get_work_item_by_issue(task.repository, task.issue_number)
        resolution = resolve_ssh_work_item(
            task=task,
            repository=repository,
            base_sha=base_sha,
            existing_work_item=existing,
            runner_root=runner_root,
            created_at=created_at,
        )
        needs_prepare = resolution.action in {
            WorkItemAction.PREPARE,
            WorkItemAction.RETRY_PREPARE,
        }
        if needs_prepare and source_bundle is None:
            raise SshDispatchPlanningError("Runner preparation requires an exact source bundle")
        if source_bundle is not None and (
            source_bundle.base_sha != resolution.work_item.base_sha
        ):
            raise SshDispatchPlanningError(
                "source bundle base SHA conflicts with persisted WorkItem"
            )

        if resolution.is_new:
            self._store.create_work_item(resolution.work_item)
        if resolution.action is WorkItemAction.REACTIVATE:
            return self._store.update_work_item_state(
                resolution.work_item.work_item_id, WorkItemState.READY
            )
        if needs_prepare:
            assert source_bundle is not None
            return self._orchestrator.prepare_work_item(
                resolution.work_item.work_item_id,
                source_bundle=source_bundle.artifact,
            )
        return resolution.work_item

    def run_claimed_turn(
        self,
        task: TrackerTask,
        *,
        comments: Iterable[object] = (),
        turn_id: str | None = None,
    ) -> TurnProgress:
        """Freeze and run the next Turn while atomically checking its Prompt number."""
        repository = self._repository(task.repository)
        work_item = self._store.get_work_item_by_issue(
            task.repository, task.issue_number
        )
        if work_item is None:
            raise SshDispatchPlanningError("Issue does not have a persisted WorkItem")
        turn_number = self._store.next_turn_number(work_item.work_item_id)
        plan = build_ssh_turn_plan(
            task=task,
            repository=repository,
            work_item=work_item,
            turn_number=turn_number,
            comments=comments,
        )
        return self._orchestrator.run_turn(
            work_item.work_item_id,
            issue_revision=plan.issue_revision,
            prompt=plan.prompt,
            expected_turn_number=plan.turn_number,
            turn_id=turn_id,
        )

    def reconcile_turn(self, turn_id: str) -> TurnProgress:
        """Reconcile one ambiguous active Turn without replaying its Prompt."""
        return self._orchestrator.reconcile_turn(turn_id)

    def _repository(self, slug: str) -> RepositoryConfig:
        try:
            return self._repositories[slug]
        except KeyError as exc:
            raise SshDispatchPlanningError("Issue repository is not configured") from exc
