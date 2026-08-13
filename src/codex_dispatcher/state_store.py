"""SQLite-backed, recoverable run state."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any

from codex_dispatcher.domain import Run, RunState, utc_now_iso


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
        migration_sql = (
            files("codex_dispatcher.migrations")
            .joinpath("001_initial.sql")
            .read_text(encoding="utf-8")
        )
        with self._transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 1"
            ).fetchone()
            if applied is None:
                for statement in migration_sql.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, utc_now_iso()),
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
