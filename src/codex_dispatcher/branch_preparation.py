"""Crash-recoverable orchestration for initial task-branch creation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from codex_dispatcher.domain import Run, RunState
from codex_dispatcher.git_workspace import GitWorkspace, PreparedWorktree, PublishedBranch
from codex_dispatcher.state_store import StateStore


@dataclass(frozen=True, slots=True)
class BranchPreparationResult:
    run: Run
    worktree: PreparedWorktree
    published: PublishedBranch


class BranchPreparationService:
    """Persist an anchor, publish it idempotently, then advance the run state."""

    def __init__(self, *, store: StateStore, workspace: GitWorkspace) -> None:
        self._store = store
        self._workspace = workspace

    def prepare_run(self, run_id: str, *, remote_url: str) -> BranchPreparationResult:
        run = self._store.get_run(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        if run.state not in {RunState.CLAIMED, RunState.BRANCH_PREPARED}:
            raise ValueError("run must be claimed before branch preparation")
        if (run.base_sha is None) != (run.task_branch is None):
            raise ValueError("run contains a partial branch anchor")

        task_branch = run.task_branch or _task_branch(run)
        worktree = self._workspace.prepare(
            repository=run.repository,
            remote_url=remote_url,
            base_branch=run.base_branch,
            task_branch=task_branch,
            expected_base_sha=run.base_sha,
        )
        anchored = self._store.record_branch_anchor(
            run.run_id,
            base_sha=worktree.base_sha,
            task_branch=worktree.task_branch,
        )
        if anchored.base_sha != worktree.base_sha or anchored.task_branch != task_branch:
            raise RuntimeError("persisted branch anchor changed unexpectedly")

        published = self._workspace.publish_task_branch(worktree)
        completed = self._store.mark_branch_prepared(
            run.run_id,
            head_sha=published.head_sha,
            remote_reused=published.reused,
        )
        return BranchPreparationResult(completed, worktree, published)


def _task_branch(run: Run) -> str:
    digest = hashlib.sha256(run.run_id.encode("utf-8")).hexdigest()[:12]
    return f"codex/issue-{run.issue_number}-{digest}"
