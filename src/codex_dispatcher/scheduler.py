"""Read-only dispatch candidate planning for offline and dry-run use."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from codex_dispatcher.config import Config, RepositoryConfig
from codex_dispatcher.domain import Run
from codex_dispatcher.repository_admission import (
    HigherValueCanaryTarget,
    RepositoryRecoveryProfile,
    RepositoryTargetReadbackProfile,
    build_higher_value_canary_policy_identity,
    evaluate_repository_admission,
)
from codex_dispatcher.task_spec import TaskSpecError, is_path_allowed, parse_task_spec
from codex_dispatcher.trackers.base import TaskState, Tracker, TrackerTask


CLOUD_EXECUTOR_LABEL = "exec:cloud"
SSH_CLI_EXECUTOR_LABEL = "exec:ssh-cli"


@dataclass(frozen=True, slots=True)
class Rejection:
    repository: str
    issue_number: int
    code: str


@dataclass(frozen=True, slots=True)
class DryRunPlan:
    selected: tuple[TrackerTask, ...]
    rejected: tuple[Rejection, ...]


@dataclass(frozen=True, slots=True)
class _Candidate:
    task: TrackerTask
    priority: int
    created_at: datetime


def build_dry_run_plan(
    config: Config, tracker: Tracker, active_runs: Iterable[Run] = ()
) -> DryRunPlan:
    """Select candidates using read methods only; never claim or mutate external state."""
    return _build_dry_run_plan(
        config,
        tracker,
        active_runs=active_runs,
        executor_label=CLOUD_EXECUTOR_LABEL,
        global_max_active=config.scheduler.global_max_active,
    )


def build_ssh_dry_run_plan(
    config: Config,
    tracker: Tracker,
    *,
    active_turn_exists: bool = False,
) -> DryRunPlan:
    """Plan at most one SSH CLI candidate without mutating tracker or local state."""
    if type(active_turn_exists) is not bool:
        raise TypeError("active_turn_exists must be a bool")
    return _build_dry_run_plan(
        config,
        tracker,
        active_runs=(),
        executor_label=SSH_CLI_EXECUTOR_LABEL,
        global_max_active=0 if active_turn_exists else 1,
    )


def build_ssh_higher_value_canary_plan(
    config: Config,
    tracker: Tracker,
    *,
    target: HigherValueCanaryTarget,
    active_turn_exists: bool = False,
) -> DryRunPlan:
    """Plan only one exact manual canary without changing normal admission."""
    if not isinstance(target, HigherValueCanaryTarget):
        raise TypeError("target must be a HigherValueCanaryTarget")
    if type(active_turn_exists) is not bool:
        raise TypeError("active_turn_exists must be a bool")
    return _build_dry_run_plan(
        config,
        tracker,
        active_runs=(),
        executor_label=SSH_CLI_EXECUTOR_LABEL,
        global_max_active=0 if active_turn_exists else 1,
        higher_value_canary_target=target,
    )


def _build_dry_run_plan(
    config: Config,
    tracker: Tracker,
    *,
    active_runs: Iterable[Run],
    executor_label: str,
    global_max_active: int,
    higher_value_canary_target: HigherValueCanaryTarget | None = None,
) -> DryRunPlan:
    active = tuple(run for run in active_runs if run.is_active)
    active_by_repository = Counter(run.repository for run in active)
    active_issues = {(run.repository, run.issue_number) for run in active}
    repository_configs = {repository.slug: repository for repository in config.repositories}
    candidates: list[_Candidate] = []
    rejected: list[Rejection] = []

    for repository in config.repositories:
        for task in tracker.list_ready_tasks(repository.slug):
            candidate, error = _validate_candidate(
                task,
                repository,
                executor_label=executor_label,
                recovery_profiles=(
                    frozenset()
                    if config.repository_admission is None
                    else config.repository_admission.recovery_profiles
                ),
                target_readback_profiles=(
                    frozenset()
                    if config.repository_admission is None
                    else config.repository_admission.target_readback_profiles
                ),
                higher_value_canary_target=higher_value_canary_target,
            )
            if error is not None:
                rejected.append(Rejection(task.repository, task.issue_number, error))
            elif candidate is not None:
                candidates.append(candidate)

    candidates.sort(
        key=lambda candidate: (
            candidate.priority,
            candidate.created_at,
            candidate.task.issue_number,
            candidate.task.repository,
        )
    )
    remaining_global = max(global_max_active - len(active), 0)
    selected: list[TrackerTask] = []
    selected_repositories: set[str] = set()
    for candidate in candidates:
        task = candidate.task
        repository = repository_configs[task.repository]
        if (task.repository, task.issue_number) in active_issues:
            rejected.append(Rejection(task.repository, task.issue_number, "issue_already_active"))
        elif remaining_global == 0:
            rejected.append(Rejection(task.repository, task.issue_number, "global_capacity"))
        elif active_by_repository[task.repository] >= repository.max_active:
            rejected.append(Rejection(task.repository, task.issue_number, "repository_capacity"))
        elif task.repository in selected_repositories:
            rejected.append(
                Rejection(task.repository, task.issue_number, "one_per_repository_sweep")
            )
        else:
            selected.append(task)
            selected_repositories.add(task.repository)
            remaining_global -= 1

    return DryRunPlan(tuple(selected), tuple(rejected))


def _validate_candidate(
    task: TrackerTask,
    repository: RepositoryConfig,
    *,
    executor_label: str,
    recovery_profiles: frozenset[RepositoryRecoveryProfile],
    target_readback_profiles: frozenset[RepositoryTargetReadbackProfile],
    higher_value_canary_target: HigherValueCanaryTarget | None = None,
) -> tuple[_Candidate | None, str | None]:
    if task.repository != repository.slug:
        return None, "repository_mismatch"
    if higher_value_canary_target is None:
        admission = evaluate_repository_admission(
            repository.repository_class,
            recovery_profiles,
            target_readback_profiles,
        )
        if not admission.admitted:
            return None, admission.code
    else:
        if not higher_value_canary_target.matches_issue(
            repository=task.repository,
            issue_number=task.issue_number,
            issue_node_id=task.issue_node_id,
        ):
            return None, "higher_value_canary_target_mismatch"
        try:
            build_higher_value_canary_policy_identity(
                target=higher_value_canary_target,
                repository=task.repository,
                issue_number=task.issue_number,
                issue_node_id=task.issue_node_id,
                repository_class=repository.repository_class,
                recovery_profiles=recovery_profiles,
                target_readback_profiles=target_readback_profiles,
            )
        except ValueError:
            return None, "higher_value_canary_policy_mismatch"
    if task.state is not TaskState.READY or not task.is_open:
        return None, "not_open_ready"
    status_labels = [label for label in task.labels if label.startswith("agent:")]
    if status_labels != ["agent:ready"]:
        return None, "invalid_status_labels"
    executor_labels = [label for label in task.labels if label.startswith("exec:")]
    if executor_labels != [executor_label]:
        return None, "invalid_executor_labels"
    if task.ready_approved_by not in repository.maintainers:
        return None, "untrusted_ready_approval"
    if task.has_unresolved_dependencies:
        return None, "unresolved_dependencies"
    if type(task.issue_number) is not int or task.issue_number <= 0:
        return None, "invalid_issue_number"

    try:
        task_spec = parse_task_spec(task.body)
    except TaskSpecError:
        return None, "invalid_task_spec"
    if any(
        not is_path_allowed(path, repository.allowed_paths, repository.denied_paths)
        for path in task_spec.allowed_paths
    ):
        return None, "path_outside_policy"

    priority_labels = [label for label in task.labels if label.startswith("priority:")]
    if len(priority_labels) > 1:
        return None, "invalid_priority_labels"
    if priority_labels:
        priority_label = priority_labels[0]
        if priority_label not in {f"priority:p{value}" for value in range(4)}:
            return None, "invalid_priority_labels"
        priority = int(priority_label[-1])
    else:
        priority = 2

    try:
        created_at = datetime.fromisoformat(task.created_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None, "invalid_created_at"
    if created_at.tzinfo is None:
        return None, "invalid_created_at"
    return _Candidate(task, priority, created_at), None
