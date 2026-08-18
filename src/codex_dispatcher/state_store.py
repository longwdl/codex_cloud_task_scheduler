"""SQLite-backed, recoverable run state."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from typing import Any

from codex_dispatcher.domain import Run, RunState, utc_now_iso
from codex_dispatcher.work_items import (
    ACTIVE_TURN_STATES,
    TaskBranchSource,
    Turn,
    TurnState,
    WorkItem,
    WorkItemState,
    validate_git_sha,
)


_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


class StateStore:
    """Own a SQLite connection and provide transactional run persistence."""

    def __init__(self, database_path: Path, *, read_only: bool = False) -> None:
        self.database_path = database_path
        if read_only:
            database_uri = database_path.resolve().as_uri() + "?mode=ro"
            self._connection = sqlite3.connect(database_uri, uri=True)
        else:
            self._connection = sqlite3.connect(database_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        if not read_only:
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield self._connection
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def migrate(self) -> None:
        with self._transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                int(row[0])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            migration_root = files("codex_dispatcher.migrations")
            migrations: list[tuple[int, str]] = []
            for resource in migration_root.iterdir():
                match = re.fullmatch(r"([0-9]{3})_[A-Za-z0-9_]+\.sql", resource.name)
                if match is not None:
                    migrations.append((int(match.group(1)), resource.read_text(encoding="utf-8")))
            versions = [version for version, _ in migrations]
            if len(set(versions)) != len(versions):
                raise RuntimeError("duplicate SQLite migration version")
            unknown_applied = applied - set(versions)
            if unknown_applied:
                raise RuntimeError("database contains an unsupported SQLite migration version")
            for version, migration_sql in sorted(migrations):
                if version in applied:
                    continue
                for statement in migration_sql.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utc_now_iso()),
                )

    def create_run(self, run: Run) -> None:
        fields = (
            "run_id", "repository", "issue_number", "attempt_no", "state", "prompt_sha256",
            "base_branch", "base_sha", "task_branch", "cloud_environment_id", "cloud_task_id",
            "cloud_task_url", "cloud_diff_sha256", "head_sha", "pr_number", "retry_count",
            "created_at", "updated_at", "last_seen_at", "last_error_code", "last_error_redacted",
        )
        values = tuple(
            getattr(run, field).value if field == "state" else getattr(run, field)
            for field in fields
        )
        placeholders = ", ".join("?" for _ in fields)
        with self._transaction() as connection:
            connection.execute(
                f"INSERT INTO runs ({', '.join(fields)}) VALUES ({placeholders})", values
            )

    def get_run(self, run_id: str) -> Run | None:
        row = self._connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row is not None else None

    def list_active_runs(self, repository: str | None = None) -> list[Run]:
        sql = (
            "SELECT * FROM runs "
            "WHERE state NOT IN ('review', 'needs_input', 'blocked', 'discarded')"
        )
        params: tuple[str, ...] = ()
        if repository is not None:
            sql += " AND repository = ?"
            params = (repository,)
        sql += " ORDER BY created_at, run_id"
        return [self._row_to_run(row) for row in self._connection.execute(sql, params)]

    def update_state(self, run_id: str, state: RunState, *, updated_at: str | None = None) -> Run:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            run = self._row_to_run(row)
            updated = run.transition_to(state, at=updated_at)
            cursor = connection.execute(
                "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ? AND state = ?",
                (updated.state.value, updated.updated_at, run_id, run.state.value),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent state update detected for run: {run_id}")
        return updated

    def bind_cloud_task(
        self, run_id: str, cloud_task_id: str, cloud_task_url: str | None = None
    ) -> Run:
        if not cloud_task_id:
            raise ValueError("cloud_task_id must be non-empty")
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        if run.cloud_task_id is not None and run.cloud_task_id != cloud_task_id:
            raise ValueError(f"run {run_id} is already bound to a different cloud task")
        now = utc_now_iso()
        with self._transaction() as connection:
            connection.execute(
                "UPDATE runs SET cloud_task_id = ?, cloud_task_url = ?, "
                "updated_at = ? WHERE run_id = ?",
                (cloud_task_id, cloud_task_url, now, run_id),
            )
        bound = self.get_run(run_id)
        assert bound is not None
        return bound

    def record_branch_anchor(
        self,
        run_id: str,
        *,
        base_sha: str,
        task_branch: str,
        updated_at: str | None = None,
    ) -> Run:
        """Persist the immutable branch anchor before any remote branch write."""
        if not isinstance(base_sha, str) or re.fullmatch(r"[0-9a-f]{40,64}", base_sha) is None:
            raise ValueError("base_sha must be a lowercase Git object ID")
        if (
            not isinstance(task_branch, str)
            or _BRANCH_RE.fullmatch(task_branch) is None
            or task_branch.startswith(("/", "."))
            or task_branch.endswith(("/", ".", ".lock"))
            or ".." in task_branch
            or "//" in task_branch
            or "@{" in task_branch
        ):
            raise ValueError("task_branch must be a safe Git branch name")
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            run = self._row_to_run(row)
            if run.state not in {RunState.CLAIMED, RunState.BRANCH_PREPARED}:
                raise ValueError("branch anchor can only be recorded for a claimed run")
            if run.base_sha is not None or run.task_branch is not None:
                if run.base_sha != base_sha or run.task_branch != task_branch:
                    raise ValueError(f"run {run_id} already has a different branch anchor")
                return run
            cursor = connection.execute(
                "UPDATE runs SET base_sha = ?, task_branch = ?, updated_at = ? "
                "WHERE run_id = ? AND state = ? AND base_sha IS NULL AND task_branch IS NULL",
                (base_sha, task_branch, now, run_id, RunState.CLAIMED.value),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent branch anchor update detected for run: {run_id}")
            self._insert_event(
                connection,
                run_id,
                "branch_anchor_recorded",
                {"base_sha": base_sha, "task_branch": task_branch},
                now,
            )
        anchored = self.get_run(run_id)
        assert anchored is not None
        return anchored

    def mark_branch_prepared(
        self,
        run_id: str,
        *,
        head_sha: str,
        remote_reused: bool,
        updated_at: str | None = None,
    ) -> Run:
        """Record verified remote branch creation and advance the run atomically."""
        if not isinstance(head_sha, str) or re.fullmatch(r"[0-9a-f]{40,64}", head_sha) is None:
            raise ValueError("head_sha must be a lowercase Git object ID")
        if type(remote_reused) is not bool:
            raise TypeError("remote_reused must be a bool")
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            run = self._row_to_run(row)
            if run.base_sha is None or run.task_branch is None:
                raise ValueError("branch anchor must be persisted before branch completion")
            if head_sha != run.base_sha:
                raise ValueError("initial task branch HEAD must equal its persisted base SHA")
            if run.state is RunState.BRANCH_PREPARED:
                if run.head_sha != head_sha:
                    raise ValueError(f"run {run_id} already has a different branch HEAD")
                return run
            if run.state is not RunState.CLAIMED:
                raise ValueError("only a claimed run can complete branch preparation")
            updated = run.transition_to(RunState.BRANCH_PREPARED, at=now)
            cursor = connection.execute(
                "UPDATE runs SET state = ?, head_sha = ?, updated_at = ? "
                "WHERE run_id = ? AND state = ? AND base_sha = ? AND task_branch = ?",
                (
                    updated.state.value,
                    head_sha,
                    updated.updated_at,
                    run_id,
                    run.state.value,
                    run.base_sha,
                    run.task_branch,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent branch completion detected for run: {run_id}")
            self._insert_event(
                connection,
                run_id,
                "branch_prepared",
                {
                    "head_sha": head_sha,
                    "remote_reused": remote_reused,
                    "task_branch": run.task_branch,
                },
                now,
            )
        completed = self.get_run(run_id)
        assert completed is not None
        return completed

    def begin_cloud_dispatch(
        self,
        run_id: str,
        *,
        known_task_ids: tuple[str, ...],
        updated_at: str | None = None,
    ) -> Run:
        """Persist the pre-submit task snapshot before any Cloud task creation."""
        if not isinstance(known_task_ids, tuple) or any(
            not isinstance(task_id, str) or not task_id for task_id in known_task_ids
        ):
            raise TypeError("known_task_ids must be a tuple of non-empty strings")
        if len(set(known_task_ids)) != len(known_task_ids):
            raise ValueError("known_task_ids must be unique")
        normalized_ids = tuple(sorted(known_task_ids))
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"run not found: {run_id}")
            run = self._row_to_run(row)
            if run.state is not RunState.BRANCH_PREPARED:
                raise ValueError("only a branch-prepared run can begin Cloud dispatch")
            if run.base_sha is None or run.task_branch is None or run.head_sha != run.base_sha:
                raise ValueError("Cloud dispatch requires one verified initial branch anchor")
            updated = run.transition_to(RunState.DISPATCHING, at=now)
            cursor = connection.execute(
                "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ? AND state = ? "
                "AND base_sha = ? AND head_sha = ? AND task_branch = ?",
                (
                    updated.state.value,
                    updated.updated_at,
                    run_id,
                    run.state.value,
                    run.base_sha,
                    run.head_sha,
                    run.task_branch,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent Cloud dispatch detected for run: {run_id}")
            self._insert_event(
                connection,
                run_id,
                "cloud_dispatch_started",
                {
                    "base_sha": run.base_sha,
                    "environment_id": run.cloud_environment_id,
                    "head_sha": run.head_sha,
                    "known_task_ids": normalized_ids,
                    "prompt_sha256": run.prompt_sha256,
                    "task_branch": run.task_branch,
                },
                now,
            )
        dispatching = self.get_run(run_id)
        assert dispatching is not None
        return dispatching

    def create_work_item(self, work_item: WorkItem) -> None:
        """Persist one stable Issue-to-session identity."""
        fields = (
            "work_item_id",
            "repository",
            "issue_number",
            "issue_node_id",
            "state",
            "base_branch",
            "task_branch",
            "task_branch_source",
            "runner_directory",
            "codex_session_id",
            "slack_channel_id",
            "slack_thread_ts",
            "pr_number",
            "base_sha",
            "last_published_sha",
            "created_at",
            "updated_at",
        )
        values = tuple(
            getattr(work_item, field).value
            if field in {"state", "task_branch_source"}
            else getattr(work_item, field)
            for field in fields
        )
        with self._transaction() as connection:
            connection.execute(
                f"INSERT INTO work_items ({', '.join(fields)}) "
                f"VALUES ({', '.join('?' for _ in fields)})",
                values,
            )
            self._insert_work_item_event(
                connection,
                work_item.work_item_id,
                None,
                "work_item_created",
                {
                    "repository": work_item.repository,
                    "issue_number": work_item.issue_number,
                    "task_branch": work_item.task_branch,
                    "task_branch_source": work_item.task_branch_source.value,
                },
                work_item.created_at,
            )

    def get_work_item(self, work_item_id: str) -> WorkItem | None:
        row = self._connection.execute(
            "SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)
        ).fetchone()
        return self._row_to_work_item(row) if row is not None else None

    def get_work_item_by_issue(self, repository: str, issue_number: int) -> WorkItem | None:
        row = self._connection.execute(
            "SELECT * FROM work_items WHERE repository = ? AND issue_number = ?",
            (repository, issue_number),
        ).fetchone()
        return self._row_to_work_item(row) if row is not None else None

    def list_work_items(self, *, include_completed: bool = True) -> tuple[WorkItem, ...]:
        """Return persisted WorkItems in stable creation order for reconciliation."""
        if type(include_completed) is not bool:
            raise TypeError("include_completed must be a bool")
        sql = "SELECT * FROM work_items"
        parameters: tuple[str, ...] = ()
        if not include_completed:
            sql += " WHERE state != ?"
            parameters = (WorkItemState.COMPLETED.value,)
        sql += " ORDER BY created_at, work_item_id"
        return tuple(
            self._row_to_work_item(row)
            for row in self._connection.execute(sql, parameters)
        )

    def get_active_turn(self) -> Turn | None:
        """Return the globally unique active Turn, if one exists."""
        placeholders = ", ".join("?" for _ in ACTIVE_TURN_STATES)
        rows = self._connection.execute(
            f"SELECT * FROM turns WHERE state IN ({placeholders}) ORDER BY created_at LIMIT 2",
            tuple(state.value for state in ACTIVE_TURN_STATES),
        ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("database contains more than one active Turn")
        return self._row_to_turn(rows[0]) if rows else None

    def next_turn_number(self, work_item_id: str) -> int:
        """Read the next Turn number; ``begin_turn`` must still compare it atomically."""
        row = self._connection.execute(
            "SELECT state FROM work_items WHERE work_item_id = ?", (work_item_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"work item not found: {work_item_id}")
        if WorkItemState(row["state"]) is not WorkItemState.READY:
            raise ValueError("work item must be ready before planning a Turn")
        return int(
            self._connection.execute(
                "SELECT COALESCE(MAX(turn_number), 0) + 1 FROM turns WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()[0]
        )

    def update_work_item_state(
        self, work_item_id: str, state: WorkItemState, *, updated_at: str | None = None
    ) -> WorkItem:
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"work item not found: {work_item_id}")
            work_item = self._row_to_work_item(row)
            if work_item.state is state:
                return work_item
            updated = work_item.transition_to(state, at=now)
            cursor = connection.execute(
                "UPDATE work_items SET state = ?, updated_at = ? "
                "WHERE work_item_id = ? AND state = ?",
                (state.value, updated.updated_at, work_item_id, work_item.state.value),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent work-item update detected: {work_item_id}")
            self._insert_work_item_event(
                connection,
                work_item_id,
                None,
                "work_item_state_changed",
                {"from": work_item.state.value, "to": state.value},
                now,
            )
        return updated

    def bind_codex_session(
        self, work_item_id: str, session_id: str, *, updated_at: str | None = None
    ) -> WorkItem:
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"work item not found: {work_item_id}")
            work_item = self._row_to_work_item(row)
            updated = work_item.bind_session(session_id, at=now)
            if updated is work_item:
                return work_item
            cursor = connection.execute(
                "UPDATE work_items SET codex_session_id = ?, updated_at = ? "
                "WHERE work_item_id = ? AND codex_session_id IS NULL",
                (session_id, now, work_item_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent session binding detected: {work_item_id}")
            self._insert_work_item_event(
                connection,
                work_item_id,
                None,
                "codex_session_bound",
                {"session_id": session_id},
                now,
            )
        return updated

    def bind_slack_thread(
        self,
        work_item_id: str,
        *,
        channel_id: str,
        thread_ts: str,
        updated_at: str | None = None,
    ) -> WorkItem:
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"work item not found: {work_item_id}")
            work_item = self._row_to_work_item(row)
            updated = work_item.bind_slack_thread(channel_id, thread_ts, at=now)
            if updated is work_item:
                return work_item
            cursor = connection.execute(
                "UPDATE work_items SET slack_channel_id = ?, slack_thread_ts = ?, updated_at = ? "
                "WHERE work_item_id = ? AND slack_channel_id IS NULL AND slack_thread_ts IS NULL",
                (channel_id, thread_ts, now, work_item_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent Slack binding detected: {work_item_id}")
            self._insert_work_item_event(
                connection,
                work_item_id,
                None,
                "slack_thread_bound",
                {"channel_id": channel_id, "thread_ts": thread_ts},
                now,
            )
        return updated

    def bind_draft_pr(
        self, work_item_id: str, pr_number: int, *, updated_at: str | None = None
    ) -> WorkItem:
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"work item not found: {work_item_id}")
            work_item = self._row_to_work_item(row)
            if type(pr_number) is not int or pr_number <= 0:
                raise ValueError("pr_number must be a positive integer")
            if work_item.pr_number is not None:
                if work_item.pr_number != pr_number:
                    raise ValueError("work item is already bound to a different Draft PR")
                return work_item
            cursor = connection.execute(
                "UPDATE work_items SET pr_number = ?, updated_at = ? "
                "WHERE work_item_id = ? AND pr_number IS NULL",
                (pr_number, now, work_item_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent Draft PR binding detected: {work_item_id}")
            self._insert_work_item_event(
                connection,
                work_item_id,
                None,
                "draft_pr_bound",
                {"pr_number": pr_number},
                now,
            )
        updated = self.get_work_item(work_item_id)
        assert updated is not None
        return updated

    def record_published_sha(
        self,
        work_item_id: str,
        *,
        previous_sha: str,
        head_sha: str,
        updated_at: str | None = None,
    ) -> WorkItem:
        previous_sha = validate_git_sha(previous_sha, "previous_sha")
        head_sha = validate_git_sha(head_sha, "head_sha")
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"work item not found: {work_item_id}")
            work_item = self._row_to_work_item(row)
            current_anchor = work_item.last_published_sha or work_item.base_sha
            if work_item.last_published_sha == head_sha:
                return work_item
            if previous_sha != current_anchor:
                raise ValueError("publication anchor no longer matches the work item")
            if head_sha == current_anchor:
                raise ValueError("published SHA must advance the work item anchor")
            cursor = connection.execute(
                "UPDATE work_items SET last_published_sha = ?, updated_at = ? "
                "WHERE work_item_id = ? AND "
                "((last_published_sha IS NULL AND base_sha = ?) OR last_published_sha = ?)",
                (head_sha, now, work_item_id, previous_sha, previous_sha),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent publication update detected: {work_item_id}")
            self._insert_work_item_event(
                connection,
                work_item_id,
                None,
                "commit_published",
                {"head_sha": head_sha, "previous_sha": previous_sha},
                now,
            )
        updated = self.get_work_item(work_item_id)
        assert updated is not None
        return updated

    def plan_turn(
        self,
        work_item_id: str,
        *,
        issue_revision: str,
        prompt_sha256: str,
        input_head_sha: str,
        included_comment_ids: tuple[str, ...] = (),
        turn_id: str | None = None,
        created_at: str | None = None,
    ) -> Turn:
        """Allocate the next ordered Turn while enforcing one active Turn globally."""
        now = created_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"work item not found: {work_item_id}")
            work_item = self._row_to_work_item(row)
            if work_item.state is not WorkItemState.READY:
                raise ValueError("work item must be ready before planning a Turn")
            next_number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(turn_number), 0) + 1 FROM turns WHERE work_item_id = ?",
                    (work_item_id,),
                ).fetchone()[0]
            )
            turn = Turn.new(
                turn_id=turn_id,
                work_item_id=work_item_id,
                turn_number=next_number,
                issue_revision=issue_revision,
                prompt_sha256=prompt_sha256,
                input_head_sha=input_head_sha,
                included_comment_ids=included_comment_ids,
                at=now,
            )
            self._insert_turn(connection, turn)
            self._insert_work_item_event(
                connection,
                work_item_id,
                turn.turn_id,
                "turn_planned",
                {
                    "input_head_sha": input_head_sha,
                    "issue_revision": issue_revision,
                    "prompt_sha256": prompt_sha256,
                    "turn_number": next_number,
                },
                now,
            )
        return turn

    def begin_turn(
        self,
        work_item_id: str,
        *,
        issue_revision: str,
        prompt_sha256: str,
        input_head_sha: str,
        included_comment_ids: tuple[str, ...] = (),
        expected_turn_number: int | None = None,
        turn_id: str | None = None,
        created_at: str | None = None,
    ) -> tuple[WorkItem, Turn]:
        """Atomically mark a ready WorkItem running and allocate its next Turn."""
        now = created_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"work item not found: {work_item_id}")
            work_item = self._row_to_work_item(row)
            if work_item.state is not WorkItemState.READY:
                raise ValueError("work item must be ready before beginning a Turn")
            next_number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(turn_number), 0) + 1 FROM turns WHERE work_item_id = ?",
                    (work_item_id,),
                ).fetchone()[0]
            )
            if expected_turn_number is not None and (
                type(expected_turn_number) is not int or expected_turn_number <= 0
            ):
                raise ValueError("expected_turn_number must be a positive integer or None")
            if expected_turn_number is not None and next_number != expected_turn_number:
                raise RuntimeError("Turn number changed after the Prompt snapshot was built")
            turn = Turn.new(
                turn_id=turn_id,
                work_item_id=work_item_id,
                turn_number=next_number,
                issue_revision=issue_revision,
                prompt_sha256=prompt_sha256,
                input_head_sha=input_head_sha,
                included_comment_ids=included_comment_ids,
                at=now,
            )
            self._insert_turn(connection, turn)
            cursor = connection.execute(
                "UPDATE work_items SET state = ?, updated_at = ? "
                "WHERE work_item_id = ? AND state = ?",
                (
                    WorkItemState.RUNNING.value,
                    now,
                    work_item_id,
                    WorkItemState.READY.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent WorkItem Turn start detected: {work_item_id}")
            self._insert_work_item_event(
                connection,
                work_item_id,
                turn.turn_id,
                "turn_begun",
                {
                    "input_head_sha": input_head_sha,
                    "issue_revision": issue_revision,
                    "prompt_sha256": prompt_sha256,
                    "turn_number": next_number,
                },
                now,
            )
        running = work_item.transition_to(WorkItemState.RUNNING, at=now)
        return running, turn

    def get_turn(self, turn_id: str) -> Turn | None:
        row = self._connection.execute(
            "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        return self._row_to_turn(row) if row is not None else None

    def list_turns(self, work_item_id: str) -> tuple[Turn, ...]:
        return tuple(
            self._row_to_turn(row)
            for row in self._connection.execute(
                "SELECT * FROM turns WHERE work_item_id = ? ORDER BY turn_number",
                (work_item_id,),
            )
        )

    def update_turn_state(
        self, turn_id: str, state: TurnState, *, updated_at: str | None = None
    ) -> Turn:
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM turns WHERE turn_id = ?", (turn_id,)).fetchone()
            if row is None:
                raise KeyError(f"turn not found: {turn_id}")
            turn = self._row_to_turn(row)
            if turn.state is state:
                return turn
            updated = turn.transition_to(state, at=now)
            cursor = connection.execute(
                "UPDATE turns SET state = ?, started_at = ?, finished_at = ?, updated_at = ? "
                "WHERE turn_id = ? AND state = ?",
                (
                    updated.state.value,
                    updated.started_at,
                    updated.finished_at,
                    updated.updated_at,
                    turn_id,
                    turn.state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent Turn update detected: {turn_id}")
            self._insert_work_item_event(
                connection,
                turn.work_item_id,
                turn_id,
                "turn_state_changed",
                {"from": turn.state.value, "to": state.value},
                now,
            )
        return updated

    def record_turn_result(
        self,
        turn_id: str,
        *,
        output_sha256: str,
        output_head_sha: str,
        result_status: str,
        result_summary: str,
        error_code: str | None = None,
        updated_at: str | None = None,
    ) -> Turn:
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM turns WHERE turn_id = ?", (turn_id,)).fetchone()
            if row is None:
                raise KeyError(f"turn not found: {turn_id}")
            turn = self._row_to_turn(row)
            if turn.state is TurnState.PLANNED:
                raise ValueError("a planned Turn cannot record an execution result")
            candidate = Turn(
                turn_id=turn.turn_id,
                work_item_id=turn.work_item_id,
                turn_number=turn.turn_number,
                state=turn.state,
                issue_revision=turn.issue_revision,
                prompt_sha256=turn.prompt_sha256,
                input_head_sha=turn.input_head_sha,
                output_sha256=output_sha256,
                output_head_sha=output_head_sha,
                result_status=result_status,
                result_summary=result_summary,
                error_code=error_code,
                started_at=turn.started_at,
                finished_at=turn.finished_at,
                included_comment_ids=turn.included_comment_ids,
                created_at=turn.created_at,
                updated_at=now,
            )
            if turn.output_sha256 is not None:
                if (
                    turn.output_sha256 != candidate.output_sha256
                    or turn.output_head_sha != candidate.output_head_sha
                    or turn.result_status != candidate.result_status
                    or turn.result_summary != candidate.result_summary
                    or turn.error_code != candidate.error_code
                ):
                    raise ValueError("Turn already has a different recorded result")
                return turn
            cursor = connection.execute(
                "UPDATE turns SET output_sha256 = ?, output_head_sha = ?, result_status = ?, "
                "result_summary = ?, error_code = ?, "
                "updated_at = ? WHERE turn_id = ? AND output_sha256 IS NULL",
                (
                    output_sha256,
                    output_head_sha,
                    result_status,
                    result_summary,
                    error_code,
                    now,
                    turn_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent Turn result update detected: {turn_id}")
            self._insert_work_item_event(
                connection,
                turn.work_item_id,
                turn_id,
                "turn_result_recorded",
                {
                    "error_code": error_code,
                    "output_head_sha": output_head_sha,
                    "output_sha256": output_sha256,
                    "result_status": result_status,
                },
                now,
            )
        return candidate

    def record_turn_error(
        self,
        turn_id: str,
        *,
        error_code: str,
        updated_at: str | None = None,
    ) -> Turn:
        """Persist one bounded machine error before terminalizing a failed Turn."""
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"turn not found: {turn_id}")
            turn = self._row_to_turn(row)
            if turn.state is TurnState.PLANNED:
                raise ValueError("a planned Turn cannot record an execution error")
            candidate = replace(turn, error_code=error_code, updated_at=now)
            if turn.error_code is not None:
                if turn.error_code != candidate.error_code:
                    raise ValueError("Turn already has a different recorded error")
                return turn
            cursor = connection.execute(
                "UPDATE turns SET error_code = ?, updated_at = ? "
                "WHERE turn_id = ? AND error_code IS NULL",
                (candidate.error_code, now, turn_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent Turn error update detected: {turn_id}")
            self._insert_work_item_event(
                connection,
                turn.work_item_id,
                turn_id,
                "turn_error_recorded",
                {"error_code": candidate.error_code},
                now,
            )
        return candidate

    def finalize_turn(
        self,
        turn_id: str,
        *,
        turn_state: TurnState,
        work_item_state: WorkItemState,
        updated_at: str | None = None,
    ) -> tuple[WorkItem, Turn]:
        """Atomically finalize a Turn and move its WorkItem to the matching state."""
        if turn_state not in {TurnState.FINISHED, TurnState.NEEDS_INPUT, TurnState.BLOCKED}:
            raise ValueError("turn_state must be a supported final outcome")
        if work_item_state not in {
            WorkItemState.REVIEW,
            WorkItemState.WAITING_INPUT,
            WorkItemState.BLOCKED,
        }:
            raise ValueError("work_item_state must be a supported final outcome")
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            turn_row = connection.execute(
                "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if turn_row is None:
                raise KeyError(f"turn not found: {turn_id}")
            turn = self._row_to_turn(turn_row)
            work_item_row = connection.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?", (turn.work_item_id,)
            ).fetchone()
            assert work_item_row is not None
            work_item = self._row_to_work_item(work_item_row)
            if turn.state is turn_state and work_item.state is work_item_state:
                return work_item, turn
            required_result = {
                TurnState.FINISHED: "completed",
                TurnState.NEEDS_INPUT: "needs_input",
            }.get(turn_state)
            if required_result is not None and turn.result_status != required_result:
                raise ValueError("Turn result does not match the requested final state")
            updated_turn = turn.transition_to(turn_state, at=now)
            updated_work_item = work_item.transition_to(work_item_state, at=now)
            turn_cursor = connection.execute(
                "UPDATE turns SET state = ?, started_at = ?, finished_at = ?, updated_at = ? "
                "WHERE turn_id = ? AND state = ?",
                (
                    turn_state.value,
                    updated_turn.started_at,
                    updated_turn.finished_at,
                    now,
                    turn_id,
                    turn.state.value,
                ),
            )
            work_item_cursor = connection.execute(
                "UPDATE work_items SET state = ?, updated_at = ? "
                "WHERE work_item_id = ? AND state = ?",
                (
                    work_item_state.value,
                    now,
                    work_item.work_item_id,
                    work_item.state.value,
                ),
            )
            if turn_cursor.rowcount != 1 or work_item_cursor.rowcount != 1:
                raise RuntimeError(f"concurrent Turn finalization detected: {turn_id}")
            self._insert_work_item_event(
                connection,
                work_item.work_item_id,
                turn_id,
                "turn_finalized",
                {
                    "turn_state": turn_state.value,
                    "work_item_state": work_item_state.value,
                },
                now,
            )
        return updated_work_item, updated_turn

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        event_time: str | None = None,
    ) -> int:
        if not event_type:
            raise ValueError("event_type must be non-empty")
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._transaction() as connection:
            cursor = self._insert_event_json(
                connection, run_id, event_type, payload_json, event_time or utc_now_iso()
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    @staticmethod
    def _insert_turn(connection: sqlite3.Connection, turn: Turn) -> None:
        fields = (
            "turn_id",
            "work_item_id",
            "turn_number",
            "state",
            "issue_revision",
            "prompt_sha256",
            "input_head_sha",
            "included_comment_ids_json",
            "output_sha256",
            "output_head_sha",
            "result_status",
            "result_summary",
            "error_code",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
        )
        values = tuple(
            turn.state.value
            if field == "state"
            else json.dumps(turn.included_comment_ids, separators=(",", ":"))
            if field == "included_comment_ids_json"
            else getattr(turn, field)
            for field in fields
        )
        connection.execute(
            f"INSERT INTO turns ({', '.join(fields)}) "
            f"VALUES ({', '.join('?' for _ in fields)})",
            values,
        )

    @staticmethod
    def _insert_work_item_event(
        connection: sqlite3.Connection,
        work_item_id: str,
        turn_id: str | None,
        event_type: str,
        payload: Mapping[str, Any],
        event_time: str,
    ) -> None:
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        connection.execute(
            "INSERT INTO work_item_events"
            "(work_item_id, turn_id, event_type, event_time, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (work_item_id, turn_id, event_type, event_time, payload_json),
        )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        event_time: str,
    ) -> sqlite3.Cursor:
        payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return StateStore._insert_event_json(
            connection, run_id, event_type, payload_json, event_time
        )

    @staticmethod
    def _insert_event_json(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload_json: str,
        event_time: str,
    ) -> sqlite3.Cursor:
        return connection.execute(
            "INSERT INTO run_events"
            "(run_id, event_type, event_time, payload_json) VALUES (?, ?, ?, ?)",
            (run_id, event_type, event_time, payload_json),
        )

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(destination)) as destination_connection:
            self._connection.backup(destination_connection)

    def integrity_check(self) -> str:
        result = self._connection.execute("PRAGMA integrity_check").fetchone()
        if result is None:
            raise RuntimeError("SQLite integrity check returned no result")
        return str(result[0])

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> Run:
        values = dict(row)
        values["state"] = RunState(values["state"])
        return Run(**values)

    @staticmethod
    def _row_to_work_item(row: sqlite3.Row) -> WorkItem:
        values = dict(row)
        values["state"] = WorkItemState(values["state"])
        values["task_branch_source"] = TaskBranchSource(values["task_branch_source"])
        return WorkItem(**values)

    @staticmethod
    def _row_to_turn(row: sqlite3.Row) -> Turn:
        values = dict(row)
        values["state"] = TurnState(values["state"])
        raw_comment_ids = values.pop("included_comment_ids_json", "[]")
        try:
            parsed_comment_ids = json.loads(raw_comment_ids)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("persisted Turn comment IDs are malformed") from exc
        if not isinstance(parsed_comment_ids, list) or any(
            not isinstance(item, str) for item in parsed_comment_ids
        ):
            raise ValueError("persisted Turn comment IDs are malformed")
        values["included_comment_ids"] = tuple(parsed_comment_ids)
        return Turn(**values)
