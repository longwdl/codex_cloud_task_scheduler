"""Read-only planning for the next recovery-first SSH dispatcher sweep."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from codex_dispatcher.config import Config
from codex_dispatcher.scheduler import DryRunPlan, Rejection, build_ssh_dry_run_plan
from codex_dispatcher.ssh_recovery import (
    SshRecoveryAction,
    SshRecoveryPlan,
    plan_ssh_recovery,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.trackers.base import PullRequest, Tracker, TrackerTask
from codex_dispatcher.work_items import Turn, WorkItem


class SshPreflightStatus(StrEnum):
    READY_CANDIDATE = "ready_candidate"
    READY_RECOVERY = "ready_recovery"
    IDLE = "idle"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class SshPreflightPlan:
    """The next safe action, computed using only tracker and SQLite reads."""

    status: SshPreflightStatus
    recovery_action: SshRecoveryAction
    task: TrackerTask | None = None
    work_item: WorkItem | None = None
    turn: Turn | None = None
    pull_request: PullRequest | None = None
    reason: str | None = None
    rejected: tuple[Rejection, ...] = ()


def build_ssh_preflight_plan(
    config: Config,
    store: StateStore,
    tracker: Tracker,
) -> SshPreflightPlan:
    """Plan recovery first, then at most one new ``exec:ssh-cli`` candidate."""
    if not isinstance(config, Config):
        raise TypeError("config must be a Config")
    if not isinstance(store, StateStore):
        raise TypeError("store must be a StateStore")

    recovery = plan_ssh_recovery(config, store, tracker)
    if recovery.action is SshRecoveryAction.BLOCK:
        return _from_recovery(
            SshPreflightStatus.BLOCKED,
            recovery,
        )
    if recovery.action is not SshRecoveryAction.IDLE:
        return _from_recovery(
            SshPreflightStatus.READY_RECOVERY,
            recovery,
        )

    candidates = build_ssh_dry_run_plan(
        config,
        tracker,
        active_turn_exists=False,
    )
    return _from_candidates(candidates)


def _from_recovery(
    status: SshPreflightStatus,
    recovery: SshRecoveryPlan,
) -> SshPreflightPlan:
    return SshPreflightPlan(
        status=status,
        recovery_action=recovery.action,
        task=recovery.task,
        work_item=recovery.work_item,
        turn=recovery.turn,
        pull_request=recovery.pull_request,
        reason=recovery.reason,
    )


def _from_candidates(candidates: DryRunPlan) -> SshPreflightPlan:
    if len(candidates.selected) > 1:
        raise RuntimeError("SSH preflight selected more than one candidate")
    if candidates.selected:
        return SshPreflightPlan(
            status=SshPreflightStatus.READY_CANDIDATE,
            recovery_action=SshRecoveryAction.IDLE,
            task=candidates.selected[0],
            rejected=candidates.rejected,
        )
    return SshPreflightPlan(
        status=SshPreflightStatus.IDLE,
        recovery_action=SshRecoveryAction.IDLE,
        rejected=candidates.rejected,
    )
