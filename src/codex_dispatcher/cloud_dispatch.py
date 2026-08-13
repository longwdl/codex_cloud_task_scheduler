"""Persist the Cloud submission boundary without creating a remote task."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from codex_dispatcher.domain import Run, RunState
from codex_dispatcher.executors.base import Executor, SubmissionRequest
from codex_dispatcher.state_store import StateStore


@dataclass(frozen=True, slots=True)
class PreparedCloudDispatch:
    run: Run
    request: SubmissionRequest
    known_task_ids: tuple[str, ...]


class CloudDispatchPreparationService:
    """Snapshot remote task IDs, then atomically mark a run as dispatching."""

    def __init__(self, *, store: StateStore, executor: Executor) -> None:
        self._store = store
        self._executor = executor

    def prepare(self, run_id: str, *, prompt: str) -> PreparedCloudDispatch:
        if not isinstance(prompt, str) or not prompt or "\x00" in prompt:
            raise ValueError("prompt must be a non-empty string without NUL")
        run = self._store.get_run(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        if run.state is not RunState.BRANCH_PREPARED:
            raise ValueError("run must be branch_prepared before Cloud dispatch")
        if run.base_sha is None or run.task_branch is None or run.head_sha != run.base_sha:
            raise ValueError("run does not have a verified initial branch anchor")
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if prompt_sha256 != run.prompt_sha256:
            raise ValueError("prompt does not match the persisted immutable snapshot hash")

        remote_runs = self._executor.list_runs(run.cloud_environment_id)
        task_ids = tuple(remote.external_task_id for remote in remote_runs)
        if any(
            not task_id or remote.environment_id != run.cloud_environment_id
            for task_id, remote in zip(task_ids, remote_runs, strict=True)
        ):
            raise ValueError("executor returned an invalid pre-submit task snapshot")
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("executor returned duplicate pre-submit task IDs")
        known_task_ids = tuple(sorted(task_ids))
        dispatching = self._store.begin_cloud_dispatch(
            run.run_id,
            known_task_ids=known_task_ids,
        )
        request = SubmissionRequest(
            run.run_id,
            run.cloud_environment_id,
            prompt,
            run.task_branch,
            run.base_sha,
        )
        return PreparedCloudDispatch(dispatching, request, known_task_ids)
