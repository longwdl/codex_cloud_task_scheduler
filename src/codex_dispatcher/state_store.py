"""SQLite-backed, recoverable run state."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import replace
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Any

from codex_dispatcher.domain import Run, RunState, utc_now_iso
from codex_dispatcher.delegation_evidence import (
    DelegationReceipt,
    delegation_receipt_to_json,
    parse_delegation_receipt,
)
from codex_dispatcher.completion_gate import (
    CompletionGateSnapshot,
    CompletionGateStatus,
)
from codex_dispatcher.handoffs import (
    PublishedCheckpoint,
    SessionHandoffSnapshot,
    validate_handoff_id,
)
from codex_dispatcher.runner_protocol import (
    AgentResult,
    agent_result_to_json,
    parse_agent_result,
)
from codex_dispatcher.runner_transport import RunnerArchiveReply, RunnerArchiveState
from codex_dispatcher.slack_reporting import (
    SlackDeliveryReceipt,
    SlackDeliveryRecord,
    SlackDeliveryState,
    SlackReport,
    SlackReportKind,
)
from codex_dispatcher.work_items import (
    ACTIVE_TURN_STATES,
    PRE_SESSION_RETRY_ROTATION_REASON,
    PromptKind,
    SessionGeneration,
    SessionGenerationRole,
    SessionGenerationState,
    TaskBranchSource,
    Turn,
    TurnContextFailureReceipt,
    TurnPromptInput,
    TurnState,
    TurnUsage,
    WorkItem,
    WorkItemState,
    validate_git_sha,
    validate_session_generation_id,
    validate_session_id,
    validate_sha256,
)
from codex_dispatcher.work_item_lifecycle import (
    WorkItemAbsenceReconciliation,
    WorkItemArchive,
    WorkItemArchiveStatus,
    WorkItemDisposition,
    WorkItemDispositionKind,
    validate_archive_error_code,
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
                statement = ""
                for line in migration_sql.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise RuntimeError(f"SQLite migration {version:03d} is incomplete")
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

    def get_work_item_completed_at(self, work_item_id: str) -> str:
        work_item = self.get_work_item(work_item_id)
        if work_item is None:
            raise KeyError(f"work item not found: {work_item_id}")
        if work_item.state is not WorkItemState.COMPLETED:
            raise ValueError("work item is not completed")
        payload = json.dumps(
            {"from": WorkItemState.REVIEW.value, "to": WorkItemState.COMPLETED.value},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        row = self._connection.execute(
            "SELECT event_time FROM work_item_events WHERE work_item_id = ? "
            "AND event_type = 'work_item_state_changed' AND payload_json = ? "
            "ORDER BY event_id DESC LIMIT 1",
            (work_item_id, payload),
        ).fetchone()
        if row is None:
            raise RuntimeError("completed WorkItem has no durable completion event")
        return str(row["event_time"])

    def get_work_item_archive(self, work_item_id: str) -> WorkItemArchive | None:
        row = self._connection.execute(
            "SELECT * FROM work_item_archives WHERE work_item_id = ?",
            (work_item_id,),
        ).fetchone()
        return self._row_to_work_item_archive(row) if row is not None else None

    def get_work_item_disposition(
        self, work_item_id: str
    ) -> WorkItemDisposition | None:
        row = self._connection.execute(
            "SELECT * FROM work_item_dispositions WHERE work_item_id = ?",
            (work_item_id,),
        ).fetchone()
        return self._row_to_work_item_disposition(row) if row is not None else None

    def list_work_item_dispositions(self) -> tuple[WorkItemDisposition, ...]:
        return tuple(
            self._row_to_work_item_disposition(row)
            for row in self._connection.execute(
                "SELECT * FROM work_item_dispositions ORDER BY created_at, work_item_id"
            )
        )

    def get_work_item_absence_reconciliation(
        self, work_item_id: str
    ) -> WorkItemAbsenceReconciliation | None:
        row = self._connection.execute(
            "SELECT * FROM work_item_absence_reconciliations WHERE work_item_id = ?",
            (work_item_id,),
        ).fetchone()
        return (
            self._row_to_work_item_absence_reconciliation(row)
            if row is not None
            else None
        )

    def record_work_item_absence_reconciliation(
        self,
        work_item_id: str,
        *,
        expected_head_sha: str,
        evidence_sha256: str,
        observed_by: str,
        observed_at: str,
        created_at: str | None = None,
    ) -> WorkItemAbsenceReconciliation:
        now = created_at or utc_now_iso()
        candidate = WorkItemAbsenceReconciliation(
            work_item_id,
            expected_head_sha,
            evidence_sha256,
            observed_by,
            observed_at,
            now,
        )
        with self._transaction() as connection:
            archive_row = connection.execute(
                "SELECT * FROM work_item_archives WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            if archive_row is None:
                raise ValueError(
                    "absence reconciliation requires a durable archive request"
                )
            archive = self._row_to_work_item_archive(archive_row)
            if archive.expected_head_sha != expected_head_sha:
                raise ValueError("absence reconciliation archive identity conflicts")
            if archive.status is WorkItemArchiveStatus.ARCHIVED:
                raise ValueError("archived WorkItem does not require absence reconciliation")
            work_item = self._require_work_item(connection, work_item_id)
            disposition = connection.execute(
                "SELECT expected_head_sha FROM work_item_dispositions "
                "WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            terminal = (
                work_item.state is WorkItemState.COMPLETED
                and work_item.last_published_sha == expected_head_sha
            ) or (
                disposition is not None
                and disposition["expected_head_sha"] == expected_head_sha
            )
            if not terminal:
                raise ValueError("absence reconciliation requires a terminal WorkItem")
            if connection.execute(
                "SELECT 1 FROM turns WHERE work_item_id = ? AND state IN "
                "('planned', 'starting', 'running', 'reconciling', "
                "'checkpointing', 'published') LIMIT 1",
                (work_item_id,),
            ).fetchone() is not None:
                raise ValueError("absence reconciliation cannot hide an active Turn")
            if connection.execute(
                "SELECT 1 FROM session_generations WHERE work_item_id = ? "
                "AND state IN ('planned', 'starting', 'active', 'retiring') LIMIT 1",
                (work_item_id,),
            ).fetchone() is not None:
                raise ValueError(
                    "absence reconciliation cannot hide a live session generation"
                )
            existing_row = connection.execute(
                "SELECT * FROM work_item_absence_reconciliations "
                "WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._row_to_work_item_absence_reconciliation(existing_row)
                if (
                    existing.expected_head_sha != expected_head_sha
                    or existing.evidence_sha256 != evidence_sha256
                    or existing.observed_by != observed_by
                    or existing.observed_at != observed_at
                ):
                    raise ValueError(
                        "absence reconciliation conflicts with durable evidence"
                    )
                return existing
            connection.execute(
                "INSERT INTO work_item_absence_reconciliations "
                "(work_item_id, expected_head_sha, evidence_sha256, observed_by, "
                "observed_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    work_item_id,
                    expected_head_sha,
                    evidence_sha256,
                    observed_by,
                    observed_at,
                    now,
                ),
            )
            self._insert_work_item_event(
                connection,
                work_item_id,
                None,
                "work_item_absence_reconciled",
                {
                    "evidence_sha256": evidence_sha256,
                    "expected_head_sha": expected_head_sha,
                    "observed_at": observed_at,
                    "observed_by": observed_by,
                },
                now,
            )
        reconciliation = self.get_work_item_absence_reconciliation(work_item_id)
        assert reconciliation is not None
        return reconciliation

    def record_work_item_disposition(
        self,
        work_item_id: str,
        *,
        kind: WorkItemDispositionKind,
        expected_head_sha: str,
        pr_number: int | None,
        requested_by: str,
        request_event_id: str,
        requested_at: str,
        reason_code: str,
        updated_at: str | None = None,
    ) -> WorkItemDisposition:
        now = updated_at or utc_now_iso()
        disposition_request_sha256 = sha256(
            json.dumps(
                {
                    "expected_head_sha": expected_head_sha,
                    "kind": kind.value,
                    "pr_number": pr_number,
                    "reason_code": reason_code,
                    "request_event_id": request_event_id,
                    "requested_at": requested_at,
                    "requested_by": requested_by,
                    "work_item_id": work_item_id,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        candidate = WorkItemDisposition(
            work_item_id,
            kind,
            expected_head_sha,
            pr_number,
            requested_by,
            request_event_id,
            requested_at,
            reason_code,
            disposition_request_sha256,
            now,
            now,
            now,
        )
        with self._transaction() as connection:
            work_item = self._require_work_item(connection, work_item_id)
            if work_item.state is WorkItemState.COMPLETED:
                raise ValueError("completed WorkItems cannot receive a disposition")
            if work_item.state is WorkItemState.RUNNING:
                raise ValueError("running WorkItems cannot receive a disposition")
            if (work_item.last_published_sha or work_item.base_sha) != expected_head_sha:
                raise ValueError("disposition HEAD conflicts with the WorkItem checkpoint")
            if kind is WorkItemDispositionKind.ABANDONED:
                if pr_number is not None or work_item.pr_number is not None:
                    raise ValueError("abandoned disposition conflicts with a Pull Request")
            elif (
                type(pr_number) is not int
                or pr_number <= 0
                or work_item.pr_number not in {None, pr_number}
            ):
                raise ValueError("superseded disposition Pull Request conflicts")
            if connection.execute(
                "SELECT 1 FROM turns WHERE work_item_id = ? AND state IN "
                "('planned', 'starting', 'running', 'reconciling', "
                "'checkpointing', 'published') LIMIT 1",
                (work_item_id,),
            ).fetchone() is not None:
                raise ValueError("WorkItem disposition cannot begin during an active Turn")
            existing_row = connection.execute(
                "SELECT * FROM work_item_dispositions WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._row_to_work_item_disposition(existing_row)
                if existing != candidate and (
                    existing.kind != kind
                    or existing.expected_head_sha != expected_head_sha
                    or existing.pr_number != pr_number
                    or existing.requested_by != requested_by
                    or existing.request_event_id != request_event_id
                    or existing.requested_at != requested_at
                    or existing.reason_code != reason_code
                    or existing.request_sha256 != disposition_request_sha256
                ):
                    raise ValueError("disposition conflicts with durable operator intent")
                return existing
            self._terminalize_session_generations(
                connection,
                work_item_id,
                work_item=work_item,
                at=now,
                reason="work_item_disposed",
                allow_cancel_unstarted=True,
            )
            connection.execute(
                "INSERT INTO work_item_dispositions "
                "(work_item_id, kind, expected_head_sha, pr_number, requested_by, "
                "request_event_id, requested_at, reason_code, request_sha256, eligible_at, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    work_item_id,
                    kind.value,
                    expected_head_sha,
                    pr_number,
                    requested_by,
                    request_event_id,
                    requested_at,
                    reason_code,
                    disposition_request_sha256,
                    now,
                    now,
                    now,
                ),
            )
            self._insert_work_item_event(
                connection,
                work_item_id,
                None,
                "work_item_disposed",
                {
                    "expected_head_sha": expected_head_sha,
                    "kind": kind.value,
                    "pr_number": pr_number,
                    "reason_code": reason_code,
                    "request_event_id": request_event_id,
                    "request_sha256": disposition_request_sha256,
                    "requested_by": requested_by,
                    "requested_at": requested_at,
                },
                now,
            )
        disposition = self.get_work_item_disposition(work_item_id)
        assert disposition is not None
        return disposition

    def prepare_work_item_archive(
        self,
        work_item_id: str,
        *,
        expected_head_sha: str,
        eligible_at: str,
        request_sha256: str,
        updated_at: str | None = None,
    ) -> WorkItemArchive:
        now = updated_at or utc_now_iso()
        candidate = WorkItemArchive(
            work_item_id,
            WorkItemArchiveStatus.PREPARED,
            expected_head_sha,
            eligible_at,
            request_sha256,
            None,
            None,
            None,
            None,
            None,
            now,
            now,
        )
        with self._transaction() as connection:
            work_item_row = connection.execute(
                "SELECT state, base_sha, last_published_sha FROM work_items "
                "WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            if work_item_row is None:
                raise KeyError(f"work item not found: {work_item_id}")
            disposition_row = connection.execute(
                "SELECT expected_head_sha FROM work_item_dispositions "
                "WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            completed_checkpoint = (
                WorkItemState(work_item_row["state"]) is WorkItemState.COMPLETED
                and work_item_row["last_published_sha"] == expected_head_sha
            )
            disposed_checkpoint = (
                disposition_row is not None
                and disposition_row["expected_head_sha"] == expected_head_sha
                and (work_item_row["last_published_sha"] or work_item_row["base_sha"])
                == expected_head_sha
            )
            if not completed_checkpoint and not disposed_checkpoint:
                raise ValueError(
                    "archive requires a terminal WorkItem at its exact checkpoint"
                )
            placeholders = ", ".join("?" for _ in ACTIVE_TURN_STATES)
            if connection.execute(
                f"SELECT 1 FROM turns WHERE state IN ({placeholders}) LIMIT 1",
                tuple(state.value for state in ACTIVE_TURN_STATES),
            ).fetchone() is not None:
                raise ValueError("archive cannot begin while a Turn is active")
            if connection.execute(
                "SELECT 1 FROM session_generations WHERE work_item_id = ? "
                "AND state IN ('planned', 'starting', 'active', 'retiring') LIMIT 1",
                (work_item_id,),
            ).fetchone() is not None:
                raise ValueError("archive cannot begin while a session generation is live")
            existing_row = connection.execute(
                "SELECT * FROM work_item_archives WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._row_to_work_item_archive(existing_row)
                if (
                    existing.expected_head_sha != expected_head_sha
                    or existing.eligible_at != eligible_at
                    or existing.request_sha256 != request_sha256
                ):
                    raise ValueError("archive request conflicts with durable lifecycle identity")
                return existing
            connection.execute(
                "INSERT INTO work_item_archives "
                "(work_item_id, status, expected_head_sha, eligible_at, request_sha256, "
                "response_json, response_sha256, reclaimed_bytes, runner_archived_at, "
                "error_code, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?)",
                (
                    candidate.work_item_id,
                    candidate.status.value,
                    candidate.expected_head_sha,
                    candidate.eligible_at,
                    candidate.request_sha256,
                    now,
                    now,
                ),
            )
            self._insert_work_item_event(
                connection,
                work_item_id,
                None,
                "work_item_archive_prepared",
                {
                    "expected_head_sha": expected_head_sha,
                    "eligible_at": eligible_at,
                    "request_sha256": request_sha256,
                },
                now,
            )
        archived = self.get_work_item_archive(work_item_id)
        assert archived is not None
        return archived

    def mark_work_item_archive_ambiguous(
        self, work_item_id: str, *, updated_at: str | None = None
    ) -> WorkItemArchive:
        return self._transition_work_item_archive(
            work_item_id,
            from_statuses={WorkItemArchiveStatus.PREPARED, WorkItemArchiveStatus.AMBIGUOUS},
            to_status=WorkItemArchiveStatus.AMBIGUOUS,
            updated_at=updated_at,
        )

    def mark_work_item_archive_retry_ready(
        self, work_item_id: str, *, updated_at: str | None = None
    ) -> WorkItemArchive:
        return self._transition_work_item_archive(
            work_item_id,
            from_statuses={WorkItemArchiveStatus.AMBIGUOUS},
            to_status=WorkItemArchiveStatus.PREPARED,
            updated_at=updated_at,
        )

    def block_work_item_archive(
        self,
        work_item_id: str,
        *,
        error_code: str,
        updated_at: str | None = None,
    ) -> WorkItemArchive:
        error_code = validate_archive_error_code(error_code)
        return self._transition_work_item_archive(
            work_item_id,
            from_statuses={WorkItemArchiveStatus.PREPARED, WorkItemArchiveStatus.AMBIGUOUS},
            to_status=WorkItemArchiveStatus.BLOCKED,
            error_code=error_code,
            updated_at=updated_at,
        )

    def complete_work_item_archive(
        self,
        work_item_id: str,
        *,
        reply: RunnerArchiveReply,
        updated_at: str | None = None,
    ) -> WorkItemArchive:
        if (
            not isinstance(reply, RunnerArchiveReply)
            or reply.state is not RunnerArchiveState.ARCHIVED
            or reply.work_item_id != work_item_id
            or reply.archived_at is None
            or reply.reclaimed_bytes is None
        ):
            raise ValueError("archive completion requires an exact archived Runner reply")
        now = updated_at or utc_now_iso()
        response_json = reply.to_json()
        response_sha256 = sha256(response_json.encode("utf-8")).hexdigest()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM work_item_archives WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"work item archive not found: {work_item_id}")
            current = self._row_to_work_item_archive(row)
            if current.status is WorkItemArchiveStatus.ARCHIVED:
                if current.response_sha256 != response_sha256:
                    raise ValueError("archive receipt conflicts with durable completion")
                return current
            if current.status not in {
                WorkItemArchiveStatus.PREPARED,
                WorkItemArchiveStatus.AMBIGUOUS,
            } or current.expected_head_sha != reply.expected_head_sha:
                raise ValueError("archive completion conflicts with lifecycle state")
            connection.execute(
                "UPDATE work_item_archives SET status = ?, response_json = ?, "
                "response_sha256 = ?, reclaimed_bytes = ?, runner_archived_at = ?, "
                "error_code = NULL, updated_at = ? WHERE work_item_id = ?",
                (
                    WorkItemArchiveStatus.ARCHIVED.value,
                    response_json,
                    response_sha256,
                    reply.reclaimed_bytes,
                    reply.archived_at,
                    now,
                    work_item_id,
                ),
            )
            self._insert_work_item_event(
                connection,
                work_item_id,
                None,
                "work_item_archived",
                {
                    "expected_head_sha": reply.expected_head_sha,
                    "reclaimed_bytes": reply.reclaimed_bytes,
                    "response_sha256": response_sha256,
                },
                now,
            )
        archived = self.get_work_item_archive(work_item_id)
        assert archived is not None
        return archived

    def _transition_work_item_archive(
        self,
        work_item_id: str,
        *,
        from_statuses: set[WorkItemArchiveStatus],
        to_status: WorkItemArchiveStatus,
        error_code: str | None = None,
        updated_at: str | None = None,
    ) -> WorkItemArchive:
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM work_item_archives WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"work item archive not found: {work_item_id}")
            current = self._row_to_work_item_archive(row)
            if current.status is to_status:
                if current.error_code != error_code:
                    raise ValueError("archive transition conflicts with durable state")
                return current
            if current.status not in from_statuses:
                raise ValueError("archive transition is not allowed")
            connection.execute(
                "UPDATE work_item_archives SET status = ?, error_code = ?, "
                "updated_at = ? WHERE work_item_id = ?",
                (to_status.value, error_code, now, work_item_id),
            )
            self._insert_work_item_event(
                connection,
                work_item_id,
                None,
                f"work_item_archive_{to_status.value}",
                ({"error_code": error_code} if error_code is not None else {}),
                now,
            )
        transitioned = self.get_work_item_archive(work_item_id)
        assert transitioned is not None
        return transitioned

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

    def runner_preparation_was_acknowledged(self, work_item_id: str) -> bool:
        """Return whether an exact Runner PREPARE ACK reached durable WorkItem state."""
        row = self._connection.execute(
            "SELECT 1 FROM work_items WHERE work_item_id = ?", (work_item_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"work item not found: {work_item_id}")
        prepared_transition = json.dumps(
            {
                "from": WorkItemState.PREPARING.value,
                "to": WorkItemState.READY.value,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            self._connection.execute(
                "SELECT 1 FROM work_item_events WHERE work_item_id = ? "
                "AND event_type = 'work_item_state_changed' AND payload_json = ? LIMIT 1",
                (work_item_id, prepared_transition),
            ).fetchone()
            is not None
        )

    def update_work_item_state(
        self, work_item_id: str, state: WorkItemState, *, updated_at: str | None = None
    ) -> WorkItem:
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM work_item_dispositions WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone() is not None:
                raise ValueError("disposed WorkItem state is immutable")
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
            if state is WorkItemState.COMPLETED:
                self._terminalize_session_generations(
                    connection,
                    work_item_id,
                    work_item=work_item,
                    at=now,
                    reason="work_item_completed",
                    allow_cancel_unstarted=False,
                )
        return updated

    def _terminalize_session_generations(
        self,
        connection: sqlite3.Connection,
        work_item_id: str,
        *,
        work_item: WorkItem,
        at: str,
        reason: str,
        allow_cancel_unstarted: bool,
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM session_generations WHERE work_item_id = ? "
            "AND state IN ('planned', 'starting', 'active', 'retiring') "
            "ORDER BY generation_number",
            (work_item_id,),
        ).fetchall()
        for row in rows:
            generation = self._row_to_session_generation(row)
            work_item_anchor = work_item.last_published_sha or work_item.base_sha
            generation_anchor = generation.last_published_sha or generation.start_head_sha
            if generation_anchor != work_item_anchor:
                raise ValueError(
                    "session generation checkpoint conflicts with terminal WorkItem"
                )
            if generation.state is SessionGenerationState.ACTIVE:
                updated = generation.transition_to(
                    SessionGenerationState.RETIRING, at=at
                ).transition_to(SessionGenerationState.RETIRED, at=at)
            elif generation.state is SessionGenerationState.RETIRING:
                updated = generation.transition_to(
                    SessionGenerationState.RETIRED, at=at
                )
            else:
                if not allow_cancel_unstarted:
                    raise ValueError(
                        "completed WorkItem has an unstarted session generation"
                    )
                if generation.state is SessionGenerationState.STARTING:
                    raise ValueError(
                        "starting session generation has an ambiguous remote identity"
                    )
                if (
                    generation.codex_session_id is not None
                    or generation.last_published_sha is not None
                    or connection.execute(
                        "SELECT 1 FROM turn_session_generations "
                        "WHERE session_generation_id = ? LIMIT 1",
                        (generation.session_generation_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError(
                        "planned session generation cannot be safely cancelled"
                    )
                updated = generation.transition_to(
                    SessionGenerationState.FAILED, at=at
                )
            cursor = connection.execute(
                "UPDATE session_generations SET state = ?, retired_at = ?, updated_at = ? "
                "WHERE session_generation_id = ? AND state = ?",
                (
                    updated.state.value,
                    updated.retired_at,
                    updated.updated_at,
                    generation.session_generation_id,
                    generation.state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("concurrent session generation terminalization detected")
            self._insert_work_item_event(
                connection,
                work_item_id,
                None,
                "session_generation_state_changed",
                {
                    "from": generation.state.value,
                    "reason": reason,
                    "session_generation_id": generation.session_generation_id,
                    "to": updated.state.value,
                },
                at,
            )

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

    def plan_session_generation(
        self,
        work_item_id: str,
        *,
        role: SessionGenerationRole,
        policy_sha256: str,
        start_head_sha: str | None = None,
        rotation_reason: str | None = None,
        session_generation_id: str | None = None,
        created_at: str | None = None,
    ) -> SessionGeneration:
        """Atomically allocate the next replaceable Codex session generation."""
        if not isinstance(role, SessionGenerationRole):
            raise ValueError("role must be a SessionGenerationRole")
        validate_sha256(policy_sha256, "policy_sha256")
        now = created_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"work item not found: {work_item_id}")
            work_item = self._row_to_work_item(row)
            expected_start_head = work_item.last_published_sha or work_item.base_sha
            if start_head_sha is not None and start_head_sha != expected_start_head:
                raise ValueError("session generation start head must match the WorkItem anchor")
            generation = SessionGeneration.new(
                session_generation_id=session_generation_id,
                work_item_id=work_item_id,
                generation_number=int(
                    connection.execute(
                        "SELECT COALESCE(MAX(generation_number), 0) + 1 "
                        "FROM session_generations WHERE work_item_id = ?",
                        (work_item_id,),
                    ).fetchone()[0]
                ),
                role=role,
                start_head_sha=expected_start_head,
                policy_sha256=policy_sha256,
                rotation_reason=rotation_reason,
                at=now,
            )
            self._insert_session_generation(connection, generation)
            self._insert_work_item_event(
                connection,
                work_item_id,
                None,
                "session_generation_planned",
                {
                    "generation_number": generation.generation_number,
                    "policy_sha256": generation.policy_sha256,
                    "role": generation.role.value,
                    "session_generation_id": generation.session_generation_id,
                    "start_head_sha": generation.start_head_sha,
                },
                now,
            )
        return generation

    def get_session_generation(self, session_generation_id: str) -> SessionGeneration | None:
        validate_session_generation_id(session_generation_id)
        row = self._connection.execute(
            "SELECT * FROM session_generations WHERE session_generation_id = ?",
            (session_generation_id,),
        ).fetchone()
        return self._row_to_session_generation(row) if row is not None else None

    def list_session_generations(self, work_item_id: str) -> tuple[SessionGeneration, ...]:
        if self.get_work_item(work_item_id) is None:
            raise KeyError(f"work item not found: {work_item_id}")
        return tuple(
            self._row_to_session_generation(row)
            for row in self._connection.execute(
                "SELECT * FROM session_generations WHERE work_item_id = ? "
                "ORDER BY generation_number",
                (work_item_id,),
            )
        )

    def get_live_session_generation(self, work_item_id: str) -> SessionGeneration | None:
        rows = self._connection.execute(
            "SELECT * FROM session_generations WHERE work_item_id = ? "
            "AND state IN ('planned', 'starting', 'active', 'retiring') "
            "ORDER BY generation_number LIMIT 2",
            (work_item_id,),
        ).fetchall()
        if len(rows) > 1:
            raise RuntimeError("database contains more than one live session generation")
        return self._row_to_session_generation(rows[0]) if rows else None

    def record_session_generation_baseline(
        self,
        session_generation_id: str,
        *,
        issue_revision: str,
        issue_content_sha256: str,
        task_spec_sha256: str,
        prompt_sha256: str,
        approved_comment_ids: tuple[str, ...],
        approved_context_sha256: str,
        updated_at: str | None = None,
    ) -> SessionGeneration:
        """Persist one planned generation's immutable prompt-input baseline."""
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM session_generations WHERE session_generation_id = ?",
                (session_generation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"session generation not found: {session_generation_id}")
            generation = self._row_to_session_generation(row)
            updated = generation.record_baseline(
                issue_revision=issue_revision,
                issue_content_sha256=issue_content_sha256,
                task_spec_sha256=task_spec_sha256,
                prompt_sha256=prompt_sha256,
                approved_comment_ids=approved_comment_ids,
                approved_context_sha256=approved_context_sha256,
                at=now,
            )
            if updated is generation:
                return generation
            cursor = connection.execute(
                "UPDATE session_generations SET baseline_issue_revision = ?, "
                "baseline_issue_content_sha256 = ?, baseline_task_spec_sha256 = ?, "
                "baseline_prompt_sha256 = ?, baseline_approved_comment_ids_json = ?, "
                "baseline_approved_context_sha256 = ?, updated_at = ? "
                "WHERE session_generation_id = ? AND state = 'planned' "
                "AND baseline_issue_revision IS NULL AND baseline_issue_content_sha256 IS NULL "
                "AND baseline_task_spec_sha256 IS NULL AND baseline_prompt_sha256 IS NULL "
                "AND baseline_approved_comment_ids_json IS NULL "
                "AND baseline_approved_context_sha256 IS NULL",
                (
                    updated.baseline_issue_revision,
                    updated.baseline_issue_content_sha256,
                    updated.baseline_task_spec_sha256,
                    updated.baseline_prompt_sha256,
                    json.dumps(updated.baseline_approved_comment_ids, separators=(",", ":")),
                    updated.baseline_approved_context_sha256,
                    updated.updated_at,
                    session_generation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"concurrent session generation baseline update detected: "
                    f"{session_generation_id}"
                )
            self._insert_work_item_event(
                connection,
                generation.work_item_id,
                None,
                "session_generation_baseline_recorded",
                {
                    "approved_comment_ids": updated.baseline_approved_comment_ids,
                    "baseline_approved_context_sha256": updated.baseline_approved_context_sha256,
                    "baseline_issue_content_sha256": updated.baseline_issue_content_sha256,
                    "baseline_issue_revision": updated.baseline_issue_revision,
                    "baseline_prompt_sha256": updated.baseline_prompt_sha256,
                    "baseline_task_spec_sha256": updated.baseline_task_spec_sha256,
                    "session_generation_id": session_generation_id,
                },
                now,
            )
        return updated

    def begin_session_generation_turn(
        self,
        work_item_id: str,
        *,
        session_generation_id: str,
        generation_number: int,
        policy_sha256: str,
        prompt_kind: PromptKind,
        issue_revision: str,
        issue_content_sha256: str,
        task_spec_sha256: str,
        prompt_sha256: str,
        approved_comment_ids: tuple[str, ...],
        approved_context_sha256: str,
        issue_allowed_paths: tuple[str, ...],
        input_head_sha: str,
        handoff_id: str | None = None,
        pre_session_retry_without_handoff: bool = False,
        expected_turn_number: int | None = None,
        turn_id: str | None = None,
        created_at: str | None = None,
    ) -> tuple[WorkItem, SessionGeneration, Turn, TurnPromptInput]:
        """Atomically snapshot/start a generation and its first or delta Turn."""
        if not isinstance(prompt_kind, PromptKind):
            raise ValueError("prompt_kind must be a PromptKind")
        if not issue_allowed_paths:
            raise ValueError("issue_allowed_paths must be non-empty for a generation Turn")
        if not isinstance(pre_session_retry_without_handoff, bool):
            raise ValueError("pre_session_retry_without_handoff must be a bool")
        validate_sha256(policy_sha256, "policy_sha256")
        validate_git_sha(input_head_sha, "input_head_sha")
        now = created_at or utc_now_iso()
        with self._transaction() as connection:
            work_item_row = connection.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)
            ).fetchone()
            if work_item_row is None:
                raise KeyError(f"work item not found: {work_item_id}")
            work_item = self._row_to_work_item(work_item_row)
            if work_item.state is not WorkItemState.READY:
                raise ValueError("work item must be ready before beginning a generation Turn")
            generation = self._require_generation_identity(
                connection,
                session_generation_id=session_generation_id,
                work_item_id=work_item_id,
                generation_number=generation_number,
                policy_sha256=policy_sha256,
            )
            anchor = generation.last_published_sha or generation.start_head_sha
            work_item_anchor = work_item.last_published_sha or work_item.base_sha
            if input_head_sha != anchor or input_head_sha != work_item_anchor:
                raise ValueError("generation Turn input head does not match the publication anchor")
            if generation.state is SessionGenerationState.PLANNED:
                if prompt_kind is not PromptKind.FULL:
                    raise ValueError("a planned SessionGeneration requires a full prompt")
                if generation.generation_number == 1:
                    if pre_session_retry_without_handoff:
                        raise ValueError(
                            "the first SessionGeneration cannot be a pre-session retry"
                        )
                    if handoff_id is not None:
                        raise ValueError("the first SessionGeneration cannot bind a handoff")
                else:
                    if handoff_id is None:
                        if not pre_session_retry_without_handoff:
                            raise ValueError(
                                "a replacement SessionGeneration requires a handoff"
                            )
                        self._require_pre_session_rejection_retry(
                            connection,
                            work_item=work_item,
                            generation=generation,
                        )
                        if generation.rotation_reason is None:
                            cursor = connection.execute(
                                "UPDATE session_generations SET rotation_reason = ?, "
                                "updated_at = ? WHERE session_generation_id = ? "
                                "AND state = 'planned' AND rotation_reason IS NULL",
                                (
                                    PRE_SESSION_RETRY_ROTATION_REASON,
                                    now,
                                    generation.session_generation_id,
                                ),
                            )
                            if cursor.rowcount != 1:
                                raise RuntimeError(
                                    "concurrent pre-session retry normalization detected"
                                )
                            generation = replace(
                                generation,
                                rotation_reason=PRE_SESSION_RETRY_ROTATION_REASON,
                                updated_at=now,
                            )
                    else:
                        if pre_session_retry_without_handoff:
                            raise ValueError(
                                "a handoff cannot also be a pre-session retry"
                            )
                        validate_handoff_id(handoff_id)
                        row = connection.execute(
                            "SELECT * FROM session_handoffs WHERE handoff_id = ? "
                            "AND to_session_generation_id = ?",
                            (handoff_id, generation.session_generation_id),
                        ).fetchone()
                        if row is None:
                            raise ValueError("replacement SessionGeneration handoff is missing")
                        handoff = self._row_to_session_handoff(row)
                        if (
                            handoff.work_item_id != work_item_id
                            or handoff.to_generation_number != generation.generation_number
                            or handoff.current_head_sha != input_head_sha
                            or handoff.issue_revision != issue_revision
                            or handoff.issue_content_sha256 != issue_content_sha256
                            or handoff.task_spec_sha256 != task_spec_sha256
                            or handoff.approved_context_sha256 != approved_context_sha256
                            or handoff.to_agent_policy_sha256 != policy_sha256
                        ):
                            raise ValueError(
                                "replacement SessionGeneration handoff conflicts with Turn inputs"
                            )
                baseline_was_recorded = generation.baseline_issue_revision is not None
                generation = generation.record_baseline(
                    issue_revision=issue_revision,
                    issue_content_sha256=issue_content_sha256,
                    task_spec_sha256=task_spec_sha256,
                    prompt_sha256=prompt_sha256,
                    approved_comment_ids=approved_comment_ids,
                    approved_context_sha256=approved_context_sha256,
                    at=now,
                ).transition_to(SessionGenerationState.STARTING, at=now)
                approved_ids_json = json.dumps(
                    generation.baseline_approved_comment_ids,
                    separators=(",", ":"),
                )
                if baseline_was_recorded:
                    cursor = connection.execute(
                        "UPDATE session_generations SET state = ?, started_at = ?, updated_at = ? "
                        "WHERE session_generation_id = ? AND state = 'planned' "
                        "AND baseline_issue_revision = ? "
                        "AND baseline_issue_content_sha256 = ? "
                        "AND baseline_task_spec_sha256 = ? AND baseline_prompt_sha256 = ? "
                        "AND baseline_approved_comment_ids_json = ? "
                        "AND baseline_approved_context_sha256 = ?",
                        (
                            generation.state.value,
                            generation.started_at,
                            generation.updated_at,
                            session_generation_id,
                            generation.baseline_issue_revision,
                            generation.baseline_issue_content_sha256,
                            generation.baseline_task_spec_sha256,
                            generation.baseline_prompt_sha256,
                            approved_ids_json,
                            generation.baseline_approved_context_sha256,
                        ),
                    )
                else:
                    cursor = connection.execute(
                        "UPDATE session_generations SET state = ?, baseline_issue_revision = ?, "
                        "baseline_issue_content_sha256 = ?, baseline_task_spec_sha256 = ?, "
                        "baseline_prompt_sha256 = ?, baseline_approved_comment_ids_json = ?, "
                        "baseline_approved_context_sha256 = ?, started_at = ?, updated_at = ? "
                        "WHERE session_generation_id = ? AND state = 'planned' "
                        "AND baseline_issue_revision IS NULL "
                        "AND baseline_issue_content_sha256 IS NULL "
                        "AND baseline_task_spec_sha256 IS NULL "
                        "AND baseline_prompt_sha256 IS NULL "
                        "AND baseline_approved_comment_ids_json IS NULL "
                        "AND baseline_approved_context_sha256 IS NULL",
                        (
                            generation.state.value,
                            generation.baseline_issue_revision,
                            generation.baseline_issue_content_sha256,
                            generation.baseline_task_spec_sha256,
                            generation.baseline_prompt_sha256,
                            approved_ids_json,
                            generation.baseline_approved_context_sha256,
                            generation.started_at,
                            generation.updated_at,
                            session_generation_id,
                        ),
                    )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "concurrent session generation baseline/start update detected"
                    )
            elif generation.state is SessionGenerationState.ACTIVE:
                if pre_session_retry_without_handoff:
                    raise ValueError(
                        "an active SessionGeneration cannot be a pre-session retry"
                    )
                if handoff_id is not None:
                    raise ValueError("an active SessionGeneration cannot replay a handoff")
                if prompt_kind is not PromptKind.DELTA:
                    raise ValueError("an active SessionGeneration requires a delta prompt")
                if generation.codex_session_id is None:
                    raise RuntimeError("active SessionGeneration has no Codex session")
                if (
                    generation.baseline_issue_content_sha256
                    != issue_content_sha256
                    or generation.baseline_task_spec_sha256 != task_spec_sha256
                ):
                    raise ValueError(
                        "active SessionGeneration semantic baseline has changed"
                    )
            else:
                raise ValueError("session generation is not ready to begin a Turn")
            next_number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(turn_number), 0) + 1 FROM turns WHERE work_item_id = ?",
                    (work_item_id,),
                ).fetchone()[0]
            )
            if expected_turn_number is not None and next_number != expected_turn_number:
                raise RuntimeError("Turn number changed after the Prompt snapshot was built")
            turn = Turn.new(
                turn_id=turn_id,
                work_item_id=work_item_id,
                turn_number=next_number,
                issue_revision=issue_revision,
                prompt_sha256=prompt_sha256,
                input_head_sha=input_head_sha,
                included_comment_ids=approved_comment_ids,
                issue_allowed_paths=issue_allowed_paths,
                at=now,
            ).transition_to(TurnState.STARTING, at=now)
            prompt_input = TurnPromptInput.new(
                turn_id=turn.turn_id,
                prompt_kind=prompt_kind,
                issue_content_sha256=issue_content_sha256,
                task_spec_sha256=task_spec_sha256,
                cumulative_approved_context_sha256=approved_context_sha256,
                at=now,
            )
            self._insert_turn(connection, turn)
            connection.execute(
                "INSERT INTO turn_session_generations(turn_id, session_generation_id) VALUES (?, ?)",
                (turn.turn_id, session_generation_id),
            )
            self._insert_turn_prompt_input(connection, prompt_input)
            if handoff_id is not None:
                connection.execute(
                    "INSERT INTO turn_handoff_bindings(turn_id, handoff_id) VALUES (?, ?)",
                    (turn.turn_id, handoff_id),
                )
            cursor = connection.execute(
                "UPDATE work_items SET state = ?, updated_at = ? "
                "WHERE work_item_id = ? AND state = 'ready'",
                (WorkItemState.RUNNING.value, now, work_item_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"concurrent WorkItem generation Turn start: {work_item_id}")
            self._insert_work_item_event(
                connection,
                work_item_id,
                turn.turn_id,
                "generation_turn_begun",
                {
                    "generation_number": generation.generation_number,
                    "handoff_id": handoff_id,
                    "pre_session_retry_without_handoff": (
                        pre_session_retry_without_handoff
                    ),
                    "prompt_kind": prompt_kind.value,
                    "session_generation_id": session_generation_id,
                    "turn_number": turn.turn_number,
                },
                now,
            )
        return work_item.transition_to(WorkItemState.RUNNING, at=now), generation, turn, prompt_input

    def _require_pre_session_rejection_retry(
        self,
        connection: sqlite3.Connection,
        *,
        work_item: WorkItem,
        generation: SessionGeneration,
    ) -> None:
        """Re-prove a handoff-free retry at the persistence boundary."""
        if (
            generation.generation_number <= 1
            or generation.state is not SessionGenerationState.PLANNED
            or generation.role is not SessionGenerationRole.IMPLEMENTATION
            or generation.codex_session_id is not None
            or generation.last_published_sha is not None
            or generation.start_head_sha != work_item.base_sha
            or generation.rotation_reason
            not in {None, PRE_SESSION_RETRY_ROTATION_REASON}
            or work_item.last_published_sha is not None
        ):
            raise ValueError("pre-session retry generation is not mechanically safe")
        publication_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM publication_checkpoints "
                "JOIN turns USING(turn_id) WHERE turns.work_item_id = ?",
                (work_item.work_item_id,),
            ).fetchone()[0]
        )
        handoff_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM session_handoffs WHERE work_item_id = ?",
                (work_item.work_item_id,),
            ).fetchone()[0]
        )
        if publication_count != 0 or handoff_count != 0:
            raise ValueError("pre-session retry history contains durable work output")
        prior_rows = connection.execute(
            "SELECT * FROM session_generations WHERE work_item_id = ? "
            "AND generation_number < ? ORDER BY generation_number",
            (work_item.work_item_id, generation.generation_number),
        ).fetchall()
        prior_generations = tuple(
            self._row_to_session_generation(row) for row in prior_rows
        )
        if tuple(item.generation_number for item in prior_generations) != tuple(
            range(1, generation.generation_number)
        ):
            raise ValueError("pre-session retry generation history is incomplete")
        for prior in prior_generations:
            if (
                prior.state is not SessionGenerationState.FAILED
                or prior.role is not SessionGenerationRole.IMPLEMENTATION
                or prior.codex_session_id is not None
                or prior.last_published_sha is not None
                or prior.start_head_sha != work_item.base_sha
                or prior.rotation_reason
                not in {None, PRE_SESSION_RETRY_ROTATION_REASON}
            ):
                raise ValueError("pre-session retry has a non-rejection generation")
            turn_rows = connection.execute(
                "SELECT turns.* FROM turns "
                "JOIN turn_session_generations USING(turn_id) "
                "WHERE session_generation_id = ? ORDER BY turns.turn_number",
                (prior.session_generation_id,),
            ).fetchall()
            if len(turn_rows) != 1:
                raise ValueError("pre-session retry generation must have exactly one Turn")
            turn = self._row_to_turn(turn_rows[0])
            if (
                turn.state is not TurnState.BLOCKED
                or turn.error_code != "runner_request_rejected"
                or turn.input_head_sha != work_item.base_sha
                or turn.output_sha256 is not None
                or turn.output_head_sha is not None
                or turn.result_status is not None
                or turn.result_summary is not None
            ):
                raise ValueError("pre-session retry Turn is not a definitive START rejection")
            receipt_count = int(
                connection.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM turn_agent_results WHERE turn_id = ?) + "
                    "(SELECT COUNT(*) FROM turn_usage WHERE turn_id = ?)",
                    (turn.turn_id, turn.turn_id),
                ).fetchone()[0]
            )
            if receipt_count != 0:
                raise ValueError("pre-session retry Turn has Codex output receipts")

    def get_turn_prompt_input(self, turn_id: str) -> TurnPromptInput | None:
        row = self._connection.execute(
            "SELECT * FROM turn_prompt_inputs WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        return self._row_to_turn_prompt_input(row) if row is not None else None

    def get_turn_agent_result(self, turn_id: str) -> AgentResult | None:
        row = self._connection.execute(
            "SELECT result_json, result_sha256 FROM turn_agent_results WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        result_json = str(row["result_json"])
        if sha256(result_json.encode("utf-8")).hexdigest() != row["result_sha256"]:
            raise ValueError("persisted Agent result digest is invalid")
        return parse_agent_result(result_json)

    def get_session_handoff_for_generation(
        self, session_generation_id: str
    ) -> SessionHandoffSnapshot | None:
        validate_session_generation_id(session_generation_id)
        row = self._connection.execute(
            "SELECT * FROM session_handoffs WHERE to_session_generation_id = ?",
            (session_generation_id,),
        ).fetchone()
        return self._row_to_session_handoff(row) if row is not None else None

    def get_turn_handoff(self, turn_id: str) -> SessionHandoffSnapshot | None:
        row = self._connection.execute(
            "SELECT session_handoffs.* FROM session_handoffs "
            "JOIN turn_handoff_bindings USING(handoff_id) WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        return self._row_to_session_handoff(row) if row is not None else None

    def list_work_item_publication_checkpoints(
        self, work_item_id: str
    ) -> tuple[PublishedCheckpoint, ...]:
        if self.get_work_item(work_item_id) is None:
            raise KeyError(f"work item not found: {work_item_id}")
        return tuple(
            self._row_to_published_checkpoint(row)
            for row in self._connection.execute(
                "SELECT publication_checkpoints.* FROM publication_checkpoints "
                "JOIN turns USING(turn_id) WHERE turns.work_item_id = ? "
                "ORDER BY turns.turn_number",
                (work_item_id,),
            )
        )

    def list_session_generation_turn_prompt_inputs(
        self, session_generation_id: str
    ) -> tuple[TurnPromptInput, ...]:
        validate_session_generation_id(session_generation_id)
        return tuple(
            self._row_to_turn_prompt_input(row)
            for row in self._connection.execute(
                "SELECT turn_prompt_inputs.* FROM turn_prompt_inputs "
                "JOIN turn_session_generations USING(turn_id) "
                "JOIN turns USING(turn_id) WHERE session_generation_id = ? "
                "ORDER BY turns.turn_number",
                (session_generation_id,),
            )
        )

    def list_session_generation_turns(self, session_generation_id: str) -> tuple[Turn, ...]:
        """Return one generation's Turns in durable execution order."""
        if self.get_session_generation(session_generation_id) is None:
            raise KeyError(f"session generation not found: {session_generation_id}")
        return tuple(
            self._row_to_turn(row)
            for row in self._connection.execute(
                "SELECT turns.* FROM turns JOIN turn_session_generations USING(turn_id) "
                "WHERE session_generation_id = ? ORDER BY turns.turn_number",
                (session_generation_id,),
            )
        )

    def rotate_session_generation(
        self,
        work_item_id: str,
        *,
        current_session_generation_id: str,
        current_generation_number: int,
        rotation_reason: str,
        expected_current_policy_sha256: str | None = None,
        new_policy_sha256: str | None = None,
        policy_sha256: str | None = None,
        new_role: SessionGenerationRole = SessionGenerationRole.IMPLEMENTATION,
        new_session_generation_id: str | None = None,
        handoff: SessionHandoffSnapshot | None = None,
        updated_at: str | None = None,
    ) -> tuple[SessionGeneration, SessionGeneration]:
        """Retire one idle active generation and atomically plan its replacement."""
        if not isinstance(new_role, SessionGenerationRole):
            raise ValueError("new_role must be a SessionGenerationRole")
        if (
            not isinstance(rotation_reason, str)
            or not rotation_reason
            or len(rotation_reason) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in rotation_reason)
        ):
            raise ValueError("rotation_reason must be non-empty bounded text")
        if policy_sha256 is not None:
            if (
                expected_current_policy_sha256 is not None
                or new_policy_sha256 is not None
            ):
                raise ValueError("legacy policy_sha256 cannot be combined with split policy digests")
            expected_current_policy_sha256 = policy_sha256
            new_policy_sha256 = policy_sha256
        if new_policy_sha256 is None:
            raise ValueError("new_policy_sha256 is required")
        validate_sha256(new_policy_sha256, "new_policy_sha256")
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            work_item = self._require_work_item(connection, work_item_id)
            if work_item.state is not WorkItemState.READY:
                raise ValueError("work item must be ready before rotating a session generation")
            placeholders = ", ".join("?" for _ in ACTIVE_TURN_STATES)
            active_turn = connection.execute(
                f"SELECT turn_id FROM turns WHERE state IN ({placeholders}) LIMIT 1",
                tuple(state.value for state in ACTIVE_TURN_STATES),
            ).fetchone()
            if active_turn is not None:
                raise ValueError("cannot rotate while an active Turn exists")
            current = self._require_generation_identity(
                connection,
                session_generation_id=current_session_generation_id,
                work_item_id=work_item_id,
                generation_number=current_generation_number,
                policy_sha256=expected_current_policy_sha256,
                allow_legacy_policy=True,
            )
            if current.state is not SessionGenerationState.ACTIVE:
                raise ValueError("only an active session generation can be rotated")
            work_item_anchor = work_item.last_published_sha or work_item.base_sha
            generation_anchor = current.last_published_sha or current.start_head_sha
            if work_item_anchor != generation_anchor:
                raise ValueError("work item and session generation publication anchors differ")
            retiring = current.transition_to(SessionGenerationState.RETIRING, at=now)
            retired = retiring.transition_to(SessionGenerationState.RETIRED, at=now)
            next_number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(generation_number), 0) + 1 "
                    "FROM session_generations WHERE work_item_id = ?",
                    (work_item_id,),
                ).fetchone()[0]
            )
            if handoff is None:
                raise ValueError("session generation rotation requires a handoff")
            if not isinstance(handoff, SessionHandoffSnapshot):
                raise TypeError("handoff must be a SessionHandoffSnapshot")
            if new_session_generation_id is None:
                new_session_generation_id = handoff.to_session_generation_id
            replacement = SessionGeneration.new(
                session_generation_id=new_session_generation_id,
                work_item_id=work_item_id,
                generation_number=next_number,
                role=new_role,
                start_head_sha=work_item_anchor,
                policy_sha256=new_policy_sha256,
                rotation_reason=rotation_reason,
                at=now,
            )
            trusted = handoff.trusted_facts
            trusted_work_item = trusted["work_item"]
            trusted_generation = trusted["generation"]
            trusted_git = trusted["git"]
            trusted_issue = trusted["issue"]
            if (
                handoff.work_item_id != work_item_id
                or handoff.from_session_generation_id
                != current.session_generation_id
                or handoff.to_session_generation_id
                != replacement.session_generation_id
                or handoff.from_generation_number != current.generation_number
                or handoff.to_generation_number != replacement.generation_number
                or handoff.current_head_sha != work_item_anchor
                or handoff.to_agent_policy_sha256 != new_policy_sha256
                or trusted_work_item["repository"] != work_item.repository
                or trusted_work_item["issue_number"] != work_item.issue_number
                or trusted_git["base_sha"] != work_item.base_sha
                or trusted_git["task_branch"] != work_item.task_branch
                or trusted_generation["rotation_reason"] != rotation_reason
            ):
                raise ValueError("handoff identity conflicts with generation rotation")
            if current.rotation_reason != "legacy_migration" and (
                trusted_issue["content_sha256"]
                != current.baseline_issue_content_sha256
                or trusted_issue["task_spec_sha256"]
                != current.baseline_task_spec_sha256
            ):
                raise ValueError(
                    "handoff semantic inputs conflict with the source generation baseline"
                )
            cursor = connection.execute(
                "UPDATE session_generations SET state = ?, retired_at = ?, updated_at = ? "
                "WHERE session_generation_id = ? AND state = 'active'",
                (
                    retired.state.value,
                    retired.retired_at,
                    retired.updated_at,
                    current_session_generation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"concurrent session generation rotation detected: "
                    f"{current_session_generation_id}"
                )
            self._insert_session_generation(connection, replacement)
            self._insert_session_handoff(connection, handoff)
            self._insert_work_item_event(
                connection,
                work_item_id,
                None,
                "session_generation_rotated",
                {
                    "from_generation_number": current.generation_number,
                    "from_session_generation_id": current.session_generation_id,
                    "handoff_id": handoff.handoff_id,
                    "handoff_sha256": handoff.handoff_sha256,
                    "rotation_reason": rotation_reason,
                    "to_generation_number": replacement.generation_number,
                    "to_session_generation_id": replacement.session_generation_id,
                },
                now,
            )
        return retired, replacement

    def record_generation_turn_running(
        self,
        turn_id: str,
        *,
        session_generation_id: str,
        generation_number: int,
        policy_sha256: str,
        session_id: str,
        updated_at: str | None = None,
    ) -> tuple[SessionGeneration, Turn]:
        """Record a proven started session and a running Turn in one transaction."""
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            generation, turn = self._require_bound_generation_turn(
                connection,
                turn_id=turn_id,
                session_generation_id=session_generation_id,
                generation_number=generation_number,
                policy_sha256=policy_sha256,
            )
            if generation.state is SessionGenerationState.STARTING:
                generation = generation.bind_session(session_id, at=now).transition_to(
                    SessionGenerationState.ACTIVE, at=now
                )
                cursor = connection.execute(
                    "UPDATE session_generations SET state = ?, codex_session_id = ?, updated_at = ? "
                    "WHERE session_generation_id = ? AND state = 'starting' "
                    "AND (codex_session_id IS NULL OR codex_session_id = ?)",
                    (
                        generation.state.value,
                        generation.codex_session_id,
                        now,
                        session_generation_id,
                        session_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "concurrent session generation activation detected"
                    )
            elif generation.state is SessionGenerationState.ACTIVE:
                generation = generation.bind_session(session_id, at=now)
            else:
                raise ValueError("session generation cannot record a running Turn")
            if turn.state is TurnState.STARTING or turn.state is TurnState.RECONCILING:
                turn = turn.transition_to(TurnState.RUNNING, at=now)
                connection.execute(
                    "UPDATE turns SET state = ?, started_at = ?, updated_at = ? WHERE turn_id = ?",
                    (turn.state.value, turn.started_at, now, turn_id),
                )
            elif turn.state is not TurnState.RUNNING:
                raise ValueError("Turn cannot record a running receipt")
        return generation, turn

    def record_generation_turn_unknown(
        self,
        turn_id: str,
        *,
        session_generation_id: str,
        generation_number: int,
        policy_sha256: str,
        session_id: str | None = None,
        updated_at: str | None = None,
    ) -> tuple[SessionGeneration, Turn]:
        """Persist an ambiguous execution receipt without activating the generation."""
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            generation, turn = self._require_bound_generation_turn(
                connection,
                turn_id=turn_id,
                session_generation_id=session_generation_id,
                generation_number=generation_number,
                policy_sha256=policy_sha256,
            )
            if generation.state not in {
                SessionGenerationState.STARTING,
                SessionGenerationState.ACTIVE,
            }:
                raise ValueError(
                    "unknown execution requires a starting or active generation"
                )
            if session_id is not None:
                updated_generation = generation.bind_session(session_id, at=now)
                if updated_generation is not generation:
                    cursor = connection.execute(
                        "UPDATE session_generations SET codex_session_id = ?, updated_at = ? "
                        "WHERE session_generation_id = ? AND codex_session_id IS NULL",
                        (session_id, now, session_generation_id),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            "concurrent session generation receipt binding detected"
                        )
                    generation = updated_generation
            if turn.state is TurnState.STARTING:
                turn = turn.transition_to(TurnState.RECONCILING, at=now)
                connection.execute(
                    "UPDATE turns SET state = ?, updated_at = ? WHERE turn_id = ?",
                    (turn.state.value, now, turn_id),
                )
            elif turn.state is not TurnState.RECONCILING:
                raise ValueError("Turn cannot record an unknown execution receipt")
        return generation, turn

    def record_generation_turn_failed(
        self,
        turn_id: str,
        *,
        session_generation_id: str,
        generation_number: int,
        policy_sha256: str,
        error_code: str,
        session_id: str | None = None,
        updated_at: str | None = None,
    ) -> tuple[WorkItem, SessionGeneration, Turn]:
        """Terminalize a generation failure, its Turn, and its WorkItem atomically."""
        if not isinstance(error_code, str) or not error_code or len(error_code) > 128:
            raise ValueError("error_code must be bounded non-empty text")
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            generation, turn = self._require_bound_generation_turn(
                connection,
                turn_id=turn_id,
                session_generation_id=session_generation_id,
                generation_number=generation_number,
                policy_sha256=policy_sha256,
            )
            work_item = self._require_work_item(connection, turn.work_item_id)
            if generation.state is not SessionGenerationState.FAILED:
                if session_id is not None:
                    generation = generation.bind_session(session_id, at=now)
                generation = generation.transition_to(SessionGenerationState.FAILED, at=now)
                connection.execute(
                    "UPDATE session_generations SET state = ?, codex_session_id = ?, "
                    "retired_at = ?, updated_at = ? WHERE session_generation_id = ?",
                    (
                        generation.state.value,
                        generation.codex_session_id,
                        generation.retired_at,
                        now,
                        session_generation_id,
                    ),
                )
            elif session_id is not None and generation.codex_session_id != session_id:
                raise ValueError("failed generation is bound to a different Codex session")
            if turn.state is not TurnState.BLOCKED:
                if turn.state not in ACTIVE_TURN_STATES:
                    raise ValueError("Turn cannot record a failure receipt")
                turn = replace(turn, error_code=error_code).transition_to(TurnState.BLOCKED, at=now)
                connection.execute(
                    "UPDATE turns SET state = ?, error_code = ?, finished_at = ?, updated_at = ? "
                    "WHERE turn_id = ?",
                    (turn.state.value, error_code, turn.finished_at, now, turn_id),
                )
            elif turn.error_code != error_code:
                raise ValueError("Turn already has a different recorded error")
            if work_item.state is WorkItemState.RUNNING:
                work_item = work_item.transition_to(WorkItemState.BLOCKED, at=now)
                connection.execute(
                    "UPDATE work_items SET state = ?, updated_at = ? WHERE work_item_id = ?",
                    (work_item.state.value, now, work_item.work_item_id),
                )
            elif work_item.state is not WorkItemState.BLOCKED:
                raise ValueError("WorkItem cannot record a generation failure")
        return work_item, generation, turn

    def record_generation_turn_context_failure(
        self,
        turn_id: str,
        *,
        session_generation_id: str,
        generation_number: int,
        policy_sha256: str,
        session_id: str,
        head_sha: str,
        worktree_clean: bool,
        error_code: str,
        updated_at: str | None = None,
    ) -> tuple[WorkItem, SessionGeneration, Turn, TurnContextFailureReceipt]:
        """Record a clean exact-anchor context failure without killing the generation."""
        now = updated_at or utc_now_iso()
        receipt = TurnContextFailureReceipt(
            turn_id=turn_id,
            session_generation_id=session_generation_id,
            head_sha=head_sha,
            worktree_clean=worktree_clean,
            error_code=error_code,
            created_at=now,
            updated_at=now,
        )
        with self._transaction() as connection:
            generation, turn = self._require_bound_generation_turn(
                connection,
                turn_id=turn_id,
                session_generation_id=session_generation_id,
                generation_number=generation_number,
                policy_sha256=policy_sha256,
            )
            work_item = self._require_work_item(connection, turn.work_item_id)
            existing_row = connection.execute(
                "SELECT * FROM turn_context_failures WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if existing_row is not None:
                existing = self._row_to_turn_context_failure(existing_row)
                if existing != replace(
                    receipt,
                    created_at=existing.created_at,
                    updated_at=existing.updated_at,
                ):
                    raise ValueError("Turn already has different context failure evidence")
                if (
                    turn.state is TurnState.INTERRUPTED
                    and turn.error_code == error_code
                    and work_item.state is WorkItemState.READY
                    and generation.state is SessionGenerationState.ACTIVE
                ):
                    return work_item, generation, turn, existing
                raise ValueError("persisted context failure state is inconsistent")
            if (
                turn.state not in {
                    TurnState.STARTING,
                    TurnState.RUNNING,
                    TurnState.RECONCILING,
                }
                or turn.input_head_sha != head_sha
                or work_item.state is not WorkItemState.RUNNING
            ):
                raise ValueError("Turn cannot record a clean context failure")
            if generation.state is SessionGenerationState.STARTING:
                generation = generation.bind_session(session_id, at=now).transition_to(
                    SessionGenerationState.ACTIVE, at=now
                )
                cursor = connection.execute(
                    "UPDATE session_generations SET state = ?, codex_session_id = ?, "
                    "started_at = ?, updated_at = ? WHERE session_generation_id = ? "
                    "AND state = 'starting'",
                    (
                        generation.state.value,
                        generation.codex_session_id,
                        generation.started_at,
                        now,
                        generation.session_generation_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("concurrent context failure activation detected")
            elif generation.state is SessionGenerationState.ACTIVE:
                generation = generation.bind_session(session_id, at=now)
            else:
                raise ValueError("context failure requires a live generation")
            turn = replace(turn, error_code=error_code).transition_to(
                TurnState.INTERRUPTED, at=now
            )
            work_item = work_item.transition_to(WorkItemState.READY, at=now)
            connection.execute(
                "INSERT INTO turn_context_failures "
                "(turn_id, session_generation_id, head_sha, worktree_clean, "
                "error_code, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
                (
                    receipt.turn_id,
                    receipt.session_generation_id,
                    receipt.head_sha,
                    receipt.error_code,
                    receipt.created_at,
                    receipt.updated_at,
                ),
            )
            connection.execute(
                "UPDATE turns SET state = ?, error_code = ?, finished_at = ?, "
                "updated_at = ? WHERE turn_id = ?",
                (
                    turn.state.value,
                    turn.error_code,
                    turn.finished_at,
                    now,
                    turn.turn_id,
                ),
            )
            connection.execute(
                "UPDATE work_items SET state = ?, updated_at = ? WHERE work_item_id = ?",
                (work_item.state.value, now, work_item.work_item_id),
            )
            self._insert_work_item_event(
                connection,
                work_item.work_item_id,
                turn.turn_id,
                "session_context_failure_clean",
                {
                    "head_sha": head_sha,
                    "session_generation_id": session_generation_id,
                    "worktree_clean": True,
                },
                now,
            )
        return work_item, generation, turn, receipt

    def get_turn_context_failure(
        self, turn_id: str
    ) -> TurnContextFailureReceipt | None:
        row = self._connection.execute(
            "SELECT * FROM turn_context_failures WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        return self._row_to_turn_context_failure(row) if row is not None else None

    def record_generation_turn_finished(
        self,
        turn_id: str,
        *,
        session_generation_id: str,
        generation_number: int,
        policy_sha256: str,
        session_id: str,
        output_sha256: str,
        output_head_sha: str,
        result_status: str,
        result_summary: str,
        agent_result: AgentResult,
        input_tokens: int,
        cached_input_tokens: int,
        cache_write_input_tokens: int,
        output_tokens: int,
        reasoning_output_tokens: int,
        delegation_receipt: DelegationReceipt | None = None,
        updated_at: str | None = None,
    ) -> tuple[WorkItem, SessionGeneration, Turn, TurnUsage]:
        """Record a terminal Agent receipt and usage without splitting its durability."""
        if not isinstance(agent_result, AgentResult):
            raise TypeError("agent_result must be an AgentResult")
        if (
            agent_result.status.value != result_status
            or agent_result.summary != result_summary
        ):
            raise ValueError("Agent result conflicts with the terminal Turn fields")
        result_json = agent_result_to_json(agent_result)
        result_sha256 = sha256(result_json.encode("utf-8")).hexdigest()
        if delegation_receipt is not None:
            if not isinstance(delegation_receipt, DelegationReceipt):
                raise TypeError("delegation_receipt must be a DelegationReceipt")
            if delegation_receipt.root_thread_id != session_id:
                raise ValueError("delegation receipt conflicts with the Codex session")
            delegation_json = delegation_receipt_to_json(delegation_receipt)
            delegation_sha256 = sha256(delegation_json.encode("utf-8")).hexdigest()
        else:
            delegation_json = None
            delegation_sha256 = None
        now = updated_at or utc_now_iso()
        usage = TurnUsage.new(
            turn_id=turn_id,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_input_tokens=cache_write_input_tokens,
            output_tokens=output_tokens,
            reasoning_output_tokens=reasoning_output_tokens,
            at=now,
        )
        with self._transaction() as connection:
            generation, turn = self._require_bound_generation_turn(
                connection,
                turn_id=turn_id,
                session_generation_id=session_generation_id,
                generation_number=generation_number,
                policy_sha256=policy_sha256,
            )
            work_item = self._require_work_item(connection, turn.work_item_id)
            if generation.state is SessionGenerationState.STARTING:
                generation = generation.bind_session(session_id, at=now).transition_to(
                    SessionGenerationState.ACTIVE, at=now
                )
                cursor = connection.execute(
                    "UPDATE session_generations SET state = ?, codex_session_id = ?, updated_at = ? "
                    "WHERE session_generation_id = ? AND state = 'starting' "
                    "AND (codex_session_id IS NULL OR codex_session_id = ?)",
                    (
                        generation.state.value,
                        generation.codex_session_id,
                        now,
                        session_generation_id,
                        session_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "concurrent session generation activation detected"
                    )
            elif generation.state is SessionGenerationState.ACTIVE:
                generation = generation.bind_session(session_id, at=now)
            else:
                raise ValueError("session generation cannot record a finished Turn")
            candidate = replace(
                turn,
                output_sha256=output_sha256,
                output_head_sha=output_head_sha,
                result_status=result_status,
                result_summary=result_summary,
                updated_at=now,
            )
            if turn.output_sha256 is not None:
                if (
                    turn.output_sha256 != candidate.output_sha256
                    or turn.output_head_sha != candidate.output_head_sha
                    or turn.result_status != candidate.result_status
                    or turn.result_summary != candidate.result_summary
                ):
                    raise ValueError("Turn already has a different recorded result")
                candidate = turn
            else:
                connection.execute(
                    "UPDATE turns SET output_sha256 = ?, output_head_sha = ?, result_status = ?, "
                    "result_summary = ?, updated_at = ? WHERE turn_id = ? AND output_sha256 IS NULL",
                    (
                        candidate.output_sha256,
                        candidate.output_head_sha,
                        candidate.result_status,
                        candidate.result_summary,
                        now,
                        turn_id,
                    ),
                )
            existing_usage = connection.execute(
                "SELECT * FROM turn_usage WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if existing_usage is not None:
                persisted_usage = self._row_to_turn_usage(existing_usage)
                if (
                    persisted_usage.input_tokens != usage.input_tokens
                    or persisted_usage.cached_input_tokens
                    != usage.cached_input_tokens
                    or persisted_usage.cache_write_input_tokens
                    != usage.cache_write_input_tokens
                    or persisted_usage.output_tokens != usage.output_tokens
                    or persisted_usage.reasoning_output_tokens
                    != usage.reasoning_output_tokens
                ):
                    raise ValueError("Turn already has different recorded usage")
                usage = persisted_usage
            else:
                connection.execute(
                    "INSERT INTO turn_usage (turn_id, input_tokens, cached_input_tokens, "
                    "cache_write_input_tokens, output_tokens, reasoning_output_tokens, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        usage.turn_id,
                        usage.input_tokens,
                        usage.cached_input_tokens,
                        usage.cache_write_input_tokens,
                        usage.output_tokens,
                        usage.reasoning_output_tokens,
                        usage.created_at,
                        usage.updated_at,
                    ),
                )
            existing_result = connection.execute(
                "SELECT result_json, result_sha256 FROM turn_agent_results WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if existing_result is not None:
                if (
                    existing_result["result_json"] != result_json
                    or existing_result["result_sha256"] != result_sha256
                ):
                    raise ValueError("Turn already has a different Agent result receipt")
            else:
                connection.execute(
                    "INSERT INTO turn_agent_results "
                    "(turn_id, result_json, result_sha256, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (turn_id, result_json, result_sha256, now, now),
                )
            existing_delegation = connection.execute(
                "SELECT receipt_json, receipt_sha256 FROM turn_delegation_receipts "
                "WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if existing_delegation is not None:
                if (
                    delegation_json is None
                    or existing_delegation["receipt_json"] != delegation_json
                    or existing_delegation["receipt_sha256"] != delegation_sha256
                ):
                    raise ValueError(
                        "Turn already has different recorded delegation evidence"
                    )
            elif delegation_receipt is not None:
                connection.execute(
                    "INSERT INTO turn_delegation_receipts "
                    "(turn_id, schema_version, observer, codex_version, root_thread_id, "
                    "root_model, root_reasoning_effort, agent_count, receipt_json, "
                    "receipt_sha256, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        turn_id,
                        delegation_receipt.schema_version,
                        delegation_receipt.observer,
                        delegation_receipt.codex_version,
                        delegation_receipt.root_thread_id,
                        delegation_receipt.root_model,
                        delegation_receipt.root_reasoning_effort,
                        len(delegation_receipt.agents),
                        delegation_json,
                        delegation_sha256,
                        now,
                        now,
                    ),
                )
                self._insert_work_item_event(
                    connection,
                    turn.work_item_id,
                    turn_id,
                    "turn_delegation_observed",
                    {
                        "agent_count": len(delegation_receipt.agents),
                        "agents": [
                            {
                                "agent_name": agent.agent_name,
                                "edge_status": agent.edge_status,
                                "model": agent.model,
                                "reasoning_effort": agent.reasoning_effort,
                                "tokens_used": agent.tokens_used,
                            }
                            for agent in delegation_receipt.agents
                        ],
                        "observer": delegation_receipt.observer,
                        "root_model": delegation_receipt.root_model,
                        "root_reasoning_effort": (
                            delegation_receipt.root_reasoning_effort
                        ),
                    },
                    now,
                )
            if turn.output_sha256 is not None and turn.state not in ACTIVE_TURN_STATES:
                return work_item, generation, turn, usage
            if work_item.state is not WorkItemState.RUNNING:
                raise ValueError("work item is not running")
            if output_head_sha != turn.input_head_sha:
                next_turn = candidate.transition_to(TurnState.CHECKPOINTING, at=now)
                next_work_item = work_item
            else:
                outcome = {
                    # Protocol-v2 completion candidates remain active until exact-HEAD
                    # CI and structured acceptance evidence pass on the Control Host.
                    "completed": (TurnState.PUBLISHED, WorkItemState.RUNNING),
                    "needs_input": (TurnState.NEEDS_INPUT, WorkItemState.WAITING_INPUT),
                    "blocked": (TurnState.BLOCKED, WorkItemState.BLOCKED),
                }.get(result_status)
                if outcome is None:
                    raise ValueError("result_status is unsupported")
                next_turn = candidate.transition_to(outcome[0], at=now)
                next_work_item = (
                    work_item
                    if outcome[1] is work_item.state
                    else work_item.transition_to(outcome[1], at=now)
                )
            if turn.state is not next_turn.state:
                connection.execute(
                    "UPDATE turns SET state = ?, started_at = ?, finished_at = ?, updated_at = ? "
                    "WHERE turn_id = ?",
                    (
                        next_turn.state.value,
                        next_turn.started_at,
                        next_turn.finished_at,
                        now,
                        turn_id,
                    ),
                )
            if next_work_item is not work_item:
                connection.execute(
                    "UPDATE work_items SET state = ?, updated_at = ? WHERE work_item_id = ?",
                    (next_work_item.state.value, now, work_item.work_item_id),
                )
        return next_work_item, generation, next_turn, usage

    def get_turn_delegation_receipt(
        self, turn_id: str
    ) -> DelegationReceipt | None:
        row = self._connection.execute(
            "SELECT receipt_json, receipt_sha256 FROM turn_delegation_receipts "
            "WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        receipt_json = str(row["receipt_json"])
        if sha256(receipt_json.encode("utf-8")).hexdigest() != row["receipt_sha256"]:
            raise ValueError("persisted delegation receipt digest is invalid")
        try:
            payload = json.loads(
                receipt_json, object_pairs_hook=_unique_json_object
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("persisted delegation receipt is invalid") from exc
        return parse_delegation_receipt(payload)

    def get_turn_completion_gate(
        self, turn_id: str
    ) -> CompletionGateSnapshot | None:
        row = self._connection.execute(
            "SELECT * FROM turn_completion_gates WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        return self._row_to_completion_gate(row) if row is not None else None

    def record_turn_completion_gate(
        self,
        snapshot: CompletionGateSnapshot,
        *,
        updated_at: str | None = None,
    ) -> tuple[WorkItem, Turn, CompletionGateSnapshot]:
        """Persist one gate observation and atomically apply a terminal verdict."""
        if not isinstance(snapshot, CompletionGateSnapshot):
            raise TypeError("snapshot must be a CompletionGateSnapshot")
        now = updated_at or snapshot.observed_at
        with self._transaction() as connection:
            turn = self._require_turn(connection, snapshot.turn_id)
            work_item = self._require_work_item(connection, turn.work_item_id)
            existing_row = connection.execute(
                "SELECT * FROM turn_completion_gates WHERE turn_id = ?",
                (snapshot.turn_id,),
            ).fetchone()
            existing = (
                self._row_to_completion_gate(existing_row)
                if existing_row is not None
                else None
            )
            if existing is not None and (
                existing.work_item_id != snapshot.work_item_id
                or existing.head_sha != snapshot.head_sha
                or existing.task_spec_sha256 != snapshot.task_spec_sha256
            ):
                raise ValueError("completion gate target is already bound differently")
            if existing is not None and existing.status is not CompletionGateStatus.PENDING:
                if (
                    existing.status is CompletionGateStatus.PASSED
                    and turn.state is TurnState.FINISHED
                    and work_item.state is WorkItemState.REVIEW
                ) or (
                    existing.status
                    in {CompletionGateStatus.FAILED, CompletionGateStatus.UNVERIFIED}
                    and turn.state is TurnState.BLOCKED
                    and work_item.state is WorkItemState.BLOCKED
                ):
                    return work_item, turn, existing
                raise ValueError("terminal completion gate state is inconsistent")
            if (
                turn.work_item_id != snapshot.work_item_id
                or turn.output_head_sha != snapshot.head_sha
                or turn.result_status != "completed"
                or turn.state is not TurnState.PUBLISHED
                or work_item.state is not WorkItemState.RUNNING
            ):
                raise ValueError("Turn is not awaiting its completion gate")

            persisted = replace(
                snapshot,
                created_at=(existing.created_at if existing is not None else now),
                updated_at=now,
            )
            if existing is None:
                connection.execute(
                    "INSERT INTO turn_completion_gates "
                    "(turn_id, work_item_id, status, head_sha, task_spec_sha256, "
                    "evidence_json, evidence_sha256, observed_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        persisted.turn_id,
                        persisted.work_item_id,
                        persisted.status.value,
                        persisted.head_sha,
                        persisted.task_spec_sha256,
                        persisted.evidence_json,
                        persisted.evidence_sha256,
                        persisted.observed_at,
                        persisted.created_at,
                        persisted.updated_at,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE turn_completion_gates SET status = ?, evidence_json = ?, "
                    "evidence_sha256 = ?, observed_at = ?, updated_at = ? "
                    "WHERE turn_id = ? AND status = 'pending'",
                    (
                        persisted.status.value,
                        persisted.evidence_json,
                        persisted.evidence_sha256,
                        persisted.observed_at,
                        persisted.updated_at,
                        persisted.turn_id,
                    ),
                )
            self._insert_work_item_event(
                connection,
                work_item.work_item_id,
                turn.turn_id,
                "completion_gate_observed",
                {
                    "evidence_sha256": persisted.evidence_sha256,
                    "head_sha": persisted.head_sha,
                    "status": persisted.status.value,
                },
                now,
            )
            if persisted.status is CompletionGateStatus.PENDING:
                return work_item, turn, persisted

            if persisted.status is CompletionGateStatus.PASSED:
                next_turn = turn.transition_to(TurnState.FINISHED, at=now)
                next_work_item = work_item.transition_to(WorkItemState.REVIEW, at=now)
                error_code = None
            else:
                error_code = (
                    "completion_gate_failed"
                    if persisted.status is CompletionGateStatus.FAILED
                    else "completion_gate_unverified"
                )
                next_turn = replace(turn, error_code=error_code).transition_to(
                    TurnState.BLOCKED, at=now
                )
                next_work_item = work_item.transition_to(WorkItemState.BLOCKED, at=now)
            turn_cursor = connection.execute(
                "UPDATE turns SET state = ?, error_code = ?, finished_at = ?, "
                "updated_at = ? WHERE turn_id = ? AND state = 'published'",
                (
                    next_turn.state.value,
                    error_code,
                    next_turn.finished_at,
                    now,
                    turn.turn_id,
                ),
            )
            work_cursor = connection.execute(
                "UPDATE work_items SET state = ?, updated_at = ? "
                "WHERE work_item_id = ? AND state = 'running'",
                (
                    next_work_item.state.value,
                    now,
                    work_item.work_item_id,
                ),
            )
            if turn_cursor.rowcount != 1 or work_cursor.rowcount != 1:
                raise RuntimeError("concurrent completion gate finalization detected")
            self._insert_work_item_event(
                connection,
                work_item.work_item_id,
                turn.turn_id,
                "completion_gate_finalized",
                {
                    "status": persisted.status.value,
                    "turn_state": next_turn.state.value,
                    "work_item_state": next_work_item.state.value,
                },
                now,
            )
        return next_work_item, next_turn, persisted

    def record_generation_publication(
        self,
        turn_id: str,
        *,
        previous_sha: str,
        head_sha: str,
        bundle_sha256: str,
        changed_paths: tuple[str, ...],
        commit_count: int,
        size_bytes: int,
        updated_at: str | None = None,
    ) -> tuple[WorkItem, SessionGeneration]:
        """Advance matching WorkItem and generation publication anchors together."""
        previous_sha = validate_git_sha(previous_sha, "previous_sha")
        head_sha = validate_git_sha(head_sha, "head_sha")
        checkpoint = PublishedCheckpoint(
            head_sha=head_sha,
            previous_sha=previous_sha,
            bundle_sha256=bundle_sha256,
            changed_paths=changed_paths,
        )
        if type(commit_count) is not int or not 1 <= commit_count <= 100:
            raise ValueError("commit_count must be within the publication boundary")
        if type(size_bytes) is not int or not 1 <= size_bytes <= 100 * 1024 * 1024:
            raise ValueError("size_bytes must be within the publication boundary")
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            generation, turn = self._require_bound_generation_turn_by_turn(connection, turn_id)
            work_item = self._require_work_item(connection, turn.work_item_id)
            work_anchor = work_item.last_published_sha or work_item.base_sha
            generation_anchor = generation.last_published_sha or generation.start_head_sha
            if work_item.last_published_sha == head_sha and generation.last_published_sha == head_sha:
                existing = connection.execute(
                    "SELECT * FROM publication_checkpoints WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                if existing is None or not self._publication_checkpoint_matches(
                    existing,
                    generation.session_generation_id,
                    checkpoint,
                    commit_count,
                    size_bytes,
                ):
                    raise ValueError("recorded publication is missing or conflicts with evidence")
                return work_item, generation
            if previous_sha != work_anchor or previous_sha != generation_anchor or head_sha == previous_sha:
                raise ValueError("publication anchor no longer matches the bound generation")
            connection.execute(
                "UPDATE work_items SET last_published_sha = ?, updated_at = ? WHERE work_item_id = ?",
                (head_sha, now, work_item.work_item_id),
            )
            connection.execute(
                "UPDATE session_generations SET last_published_sha = ?, updated_at = ? "
                "WHERE session_generation_id = ?",
                (head_sha, now, generation.session_generation_id),
            )
            connection.execute(
                "INSERT INTO publication_checkpoints "
                "(turn_id, session_generation_id, previous_sha, head_sha, bundle_sha256, "
                "changed_paths_json, commit_count, size_bytes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    turn_id,
                    generation.session_generation_id,
                    checkpoint.previous_sha,
                    checkpoint.head_sha,
                    checkpoint.bundle_sha256,
                    json.dumps(checkpoint.changed_paths, separators=(",", ":")),
                    commit_count,
                    size_bytes,
                    now,
                    now,
                ),
            )
            self._insert_work_item_event(
                connection,
                work_item.work_item_id,
                turn_id,
                "generation_commit_published",
                {
                    "bundle_sha256": bundle_sha256,
                    "changed_paths": changed_paths,
                    "commit_count": commit_count,
                    "head_sha": head_sha,
                    "previous_sha": previous_sha,
                    "size_bytes": size_bytes,
                },
                now,
            )
            work_item = replace(work_item, last_published_sha=head_sha, updated_at=now)
            generation = replace(generation, last_published_sha=head_sha, updated_at=now)
        return work_item, generation

    def transition_session_generation(
        self,
        session_generation_id: str,
        state: SessionGenerationState,
        *,
        updated_at: str | None = None,
    ) -> SessionGeneration:
        if not isinstance(state, SessionGenerationState):
            raise ValueError("state must be a SessionGenerationState")
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM session_generations WHERE session_generation_id = ?",
                (session_generation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"session generation not found: {session_generation_id}")
            generation = self._row_to_session_generation(row)
            if generation.state is state:
                return generation
            updated = generation.transition_to(state, at=now)
            cursor = connection.execute(
                "UPDATE session_generations SET state = ?, started_at = ?, retired_at = ?, "
                "updated_at = ? WHERE session_generation_id = ? AND state = ?",
                (
                    updated.state.value,
                    updated.started_at,
                    updated.retired_at,
                    updated.updated_at,
                    session_generation_id,
                    generation.state.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"concurrent session generation update detected: {session_generation_id}"
                )
            self._insert_work_item_event(
                connection,
                generation.work_item_id,
                None,
                "session_generation_state_changed",
                {
                    "from": generation.state.value,
                    "session_generation_id": session_generation_id,
                    "to": state.value,
                },
                now,
            )
        return updated

    def bind_session_generation_codex_session(
        self,
        session_generation_id: str,
        session_id: str,
        *,
        updated_at: str | None = None,
    ) -> SessionGeneration:
        validate_session_id(session_id)
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM session_generations WHERE session_generation_id = ?",
                (session_generation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"session generation not found: {session_generation_id}")
            generation = self._row_to_session_generation(row)
            updated = generation.bind_session(session_id, at=now)
            if updated is generation:
                return generation
            cursor = connection.execute(
                "UPDATE session_generations SET codex_session_id = ?, updated_at = ? "
                "WHERE session_generation_id = ? AND codex_session_id IS NULL",
                (session_id, now, session_generation_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"concurrent session generation binding detected: {session_generation_id}"
                )
            self._insert_work_item_event(
                connection,
                generation.work_item_id,
                None,
                "session_generation_codex_session_bound",
                {"session_generation_id": session_generation_id, "session_id": session_id},
                now,
            )
        return updated

    def bind_turn_session_generation(
        self,
        turn_id: str,
        session_generation_id: str,
        *,
        updated_at: str | None = None,
    ) -> SessionGeneration:
        """Bind a Turn once to a generation from the same WorkItem."""
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            turn_row = connection.execute(
                "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if turn_row is None:
                raise KeyError(f"turn not found: {turn_id}")
            turn = self._row_to_turn(turn_row)
            generation_row = connection.execute(
                "SELECT * FROM session_generations WHERE session_generation_id = ?",
                (session_generation_id,),
            ).fetchone()
            if generation_row is None:
                raise KeyError(f"session generation not found: {session_generation_id}")
            generation = self._row_to_session_generation(generation_row)
            if generation.work_item_id != turn.work_item_id:
                raise ValueError("Turn and session generation must belong to the same WorkItem")
            existing = connection.execute(
                "SELECT session_generation_id FROM turn_session_generations WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if existing is not None:
                if existing["session_generation_id"] != session_generation_id:
                    raise ValueError("Turn is already bound to a different session generation")
                return generation
            if not generation.is_live:
                raise ValueError("a Turn cannot first bind to a terminal session generation")
            connection.execute(
                "INSERT INTO turn_session_generations(turn_id, session_generation_id) "
                "VALUES (?, ?)",
                (turn_id, session_generation_id),
            )
            self._insert_work_item_event(
                connection,
                turn.work_item_id,
                turn_id,
                "turn_session_generation_bound",
                {"session_generation_id": session_generation_id},
                now,
            )
        return generation

    def get_turn_session_generation(self, turn_id: str) -> SessionGeneration | None:
        row = self._connection.execute(
            "SELECT session_generations.* FROM turn_session_generations "
            "JOIN session_generations USING(session_generation_id) "
            "WHERE turn_session_generations.turn_id = ?",
            (turn_id,),
        ).fetchone()
        return self._row_to_session_generation(row) if row is not None else None

    def record_session_generation_published_sha(
        self,
        session_generation_id: str,
        *,
        previous_sha: str,
        head_sha: str,
        updated_at: str | None = None,
    ) -> SessionGeneration:
        """Advance one live generation's immutable publication anchor once."""
        previous_sha = validate_git_sha(previous_sha, "previous_sha")
        head_sha = validate_git_sha(head_sha, "head_sha")
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM session_generations WHERE session_generation_id = ?",
                (session_generation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"session generation not found: {session_generation_id}")
            generation = self._row_to_session_generation(row)
            if generation.state not in {
                SessionGenerationState.ACTIVE,
                SessionGenerationState.RETIRING,
            }:
                raise ValueError("only active or retiring session generations can publish")
            current_anchor = generation.last_published_sha or generation.start_head_sha
            if generation.last_published_sha == head_sha:
                return generation
            if previous_sha != current_anchor:
                raise ValueError("publication anchor no longer matches the session generation")
            if head_sha == current_anchor:
                raise ValueError("published SHA must advance the session generation anchor")
            cursor = connection.execute(
                "UPDATE session_generations SET last_published_sha = ?, updated_at = ? "
                "WHERE session_generation_id = ? AND "
                "((last_published_sha IS NULL AND start_head_sha = ?) OR last_published_sha = ?)",
                (head_sha, now, session_generation_id, previous_sha, previous_sha),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"concurrent session generation publication update detected: "
                    f"{session_generation_id}"
                )
            self._insert_work_item_event(
                connection,
                generation.work_item_id,
                None,
                "session_generation_commit_published",
                {
                    "head_sha": head_sha,
                    "previous_sha": previous_sha,
                    "session_generation_id": session_generation_id,
                },
                now,
            )
        updated = self.get_session_generation(session_generation_id)
        assert updated is not None
        return updated

    def record_turn_usage(
        self,
        turn_id: str,
        *,
        input_tokens: int,
        cached_input_tokens: int,
        cache_write_input_tokens: int,
        output_tokens: int,
        reasoning_output_tokens: int,
        created_at: str | None = None,
    ) -> TurnUsage:
        """Write one immutable Runner token-usage receipt for a Turn."""
        usage = TurnUsage.new(
            turn_id=turn_id,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_input_tokens=cache_write_input_tokens,
            output_tokens=output_tokens,
            reasoning_output_tokens=reasoning_output_tokens,
            at=created_at,
        )
        with self._transaction() as connection:
            turn_row = connection.execute(
                "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if turn_row is None:
                raise KeyError(f"turn not found: {turn_id}")
            existing_row = connection.execute(
                "SELECT * FROM turn_usage WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if existing_row is not None:
                existing = self._row_to_turn_usage(existing_row)
                if (
                    existing.input_tokens != usage.input_tokens
                    or existing.cached_input_tokens != usage.cached_input_tokens
                    or existing.cache_write_input_tokens != usage.cache_write_input_tokens
                    or existing.output_tokens != usage.output_tokens
                    or existing.reasoning_output_tokens != usage.reasoning_output_tokens
                ):
                    raise ValueError("Turn already has different recorded usage")
                return existing
            connection.execute(
                "INSERT INTO turn_usage (turn_id, input_tokens, cached_input_tokens, "
                "cache_write_input_tokens, output_tokens, reasoning_output_tokens, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    usage.turn_id,
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.cache_write_input_tokens,
                    usage.output_tokens,
                    usage.reasoning_output_tokens,
                    usage.created_at,
                    usage.updated_at,
                ),
            )
            turn = self._row_to_turn(turn_row)
            self._insert_work_item_event(
                connection,
                turn.work_item_id,
                turn_id,
                "turn_usage_recorded",
                {
                    "cache_write_input_tokens": usage.cache_write_input_tokens,
                    "cached_input_tokens": usage.cached_input_tokens,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "reasoning_output_tokens": usage.reasoning_output_tokens,
                },
                usage.created_at,
            )
        return usage

    def get_turn_usage(self, turn_id: str) -> TurnUsage | None:
        row = self._connection.execute(
            "SELECT * FROM turn_usage WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        return self._row_to_turn_usage(row) if row is not None else None

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

    def prepare_slack_delivery(
        self,
        report: SlackReport,
        *,
        created_at: str | None = None,
    ) -> SlackDeliveryRecord:
        """Persist one immutable outbound payload identity before sending it."""
        if not isinstance(report, SlackReport):
            raise TypeError("report must be a SlackReport")
        now = created_at or utc_now_iso()
        payload_sha256 = sha256(report.text.encode("utf-8")).hexdigest()
        prepared = SlackDeliveryRecord(
            deduplication_key=report.deduplication_key,
            work_item_id=report.work_item_id,
            kind=report.kind,
            channel_id=report.channel_id,
            payload_sha256=payload_sha256,
            state=SlackDeliveryState.PREPARED,
            created_at=now,
            updated_at=now,
            turn_id=report.turn_id,
            thread_ts=report.thread_ts,
        )
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM slack_deliveries WHERE deduplication_key = ?",
                (prepared.deduplication_key,),
            ).fetchone()
            if row is not None:
                existing = self._row_to_slack_delivery(row)
                if (
                    existing.work_item_id != prepared.work_item_id
                    or existing.turn_id != prepared.turn_id
                    or existing.kind is not prepared.kind
                    or existing.channel_id != prepared.channel_id
                    or existing.thread_ts != prepared.thread_ts
                    or existing.payload_sha256 != prepared.payload_sha256
                ):
                    raise ValueError(
                        "Slack delivery key is already bound to a different payload"
                    )
                return existing
            connection.execute(
                "INSERT INTO slack_deliveries "
                "(deduplication_key, work_item_id, turn_id, kind, channel_id, "
                "thread_ts, payload_sha256, state, message_ts, permalink, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    prepared.deduplication_key,
                    prepared.work_item_id,
                    prepared.turn_id,
                    prepared.kind.value,
                    prepared.channel_id,
                    prepared.thread_ts,
                    prepared.payload_sha256,
                    prepared.state.value,
                    None,
                    None,
                    prepared.created_at,
                    prepared.updated_at,
                ),
            )
        return prepared

    def get_slack_delivery(
        self,
        deduplication_key: str,
    ) -> SlackDeliveryRecord | None:
        row = self._connection.execute(
            "SELECT * FROM slack_deliveries WHERE deduplication_key = ?",
            (deduplication_key,),
        ).fetchone()
        return self._row_to_slack_delivery(row) if row is not None else None

    def complete_slack_delivery(
        self,
        deduplication_key: str,
        receipt: SlackDeliveryReceipt,
        *,
        updated_at: str | None = None,
    ) -> SlackDeliveryRecord:
        """Atomically bind a proven root thread and record one delivery receipt."""
        if not isinstance(receipt, SlackDeliveryReceipt):
            raise TypeError("receipt must be a SlackDeliveryReceipt")
        if receipt.deduplication_key != deduplication_key:
            raise ValueError("Slack receipt key conflicts with the requested delivery")
        now = updated_at or utc_now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM slack_deliveries WHERE deduplication_key = ?",
                (deduplication_key,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Slack delivery not found: {deduplication_key}")
            delivery = self._row_to_slack_delivery(row)
            if receipt.channel_id != delivery.channel_id:
                raise ValueError("Slack receipt channel conflicts with the outbox")
            expected_thread = (
                receipt.message_ts
                if delivery.kind is SlackReportKind.ROOT
                else delivery.thread_ts
            )
            if receipt.thread_ts != expected_thread:
                raise ValueError("Slack receipt thread conflicts with the outbox")
            if delivery.state is SlackDeliveryState.DELIVERED:
                if (
                    delivery.message_ts != receipt.message_ts
                    or delivery.permalink != receipt.permalink
                ):
                    raise ValueError(
                        "Slack delivery is already bound to a different receipt"
                    )
                return delivery

            work_item_row = connection.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?",
                (delivery.work_item_id,),
            ).fetchone()
            if work_item_row is None:
                raise KeyError(f"work item not found: {delivery.work_item_id}")
            work_item = self._row_to_work_item(work_item_row)
            if delivery.kind is SlackReportKind.ROOT:
                bound = work_item.bind_slack_thread(
                    receipt.channel_id,
                    receipt.message_ts,
                    at=now,
                )
                if bound is not work_item:
                    cursor = connection.execute(
                        "UPDATE work_items SET slack_channel_id = ?, slack_thread_ts = ?, "
                        "updated_at = ? WHERE work_item_id = ? AND "
                        "slack_channel_id IS NULL AND slack_thread_ts IS NULL",
                        (
                            receipt.channel_id,
                            receipt.message_ts,
                            now,
                            delivery.work_item_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            "concurrent Slack thread binding detected: "
                            f"{delivery.work_item_id}"
                        )
                    self._insert_work_item_event(
                        connection,
                        delivery.work_item_id,
                        None,
                        "slack_thread_bound",
                        {
                            "channel_id": receipt.channel_id,
                            "thread_ts": receipt.message_ts,
                        },
                        now,
                    )
            elif (
                work_item.slack_channel_id != receipt.channel_id
                or work_item.slack_thread_ts != receipt.thread_ts
            ):
                raise ValueError(
                    "Slack Turn receipt conflicts with the WorkItem thread binding"
                )
            cursor = connection.execute(
                "UPDATE slack_deliveries SET state = ?, message_ts = ?, permalink = ?, "
                "updated_at = ? WHERE deduplication_key = ? AND state = ?",
                (
                    SlackDeliveryState.DELIVERED.value,
                    receipt.message_ts,
                    receipt.permalink,
                    now,
                    deduplication_key,
                    SlackDeliveryState.PREPARED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"concurrent Slack delivery completion detected: {deduplication_key}"
                )
            self._insert_work_item_event(
                connection,
                delivery.work_item_id,
                delivery.turn_id,
                "slack_report_delivered",
                {
                    "deduplication_key": deduplication_key,
                    "kind": delivery.kind.value,
                    "message_ts": receipt.message_ts,
                },
                now,
            )
        completed = self.get_slack_delivery(deduplication_key)
        assert completed is not None
        return completed

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
        issue_allowed_paths: tuple[str, ...] = (),
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
                issue_allowed_paths=issue_allowed_paths,
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
        issue_allowed_paths: tuple[str, ...] = (),
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
                issue_allowed_paths=issue_allowed_paths,
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
                issue_allowed_paths=turn.issue_allowed_paths,
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
            "issue_allowed_paths_json",
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
            else json.dumps(turn.issue_allowed_paths, separators=(",", ":"))
            if field == "issue_allowed_paths_json"
            else getattr(turn, field)
            for field in fields
        )
        connection.execute(
            f"INSERT INTO turns ({', '.join(fields)}) "
            f"VALUES ({', '.join('?' for _ in fields)})",
            values,
        )

    @staticmethod
    def _insert_turn_prompt_input(
        connection: sqlite3.Connection, prompt_input: TurnPromptInput
    ) -> None:
        connection.execute(
            "INSERT INTO turn_prompt_inputs (turn_id, prompt_kind, issue_content_sha256, "
            "task_spec_sha256, cumulative_approved_context_sha256, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                prompt_input.turn_id,
                prompt_input.prompt_kind.value,
                prompt_input.issue_content_sha256,
                prompt_input.task_spec_sha256,
                prompt_input.cumulative_approved_context_sha256,
                prompt_input.created_at,
                prompt_input.updated_at,
            ),
        )

    def _require_generation_identity(
        self,
        connection: sqlite3.Connection,
        *,
        session_generation_id: str,
        work_item_id: str,
        generation_number: int,
        policy_sha256: str | None,
        allow_legacy_policy: bool = False,
    ) -> SessionGeneration:
        validate_session_generation_id(session_generation_id)
        if policy_sha256 is None:
            if not allow_legacy_policy:
                raise ValueError("policy_sha256 must be a SHA-256 digest")
        else:
            validate_sha256(policy_sha256, "policy_sha256")
        if type(generation_number) is not int or generation_number <= 0:
            raise ValueError("generation_number must be a positive integer")
        row = connection.execute(
            "SELECT * FROM session_generations WHERE session_generation_id = ?",
            (session_generation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"session generation not found: {session_generation_id}")
        generation = self._row_to_session_generation(row)
        if generation.work_item_id != work_item_id or generation.generation_number != generation_number:
            raise ValueError("session generation identity does not match the requested Turn")
        if policy_sha256 is None:
            if (
                generation.policy_sha256 is not None
                or generation.rotation_reason != "legacy_migration"
            ):
                raise ValueError("legacy session generation policy does not match")
        elif generation.policy_sha256 != policy_sha256:
            raise ValueError("session generation policy does not match")
        return generation

    def _require_bound_generation_turn(
        self,
        connection: sqlite3.Connection,
        *,
        turn_id: str,
        session_generation_id: str,
        generation_number: int,
        policy_sha256: str,
    ) -> tuple[SessionGeneration, Turn]:
        turn = self._require_turn(connection, turn_id)
        generation = self._require_generation_identity(
            connection,
            session_generation_id=session_generation_id,
            work_item_id=turn.work_item_id,
            generation_number=generation_number,
            policy_sha256=policy_sha256,
        )
        row = connection.execute(
            "SELECT session_generation_id FROM turn_session_generations WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if row is None or row["session_generation_id"] != session_generation_id:
            raise ValueError("Turn is not bound to the requested session generation")
        return generation, turn

    def _require_bound_generation_turn_by_turn(
        self, connection: sqlite3.Connection, turn_id: str
    ) -> tuple[SessionGeneration, Turn]:
        turn = self._require_turn(connection, turn_id)
        row = connection.execute(
            "SELECT session_generations.* FROM session_generations "
            "JOIN turn_session_generations USING(session_generation_id) WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Turn is not bound to a session generation")
        return self._row_to_session_generation(row), turn

    def _require_turn(self, connection: sqlite3.Connection, turn_id: str) -> Turn:
        row = connection.execute("SELECT * FROM turns WHERE turn_id = ?", (turn_id,)).fetchone()
        if row is None:
            raise KeyError(f"turn not found: {turn_id}")
        return self._row_to_turn(row)

    def _require_work_item(self, connection: sqlite3.Connection, work_item_id: str) -> WorkItem:
        row = connection.execute(
            "SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"work item not found: {work_item_id}")
        return self._row_to_work_item(row)

    def _require_running_work_item(
        self, connection: sqlite3.Connection, work_item_id: str
    ) -> WorkItem:
        work_item = self._require_work_item(connection, work_item_id)
        if work_item.state not in {WorkItemState.RUNNING, WorkItemState.BLOCKED}:
            raise ValueError("work item is not running")
        return work_item

    @staticmethod
    def _insert_session_generation(
        connection: sqlite3.Connection, generation: SessionGeneration
    ) -> None:
        fields = (
            "session_generation_id",
            "work_item_id",
            "generation_number",
            "state",
            "role",
            "codex_session_id",
            "start_head_sha",
            "last_published_sha",
            "policy_sha256",
            "rotation_reason",
            "baseline_issue_revision",
            "baseline_issue_content_sha256",
            "baseline_task_spec_sha256",
            "baseline_prompt_sha256",
            "baseline_approved_comment_ids_json",
            "baseline_approved_context_sha256",
            "created_at",
            "started_at",
            "retired_at",
            "updated_at",
        )
        values = tuple(
            getattr(generation, field).value
            if field in {"state", "role"}
            else (
                json.dumps(generation.baseline_approved_comment_ids, separators=(",", ":"))
                if generation.baseline_approved_comment_ids is not None
                else None
            )
            if field == "baseline_approved_comment_ids_json"
            else getattr(generation, field)
            for field in fields
        )
        connection.execute(
            f"INSERT INTO session_generations ({', '.join(fields)}) "
            f"VALUES ({', '.join('?' for _ in fields)})",
            values,
        )

    @staticmethod
    def _insert_session_handoff(
        connection: sqlite3.Connection, handoff: SessionHandoffSnapshot
    ) -> None:
        connection.execute(
            "INSERT INTO session_handoffs "
            "(handoff_id, work_item_id, from_session_generation_id, "
            "to_session_generation_id, trusted_facts_json, untrusted_advisory_json, "
            "handoff_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                handoff.handoff_id,
                handoff.work_item_id,
                handoff.from_session_generation_id,
                handoff.to_session_generation_id,
                handoff.trusted_facts_json,
                handoff.untrusted_advisory_json,
                handoff.handoff_sha256,
                handoff.created_at,
            ),
        )

    @staticmethod
    def _publication_checkpoint_matches(
        row: sqlite3.Row,
        session_generation_id: str,
        checkpoint: PublishedCheckpoint,
        commit_count: int,
        size_bytes: int,
    ) -> bool:
        try:
            persisted_paths = StateStore._parse_string_tuple(
                row["changed_paths_json"], "persisted publication changed paths"
            )
        except ValueError:
            return False
        return (
            row["session_generation_id"] == session_generation_id
            and row["previous_sha"] == checkpoint.previous_sha
            and row["head_sha"] == checkpoint.head_sha
            and row["bundle_sha256"] == checkpoint.bundle_sha256
            and persisted_paths == checkpoint.changed_paths
            and row["commit_count"] == commit_count
            and row["size_bytes"] == size_bytes
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

    def foreign_key_violation_count(self) -> int:
        """Return the number of read-only SQLite foreign-key violations."""
        return sum(1 for _ in self._connection.execute("PRAGMA foreign_key_check"))

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
    def _row_to_session_generation(row: sqlite3.Row) -> SessionGeneration:
        values = dict(row)
        values["state"] = SessionGenerationState(values["state"])
        values["role"] = SessionGenerationRole(values["role"])
        raw_comment_ids = values.pop("baseline_approved_comment_ids_json")
        values["baseline_approved_comment_ids"] = (
            None
            if raw_comment_ids is None
            else StateStore._parse_string_tuple(
                raw_comment_ids, "persisted SessionGeneration baseline comment IDs"
            )
        )
        return SessionGeneration(**values)

    @staticmethod
    def _row_to_turn(row: sqlite3.Row) -> Turn:
        values = dict(row)
        values["state"] = TurnState(values["state"])
        raw_comment_ids = values.pop("included_comment_ids_json", "[]")
        raw_allowed_paths = values.pop("issue_allowed_paths_json", "[]")
        values["included_comment_ids"] = StateStore._parse_string_tuple(
            raw_comment_ids, "persisted Turn comment IDs"
        )
        values["issue_allowed_paths"] = StateStore._parse_string_tuple(
            raw_allowed_paths, "persisted Turn allowed paths"
        )
        return Turn(**values)

    @staticmethod
    def _row_to_turn_usage(row: sqlite3.Row) -> TurnUsage:
        return TurnUsage(**dict(row))

    @staticmethod
    def _row_to_turn_context_failure(
        row: sqlite3.Row,
    ) -> TurnContextFailureReceipt:
        values = dict(row)
        values["worktree_clean"] = bool(values["worktree_clean"])
        return TurnContextFailureReceipt(**values)

    @staticmethod
    def _row_to_turn_prompt_input(row: sqlite3.Row) -> TurnPromptInput:
        values = dict(row)
        values["prompt_kind"] = PromptKind(values["prompt_kind"])
        return TurnPromptInput(**values)

    @staticmethod
    def _row_to_completion_gate(row: sqlite3.Row) -> CompletionGateSnapshot:
        values = dict(row)
        values["status"] = CompletionGateStatus(values["status"])
        return CompletionGateSnapshot(**values)

    @staticmethod
    def _row_to_session_handoff(row: sqlite3.Row) -> SessionHandoffSnapshot:
        return SessionHandoffSnapshot(**dict(row))

    @staticmethod
    def _row_to_published_checkpoint(row: sqlite3.Row) -> PublishedCheckpoint:
        return PublishedCheckpoint(
            head_sha=row["head_sha"],
            previous_sha=row["previous_sha"],
            bundle_sha256=row["bundle_sha256"],
            changed_paths=StateStore._parse_string_tuple(
                row["changed_paths_json"], "persisted publication changed paths"
            ),
        )

    @staticmethod
    def _row_to_work_item_archive(row: sqlite3.Row) -> WorkItemArchive:
        values = dict(row)
        values["status"] = WorkItemArchiveStatus(values["status"])
        return WorkItemArchive(**values)

    @staticmethod
    def _row_to_work_item_disposition(row: sqlite3.Row) -> WorkItemDisposition:
        values = dict(row)
        values["kind"] = WorkItemDispositionKind(values["kind"])
        return WorkItemDisposition(**values)

    @staticmethod
    def _row_to_work_item_absence_reconciliation(
        row: sqlite3.Row,
    ) -> WorkItemAbsenceReconciliation:
        return WorkItemAbsenceReconciliation(**dict(row))

    @staticmethod
    def _row_to_slack_delivery(row: sqlite3.Row) -> SlackDeliveryRecord:
        values = dict(row)
        values["kind"] = SlackReportKind(values["kind"])
        values["state"] = SlackDeliveryState(values["state"])
        return SlackDeliveryRecord(**values)

    @staticmethod
    def _parse_string_tuple(value: object, field: str) -> tuple[str, ...]:
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{field} are malformed") from exc
        if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
            raise ValueError(f"{field} are malformed")
        return tuple(parsed)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result
