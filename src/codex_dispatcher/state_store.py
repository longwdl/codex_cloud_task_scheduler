"""SQLite-backed, recoverable run state."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any

from codex_dispatcher.domain import Run, RunState, utc_now_iso


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
            cursor = connection.execute(
                "INSERT INTO run_events"
                "(run_id, event_type, event_time, payload_json) VALUES (?, ?, ?, ?)",
                (run_id, event_type, event_time or utc_now_iso(), payload_json),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(destination) as destination_connection:
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
