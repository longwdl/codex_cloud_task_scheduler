from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

from codex_dispatcher.handoffs import (
    SessionHandoffSnapshot,
    build_session_handoff_snapshot,
)
from codex_dispatcher.delegation_evidence import DelegatedAgent, DelegationReceipt
from codex_dispatcher.runner_protocol import (
    AgentResult,
    AgentResultStatus,
    TestResult,
    TestStatus,
    NEXT_PROTOCOL_VERSION,
    RunnerOperation,
    RunnerRequest,
)
from codex_dispatcher.runner_transport import (
    RunnerArchiveReply,
    RunnerArchiveState,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.work_items import (
    PromptKind,
    SessionGeneration,
    SessionGenerationRole,
    SessionGenerationState,
    TaskBranchSource,
    TurnState,
    WorkItem,
    WorkItemState,
)
from codex_dispatcher.work_item_lifecycle import WorkItemArchiveStatus
from codex_dispatcher.work_item_lifecycle import WorkItemDispositionKind


SESSION = "123e4567-e89b-12d3-a456-426614174000"


def make_item(issue_number: int) -> WorkItem:
    return WorkItem.new(
        repository="owner/repo",
        issue_number=issue_number,
        issue_node_id=f"I_kwDOFixture{issue_number}",
        base_branch="main",
        base_sha="a" * 40,
        at=f"2026-01-{issue_number:02d}T00:00:00.000000Z",
    )


def rotation_handoff(
    item: WorkItem,
    generation: SessionGeneration,
    *,
    target_id: str,
    target_policy: str,
    reason: str,
) -> SessionHandoffSnapshot:
    return build_session_handoff_snapshot(
        work_item=item,
        from_generation=generation,
        to_session_generation_id=target_id,
        to_generation_number=generation.generation_number + 1,
        to_agent_policy_sha256=target_policy,
        rotation_reason=reason,
        issue_revision="revision",
        issue_content_sha256=(generation.baseline_issue_content_sha256 or "d" * 64),
        task_spec_sha256=(generation.baseline_task_spec_sha256 or "e" * 64),
        approved_context_sha256=(
            generation.baseline_approved_context_sha256 or "1" * 64
        ),
        acceptance_criteria="Fixture acceptance",
        required_checks=("tests",),
        published_checkpoints=(),
        source_turn_id=None,
        source_result_status=None,
        source_result_summary=None,
        source_agent_result=None,
        created_at="2026-08-22T00:00:00Z",
    )


def make_ready(store: StateStore, item: WorkItem) -> WorkItem:
    store.create_work_item(item)
    store.update_work_item_state(item.work_item_id, WorkItemState.PREPARING)
    return store.update_work_item_state(item.work_item_id, WorkItemState.READY)


def migrate_database_through(path: Path, last_version: int) -> None:
    migrations = sorted(
        (
            int(migration.name.split("_", 1)[0]),
            migration,
        )
        for migration in files("codex_dispatcher.migrations").iterdir()
        if migration.name.endswith(".sql")
    )
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version, migration in migrations:
            if version > last_version:
                break
            connection.executescript(migration.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (?, 'legacy')",
                (version,),
            )
        connection.commit()


class WorkItemStateStoreTests(unittest.TestCase):
    def test_migration_13_retires_completed_generation_at_durable_event_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "schema-12.db"
            migrate_database_through(path, 12)
            item = make_item(20)
            completion_time = "2026-02-20T00:00:00+00:00"
            with StateStore(path) as store:
                store.create_work_item(item)
                store._connection.execute(
                    "UPDATE work_items SET state = 'review' WHERE work_item_id = ?",
                    (item.work_item_id,),
                )
                store._connection.execute(
                    "INSERT INTO session_generations "
                    "(session_generation_id, work_item_id, generation_number, state, role, "
                    "codex_session_id, start_head_sha, last_published_sha, policy_sha256, "
                    "rotation_reason, created_at, started_at, retired_at, updated_at) "
                    "VALUES (?, ?, 1, 'active', 'implementation', ?, ?, NULL, NULL, "
                    "'legacy_migration', ?, ?, NULL, ?)",
                    (
                        "sg_" + "8" * 32,
                        item.work_item_id,
                        SESSION,
                        item.base_sha,
                        item.created_at,
                        item.created_at,
                        item.updated_at,
                    ),
                )
                store._connection.execute(
                    "UPDATE work_items SET state = 'completed', updated_at = ? "
                    "WHERE work_item_id = ?",
                    (completion_time, item.work_item_id),
                )
                store._connection.execute(
                    "INSERT INTO work_item_events "
                    "(work_item_id, turn_id, event_type, event_time, payload_json) "
                    "VALUES (?, NULL, 'work_item_state_changed', ?, ?)",
                    (
                        item.work_item_id,
                        completion_time,
                        '{"from":"review","to":"completed"}',
                    ),
                )
                store._connection.commit()

            with StateStore(path) as store:
                store.migrate()
                generation = store.get_session_generation("sg_" + "8" * 32)
                assert generation is not None
                self.assertIs(SessionGenerationState.RETIRED, generation.state)
                self.assertEqual(completion_time, generation.retired_at)
                self.assertEqual(completion_time, generation.updated_at)
                self.assertEqual("ok", store.integrity_check())

    def test_migration_13_guard_rolls_back_completed_active_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "schema-12-invalid.db"
            migrate_database_through(path, 12)
            item = make_item(19)
            with StateStore(path) as store:
                store.create_work_item(item)
                store._connection.execute(
                    "UPDATE work_items SET state = 'completed' WHERE work_item_id = ?",
                    (item.work_item_id,),
                )
                store._connection.execute(
                    "INSERT INTO turns "
                    "(turn_id, work_item_id, turn_number, state, issue_revision, "
                    "prompt_sha256, input_head_sha, created_at, updated_at) "
                    "VALUES (?, ?, 1, 'planned', 'revision', ?, ?, ?, ?)",
                    (
                        "turn_" + "9" * 32,
                        item.work_item_id,
                        "b" * 64,
                        item.base_sha,
                        item.created_at,
                        item.updated_at,
                    ),
                )
                store._connection.commit()

            with StateStore(path) as store:
                with self.assertRaises(sqlite3.IntegrityError):
                    store.migrate()
                versions = store._connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                disposition_table = store._connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'work_item_dispositions'"
                ).fetchone()
            self.assertEqual(
                tuple(range(1, 13)), tuple(row["version"] for row in versions)
            )
            self.assertIsNone(disposition_table)

    def test_completion_and_disposition_terminalize_session_generations_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                completed = make_item(21)
                store.create_work_item(completed)
                for state in (
                    WorkItemState.PREPARING,
                    WorkItemState.READY,
                    WorkItemState.RUNNING,
                    WorkItemState.REVIEW,
                ):
                    store.update_work_item_state(completed.work_item_id, state)
                store._connection.execute(
                    "INSERT INTO session_generations "
                    "(session_generation_id, work_item_id, generation_number, state, role, "
                    "codex_session_id, start_head_sha, last_published_sha, policy_sha256, "
                    "rotation_reason, created_at, started_at, retired_at, updated_at) "
                    "VALUES (?, ?, 1, 'active', 'implementation', ?, ?, NULL, NULL, "
                    "'legacy_migration', ?, ?, NULL, ?)",
                    (
                        "sg_" + "2" * 32,
                        completed.work_item_id,
                        "223e4567-e89b-12d3-a456-426614174000",
                        completed.base_sha,
                        completed.created_at,
                        completed.created_at,
                        completed.updated_at,
                    ),
                )
                store._connection.commit()
                store.update_work_item_state(
                    completed.work_item_id,
                    WorkItemState.COMPLETED,
                    updated_at="2026-02-21T00:00:00+00:00",
                )
                self.assertIs(
                    SessionGenerationState.RETIRED,
                    store.list_session_generations(completed.work_item_id)[0].state,
                )

                disposed = make_item(22)
                store.create_work_item(disposed)
                store.update_work_item_state(
                    disposed.work_item_id, WorkItemState.PREPARING
                )
                store.update_work_item_state(
                    disposed.work_item_id, WorkItemState.BLOCKED
                )
                disposition = store.record_work_item_disposition(
                    disposed.work_item_id,
                    kind=WorkItemDispositionKind.ABANDONED,
                    expected_head_sha=disposed.base_sha,
                    pr_number=None,
                    requested_by="alice",
                    request_event_id="12345",
                    requested_at="2026-02-22T00:00:00+00:00",
                    reason_code="operator_agent_discard",
                    updated_at="2026-02-22T00:01:00+00:00",
                )
                self.assertEqual("12345", disposition.request_event_id)
                self.assertEqual(disposition, store.get_work_item_disposition(disposed.work_item_id))
                with self.assertRaisesRegex(ValueError, "immutable"):
                    store.update_work_item_state(
                        disposed.work_item_id, WorkItemState.PREPARING
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    store._connection.execute(
                        "INSERT INTO turns "
                        "(turn_id, work_item_id, turn_number, state, issue_revision, "
                        "prompt_sha256, input_head_sha, created_at, updated_at) "
                        "VALUES (?, ?, 1, 'planned', 'r', ?, ?, ?, ?)",
                        (
                            "turn_" + "3" * 32,
                            disposed.work_item_id,
                            "4" * 64,
                            disposed.base_sha,
                            disposed.created_at,
                            disposed.updated_at,
                        ),
                    )
                store._connection.rollback()
                archive_request = RunnerRequest(
                    RunnerOperation.ARCHIVE,
                    disposed.work_item_id,
                    version=NEXT_PROTOCOL_VERSION,
                    expected_head_sha=disposed.base_sha,
                )
                store.prepare_work_item_archive(
                    disposed.work_item_id,
                    expected_head_sha=disposed.base_sha,
                    eligible_at=disposition.eligible_at,
                    request_sha256=sha256(
                        archive_request.to_json().encode("utf-8")
                    ).hexdigest(),
                )
                absence = store.record_work_item_absence_reconciliation(
                    disposed.work_item_id,
                    expected_head_sha=disposed.base_sha,
                    evidence_sha256="5" * 64,
                    observed_by="operator",
                    observed_at="2026-02-22T00:02:00+00:00",
                )
                self.assertEqual(
                    absence,
                    store.get_work_item_absence_reconciliation(
                        disposed.work_item_id
                    ),
                )
                self.assertIs(
                    WorkItemArchiveStatus.PREPARED,
                    store.get_work_item_archive(disposed.work_item_id).status,  # type: ignore[union-attr]
                )

    def test_completed_work_item_archive_ledger_is_durable_and_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                work_item = make_item(12)
                store.create_work_item(work_item)
                for state in (
                    WorkItemState.PREPARING,
                    WorkItemState.READY,
                    WorkItemState.RUNNING,
                    WorkItemState.REVIEW,
                ):
                    store.update_work_item_state(work_item.work_item_id, state)
                head_sha = "b" * 40
                store.record_published_sha(
                    work_item.work_item_id,
                    previous_sha=work_item.base_sha,
                    head_sha=head_sha,
                )
                completed_at = "2026-02-01T00:00:00+00:00"
                store.update_work_item_state(
                    work_item.work_item_id,
                    WorkItemState.COMPLETED,
                    updated_at=completed_at,
                )
                self.assertEqual(
                    completed_at,
                    store.get_work_item_completed_at(work_item.work_item_id),
                )
                request = RunnerRequest(
                    RunnerOperation.ARCHIVE,
                    work_item.work_item_id,
                    version=NEXT_PROTOCOL_VERSION,
                    expected_head_sha=head_sha,
                )
                prepared = store.prepare_work_item_archive(
                    work_item.work_item_id,
                    expected_head_sha=head_sha,
                    eligible_at="2026-02-08T00:00:00+00:00",
                    request_sha256=sha256(
                        request.to_json().encode("utf-8")
                    ).hexdigest(),
                )
                self.assertIs(WorkItemArchiveStatus.PREPARED, prepared.status)
                ambiguous = store.mark_work_item_archive_ambiguous(
                    work_item.work_item_id
                )
                self.assertIs(WorkItemArchiveStatus.AMBIGUOUS, ambiguous.status)
                store.mark_work_item_archive_retry_ready(work_item.work_item_id)
                reply = RunnerArchiveReply(
                    RunnerOperation.ARCHIVE,
                    work_item.work_item_id,
                    head_sha,
                    RunnerArchiveState.ARCHIVED,
                    archived_at="2026-02-08T00:01:00+00:00",
                    reclaimed_bytes=1024,
                )
                archived = store.complete_work_item_archive(
                    work_item.work_item_id, reply=reply
                )
                self.assertIs(WorkItemArchiveStatus.ARCHIVED, archived.status)
                self.assertEqual(1024, archived.reclaimed_bytes)
                self.assertEqual(
                    archived,
                    store.complete_work_item_archive(
                        work_item.work_item_id, reply=reply
                    ),
                )
                with self.assertRaises(ValueError):
                    store.prepare_work_item_archive(
                        work_item.work_item_id,
                        expected_head_sha="c" * 40,
                        eligible_at=prepared.eligible_at,
                        request_sha256=prepared.request_sha256,
                    )

    def test_runner_prepare_ack_provenance_survives_later_terminal_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                rejected = make_item(2)
                store.create_work_item(rejected)
                self.assertFalse(
                    store.runner_preparation_was_acknowledged(rejected.work_item_id)
                )
                store.update_work_item_state(
                    rejected.work_item_id, WorkItemState.PREPARING
                )
                store.update_work_item_state(rejected.work_item_id, WorkItemState.BLOCKED)
                self.assertFalse(
                    store.runner_preparation_was_acknowledged(rejected.work_item_id)
                )
                store.update_work_item_state(
                    rejected.work_item_id, WorkItemState.PREPARING
                )
                store.update_work_item_state(rejected.work_item_id, WorkItemState.READY)
                store.update_work_item_state(rejected.work_item_id, WorkItemState.PAUSED)
                self.assertTrue(
                    store.runner_preparation_was_acknowledged(rejected.work_item_id)
                )

                with self.assertRaises(KeyError):
                    store.runner_preparation_was_acknowledged("wi_" + "f" * 24)

    def test_migration_upgrades_an_existing_version_one_database_additively(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.db"
            migration = (
                files("codex_dispatcher.migrations")
                .joinpath("001_initial.sql")
                .read_text(encoding="utf-8")
            )
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(migration)
                connection.execute(
                    "CREATE TABLE schema_migrations "
                    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'legacy')"
                )
                connection.commit()

            with StateStore(path) as store:
                store.migrate()
                self.assertEqual("ok", store.integrity_check())
                store.create_work_item(make_item(9))
                self.assertIsNotNone(store.get_work_item_by_issue("owner/repo", 9))
            with closing(sqlite3.connect(path)) as connection:
                versions = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                legacy_runs = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                self.assertEqual(
                [(version,) for version in range(1, 17)],
                    versions,
                )
            self.assertEqual(0, legacy_runs)

    def test_additive_migration_persists_one_issue_identity_and_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.db"
            item = make_item(1)
            with StateStore(path) as store:
                store.migrate()
                store.create_work_item(item)
                with self.assertRaises(sqlite3.IntegrityError):
                    store.create_work_item(item)
                bound = store.bind_codex_session(item.work_item_id, SESSION)
                self.assertEqual(SESSION, bound.codex_session_id)
                self.assertEqual(bound, store.bind_codex_session(item.work_item_id, SESSION))
                bound = store.bind_slack_thread(
                    item.work_item_id,
                    channel_id="C0BR2D0MS8Y",
                    thread_ts="1234567890.123456",
                )
                self.assertEqual("C0BR2D0MS8Y", bound.slack_channel_id)
                with self.assertRaisesRegex(ValueError, "different Slack thread"):
                    store.bind_slack_thread(
                        item.work_item_id,
                        channel_id="C0BR2D0MS8Y",
                        thread_ts="1234567890.999999",
                    )
                bound = store.bind_draft_pr(item.work_item_id, 17)
                self.assertEqual(17, bound.pr_number)
                self.assertEqual(bound, store.bind_draft_pr(item.work_item_id, 17))
                with self.assertRaisesRegex(ValueError, "different Draft PR"):
                    store.bind_draft_pr(item.work_item_id, 18)
                self.assertEqual(
                    WorkItemState.DISCOVERED,
                    store.update_work_item_state(
                        item.work_item_id, WorkItemState.DISCOVERED
                    ).state,
                )
                with self.assertRaisesRegex(ValueError, "must advance"):
                    store.record_published_sha(
                        item.work_item_id, previous_sha="a" * 40, head_sha="a" * 40
                    )
                published = store.record_published_sha(
                    item.work_item_id, previous_sha="a" * 40, head_sha="b" * 40
                )
                self.assertEqual("b" * 40, published.last_published_sha)
                self.assertEqual(
                    published,
                    store.record_published_sha(
                        item.work_item_id, previous_sha="a" * 40, head_sha="b" * 40
                    ),
                )
                with self.assertRaisesRegex(ValueError, "anchor"):
                    store.record_published_sha(
                        item.work_item_id, previous_sha="a" * 40, head_sha="c" * 40
                    )
            with StateStore(path) as reopened:
                reopened.migrate()
                loaded = reopened.get_work_item_by_issue("owner/repo", 1)
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertEqual(SESSION, loaded.codex_session_id)
            with closing(sqlite3.connect(path)) as connection:
                versions = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                self.assertEqual(
                [(version,) for version in range(1, 17)],
                    versions,
                )

    def test_session_generation_ledger_is_atomic_and_turn_binding_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                item = make_ready(store, make_item(11))
                generation = store.plan_session_generation(
                    item.work_item_id,
                    role=SessionGenerationRole.IMPLEMENTATION,
                    policy_sha256="c" * 64,
                )
                self.assertEqual(1, generation.generation_number)
                self.assertEqual(SessionGenerationState.PLANNED, generation.state)
                self.assertEqual(generation, store.get_live_session_generation(item.work_item_id))
                with self.assertRaises(sqlite3.IntegrityError):
                    store.plan_session_generation(
                        item.work_item_id,
                        role=SessionGenerationRole.AUDIT,
                        policy_sha256="d" * 64,
                    )
                with self.assertRaisesRegex(ValueError, "requires a baseline"):
                    store.transition_session_generation(
                        generation.session_generation_id, SessionGenerationState.STARTING
                    )
                baseline = store.record_session_generation_baseline(
                    generation.session_generation_id,
                    issue_revision="issue-revision-1",
                    issue_content_sha256="d" * 64,
                    task_spec_sha256="e" * 64,
                    prompt_sha256="f" * 64,
                    approved_comment_ids=("IC_baseline_1",),
                    approved_context_sha256="1" * 64,
                )
                self.assertEqual("d" * 64, baseline.baseline_issue_content_sha256)
                self.assertEqual(("IC_baseline_1",), baseline.baseline_approved_comment_ids)
                self.assertEqual(
                    baseline,
                    store.record_session_generation_baseline(
                        generation.session_generation_id,
                        issue_revision="issue-revision-1",
                        issue_content_sha256="d" * 64,
                        task_spec_sha256="e" * 64,
                        prompt_sha256="f" * 64,
                        approved_comment_ids=("IC_baseline_1",),
                        approved_context_sha256="1" * 64,
                    ),
                )
                with self.assertRaisesRegex(ValueError, "different baseline"):
                    store.record_session_generation_baseline(
                        generation.session_generation_id,
                        issue_revision="issue-revision-1",
                        issue_content_sha256="2" * 64,
                        task_spec_sha256="e" * 64,
                        prompt_sha256="f" * 64,
                        approved_comment_ids=("IC_baseline_1",),
                        approved_context_sha256="1" * 64,
                    )
                starting = store.transition_session_generation(
                    generation.session_generation_id, SessionGenerationState.STARTING
                )
                self.assertIsNotNone(starting.started_at)
                with self.assertRaisesRegex(ValueError, "requires a Codex session"):
                    store.transition_session_generation(
                        generation.session_generation_id, SessionGenerationState.ACTIVE
                    )
                bound = store.bind_session_generation_codex_session(
                    starting.session_generation_id, SESSION
                )
                self.assertEqual(
                    bound,
                    store.transition_session_generation(
                        generation.session_generation_id, SessionGenerationState.STARTING
                    ),
                )
                active = store.transition_session_generation(
                    generation.session_generation_id, SessionGenerationState.ACTIVE
                )
                self.assertEqual(SESSION, active.codex_session_id)
                self.assertEqual(
                    active,
                    store.bind_session_generation_codex_session(
                        active.session_generation_id, SESSION
                    ),
                )
                with self.assertRaisesRegex(ValueError, "different Codex session"):
                    store.bind_session_generation_codex_session(
                        active.session_generation_id, "123e4567-e89b-12d3-a456-426614174001"
                    )
                published = store.record_session_generation_published_sha(
                    active.session_generation_id,
                    previous_sha="a" * 40,
                    head_sha="b" * 40,
                )
                self.assertEqual("b" * 40, published.last_published_sha)
                self.assertEqual(
                    published,
                    store.record_session_generation_published_sha(
                        active.session_generation_id,
                        previous_sha="a" * 40,
                        head_sha="b" * 40,
                    ),
                )
                with self.assertRaisesRegex(ValueError, "publication anchor"):
                    store.record_session_generation_published_sha(
                        active.session_generation_id,
                        previous_sha="a" * 40,
                        head_sha="c" * 40,
                    )
                retiring = store.transition_session_generation(
                    active.session_generation_id, SessionGenerationState.RETIRING
                )
                retired = store.transition_session_generation(
                    retiring.session_generation_id, SessionGenerationState.RETIRED
                )
                self.assertIsNotNone(retired.retired_at)
                second = store.plan_session_generation(
                    item.work_item_id,
                    role=SessionGenerationRole.AUDIT,
                    policy_sha256="d" * 64,
                )
                self.assertEqual(2, second.generation_number)
                turn = store.plan_turn(
                    item.work_item_id,
                    turn_id="turn_" + "a" * 32,
                    issue_revision="generation-binding",
                    prompt_sha256="b" * 64,
                    input_head_sha="a" * 40,
                )
                with self.assertRaisesRegex(ValueError, "terminal session generation"):
                    store.bind_turn_session_generation(turn.turn_id, retired.session_generation_id)
                self.assertEqual(
                    second,
                    store.bind_turn_session_generation(
                        turn.turn_id, second.session_generation_id
                    ),
                )
                self.assertEqual(second, store.get_turn_session_generation(turn.turn_id))
                with self.assertRaisesRegex(ValueError, "different session generation"):
                    store.bind_turn_session_generation(turn.turn_id, retired.session_generation_id)

    def test_session_generation_transitions_and_usage_are_strict_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                item = make_ready(store, make_item(12))
                generation = store.plan_session_generation(
                    item.work_item_id,
                    role=SessionGenerationRole.IMPLEMENTATION,
                    policy_sha256="c" * 64,
                )
                with self.assertRaisesRegex(Exception, "cannot transition"):
                    store.transition_session_generation(
                        generation.session_generation_id, SessionGenerationState.ACTIVE
                    )
                failed = store.transition_session_generation(
                    generation.session_generation_id, SessionGenerationState.FAILED
                )
                self.assertIsNone(failed.started_at)
                self.assertIsNotNone(failed.retired_at)
                with self.assertRaisesRegex(ValueError, "starting or active"):
                    store.bind_session_generation_codex_session(
                        failed.session_generation_id, SESSION
                    )
                turn = store.plan_turn(
                    item.work_item_id,
                    turn_id="turn_" + "b" * 32,
                    issue_revision="usage",
                    prompt_sha256="b" * 64,
                    input_head_sha="a" * 40,
                )
                with self.assertRaisesRegex(ValueError, "terminal session generation"):
                    store.bind_turn_session_generation(turn.turn_id, failed.session_generation_id)
                usage = store.record_turn_usage(
                    turn.turn_id,
                    input_tokens=100,
                    cached_input_tokens=10,
                    cache_write_input_tokens=5,
                    output_tokens=20,
                    reasoning_output_tokens=7,
                )
                self.assertEqual(usage, store.get_turn_usage(turn.turn_id))
                self.assertEqual(
                    usage,
                    store.record_turn_usage(
                        turn.turn_id,
                        input_tokens=100,
                        cached_input_tokens=10,
                        cache_write_input_tokens=5,
                        output_tokens=20,
                        reasoning_output_tokens=7,
                    ),
                )
                with self.assertRaisesRegex(ValueError, "different recorded usage"):
                    store.record_turn_usage(
                        turn.turn_id,
                        input_tokens=101,
                        cached_input_tokens=10,
                        cache_write_input_tokens=5,
                        output_tokens=20,
                        reasoning_output_tokens=7,
                    )
                with self.assertRaisesRegex(ValueError, "non-negative"):
                    store.record_turn_usage(
                        turn.turn_id,
                        input_tokens=-1,
                        cached_input_tokens=0,
                        cache_write_input_tokens=0,
                        output_tokens=0,
                        reasoning_output_tokens=0,
                    )

    def test_begin_generation_turn_is_atomic_and_persists_prompt_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                item = make_ready(store, make_item(15))
                generation = store.plan_session_generation(
                    item.work_item_id,
                    role=SessionGenerationRole.IMPLEMENTATION,
                    policy_sha256="c" * 64,
                )
                with self.assertRaisesRegex(ValueError, "non-empty"):
                    store.begin_session_generation_turn(
                        item.work_item_id,
                        session_generation_id=generation.session_generation_id,
                        generation_number=1,
                        policy_sha256="c" * 64,
                        prompt_kind=PromptKind.FULL,
                        issue_revision="revision",
                        issue_content_sha256="d" * 64,
                        task_spec_sha256="e" * 64,
                        prompt_sha256="f" * 64,
                        approved_comment_ids=("IC_fixture",),
                        approved_context_sha256="1" * 64,
                        issue_allowed_paths=(),
                        input_head_sha="a" * 40,
                    )
                self.assertEqual((), store.list_turns(item.work_item_id))
                self.assertEqual(SessionGenerationState.PLANNED, store.get_session_generation(
                    generation.session_generation_id
                ).state)
                running, starting, turn, prompt_input = store.begin_session_generation_turn(
                    item.work_item_id,
                    session_generation_id=generation.session_generation_id,
                    generation_number=1,
                    policy_sha256="c" * 64,
                    prompt_kind=PromptKind.FULL,
                    issue_revision="revision",
                    issue_content_sha256="d" * 64,
                    task_spec_sha256="e" * 64,
                    prompt_sha256="f" * 64,
                    approved_comment_ids=("IC_fixture",),
                    approved_context_sha256="1" * 64,
                    issue_allowed_paths=("src",),
                    input_head_sha="a" * 40,
                )
                self.assertEqual(WorkItemState.RUNNING, running.state)
                self.assertEqual(SessionGenerationState.STARTING, starting.state)
                self.assertEqual(TurnState.STARTING, turn.state)
                self.assertEqual(PromptKind.FULL, prompt_input.prompt_kind)
                self.assertEqual(prompt_input, store.get_turn_prompt_input(turn.turn_id))
                self.assertEqual(
                    (turn,),
                    store.list_session_generation_turns(generation.session_generation_id),
                )

    def test_pre_session_retry_proof_is_rechecked_inside_state_store(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                item = make_ready(store, make_item(16))
                first = store.plan_session_generation(
                    item.work_item_id,
                    role=SessionGenerationRole.IMPLEMENTATION,
                    policy_sha256="c" * 64,
                )
                _, _, turn, _ = store.begin_session_generation_turn(
                    item.work_item_id,
                    session_generation_id=first.session_generation_id,
                    generation_number=1,
                    policy_sha256="c" * 64,
                    prompt_kind=PromptKind.FULL,
                    issue_revision="revision",
                    issue_content_sha256="d" * 64,
                    task_spec_sha256="e" * 64,
                    prompt_sha256="f" * 64,
                    approved_comment_ids=(),
                    approved_context_sha256="1" * 64,
                    issue_allowed_paths=("src",),
                    input_head_sha="a" * 40,
                )
                store.record_generation_turn_failed(
                    turn.turn_id,
                    session_generation_id=first.session_generation_id,
                    generation_number=1,
                    policy_sha256="c" * 64,
                    error_code="runner_unexpected_artifact",
                )
                store.update_work_item_state(item.work_item_id, WorkItemState.READY)
                second = store.plan_session_generation(
                    item.work_item_id,
                    role=SessionGenerationRole.IMPLEMENTATION,
                    policy_sha256="c" * 64,
                )

                with self.assertRaisesRegex(
                    ValueError, "not a definitive START rejection"
                ):
                    store.begin_session_generation_turn(
                        item.work_item_id,
                        session_generation_id=second.session_generation_id,
                        generation_number=2,
                        policy_sha256="c" * 64,
                        prompt_kind=PromptKind.FULL,
                        issue_revision="revision",
                        issue_content_sha256="d" * 64,
                        task_spec_sha256="e" * 64,
                        prompt_sha256="f" * 64,
                        approved_comment_ids=(),
                        approved_context_sha256="1" * 64,
                        issue_allowed_paths=("src",),
                        input_head_sha="a" * 40,
                        pre_session_retry_without_handoff=True,
                    )

                self.assertEqual(WorkItemState.READY, store.get_work_item(item.work_item_id).state)
                self.assertEqual(
                    SessionGenerationState.PLANNED,
                    store.get_session_generation(second.session_generation_id).state,
                )
                self.assertEqual(1, len(store.list_turns(item.work_item_id)))

    def test_finished_generation_turn_persists_delegation_receipt_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                item = make_ready(store, make_item(17))
                generation = store.plan_session_generation(
                    item.work_item_id,
                    role=SessionGenerationRole.IMPLEMENTATION,
                    policy_sha256="c" * 64,
                )
                _, _, turn, _ = store.begin_session_generation_turn(
                    item.work_item_id,
                    session_generation_id=generation.session_generation_id,
                    generation_number=1,
                    policy_sha256="c" * 64,
                    prompt_kind=PromptKind.FULL,
                    issue_revision="revision",
                    issue_content_sha256="d" * 64,
                    task_spec_sha256="e" * 64,
                    prompt_sha256="f" * 64,
                    approved_comment_ids=(),
                    approved_context_sha256="1" * 64,
                    issue_allowed_paths=("src",),
                    input_head_sha="a" * 40,
                )
                child_session = "223e4567-e89b-12d3-a456-426614174000"
                receipt = DelegationReceipt(
                    codex_version="0.147.0",
                    root_thread_id=SESSION,
                    root_model="gpt-5.6-sol",
                    root_reasoning_effort="xhigh",
                    agents=(
                        DelegatedAgent(
                            parent_thread_id=SESSION,
                            child_thread_id=child_session,
                            agent_name="terra_worker",
                            model="gpt-5.6-terra",
                            reasoning_effort="medium",
                            edge_status="closed",
                            tokens_used=42,
                        ),
                    ),
                )
                result = AgentResult(
                    AgentResultStatus.COMPLETED,
                    "complete",
                    (),
                    (TestResult("unit", TestStatus.PASSED),),
                    (),
                    "review",
                )

                reviewed, _, finished, _ = store.record_generation_turn_finished(
                    turn.turn_id,
                    session_generation_id=generation.session_generation_id,
                    generation_number=1,
                    policy_sha256="c" * 64,
                    session_id=SESSION,
                    output_sha256="2" * 64,
                    output_head_sha="a" * 40,
                    result_status="completed",
                    result_summary="complete",
                    agent_result=result,
                    input_tokens=100,
                    cached_input_tokens=10,
                    cache_write_input_tokens=0,
                    output_tokens=20,
                    reasoning_output_tokens=5,
                    delegation_receipt=receipt,
                )

                self.assertEqual(WorkItemState.RUNNING, reviewed.state)
                self.assertEqual(TurnState.PUBLISHED, finished.state)
                self.assertEqual(receipt, store.get_turn_delegation_receipt(turn.turn_id))
                event = store._connection.execute(
                    "SELECT payload_json FROM work_item_events "
                    "WHERE turn_id = ? AND event_type = 'turn_delegation_observed'",
                    (turn.turn_id,),
                ).fetchone()
                self.assertIsNotNone(event)
                self.assertNotIn(child_session, event["payload_json"])

    def test_rotate_session_generation_is_atomic_and_requires_idle_ready_work_item(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                item = make_ready(store, make_item(16))
                current = store.plan_session_generation(
                    item.work_item_id,
                    role=SessionGenerationRole.IMPLEMENTATION,
                    policy_sha256="c" * 64,
                )
                store.record_session_generation_baseline(
                    current.session_generation_id,
                    issue_revision="revision",
                    issue_content_sha256="d" * 64,
                    task_spec_sha256="e" * 64,
                    prompt_sha256="f" * 64,
                    approved_comment_ids=("IC_fixture",),
                    approved_context_sha256="1" * 64,
                )
                store.transition_session_generation(
                    current.session_generation_id, SessionGenerationState.STARTING
                )
                store.bind_session_generation_codex_session(current.session_generation_id, SESSION)
                active = store.transition_session_generation(
                    current.session_generation_id, SessionGenerationState.ACTIVE
                )
                with self.assertRaisesRegex(ValueError, "identity"):
                    store.rotate_session_generation(
                        item.work_item_id,
                        current_session_generation_id=active.session_generation_id,
                        current_generation_number=2,
                        expected_current_policy_sha256="c" * 64,
                        new_policy_sha256="c" * 64,
                        rotation_reason="identity-conflict",
                    )
                self.assertEqual(
                    SessionGenerationState.ACTIVE,
                    store.get_session_generation(active.session_generation_id).state,
                )
                with self.assertRaisesRegex(ValueError, "requires a handoff"):
                    store.rotate_session_generation(
                        item.work_item_id,
                        current_session_generation_id=active.session_generation_id,
                        current_generation_number=1,
                        expected_current_policy_sha256="c" * 64,
                        new_policy_sha256="d" * 64,
                        rotation_reason="missing-handoff",
                    )
                self.assertEqual(
                    SessionGenerationState.ACTIVE,
                    store.get_session_generation(active.session_generation_id).state,
                )
                with self.assertRaisesRegex(ValueError, "handoff identity"):
                    store.rotate_session_generation(
                        item.work_item_id,
                        current_session_generation_id=active.session_generation_id,
                        current_generation_number=1,
                        expected_current_policy_sha256="c" * 64,
                        new_policy_sha256="d" * 64,
                        rotation_reason="requested-reason",
                        handoff=rotation_handoff(
                            item,
                            active,
                            target_id="sg_" + "3" * 32,
                            target_policy="d" * 64,
                            reason="different-reason",
                        ),
                    )
                self.assertEqual(
                    SessionGenerationState.ACTIVE,
                    store.get_session_generation(active.session_generation_id).state,
                )
                retired, replacement = store.rotate_session_generation(
                    item.work_item_id,
                    current_session_generation_id=active.session_generation_id,
                    current_generation_number=1,
                    expected_current_policy_sha256="c" * 64,
                    new_policy_sha256="d" * 64,
                    rotation_reason="approved-context-changed",
                    new_role=SessionGenerationRole.AUDIT,
                    new_session_generation_id="sg_" + "2" * 32,
                    handoff=rotation_handoff(
                        item,
                        active,
                        target_id="sg_" + "2" * 32,
                        target_policy="d" * 64,
                        reason="approved-context-changed",
                    ),
                )
                self.assertEqual(SessionGenerationState.RETIRED, retired.state)
                self.assertEqual(SessionGenerationState.PLANNED, replacement.state)
                self.assertEqual(2, replacement.generation_number)
                self.assertEqual("a" * 40, replacement.start_head_sha)
                self.assertEqual("d" * 64, replacement.policy_sha256)
                self.assertEqual(replacement, store.get_live_session_generation(item.work_item_id))
                with self.assertRaisesRegex(ValueError, "only an active"):
                    store.rotate_session_generation(
                        item.work_item_id,
                        current_session_generation_id=active.session_generation_id,
                        current_generation_number=1,
                        expected_current_policy_sha256="c" * 64,
                        new_policy_sha256="d" * 64,
                        rotation_reason="cannot-reuse-retired",
                    )

        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                item = make_ready(store, make_item(18))
                legacy_id = "sg_" + "8" * 32
                store._connection.execute(
                    "INSERT INTO session_generations (session_generation_id, work_item_id, "
                    "generation_number, state, role, codex_session_id, start_head_sha, "
                    "policy_sha256, rotation_reason, created_at, started_at, updated_at) "
                    "VALUES (?, ?, 1, 'active', 'implementation', ?, ?, NULL, "
                    "'legacy_migration', ?, ?, ?)",
                    (
                        legacy_id,
                        item.work_item_id,
                        SESSION,
                        "a" * 40,
                        item.created_at,
                        item.created_at,
                        item.updated_at,
                    ),
                )
                store._connection.commit()
                retired, replacement = store.rotate_session_generation(
                    item.work_item_id,
                    current_session_generation_id=legacy_id,
                    current_generation_number=1,
                    expected_current_policy_sha256=None,
                    new_policy_sha256="d" * 64,
                    rotation_reason="legacy-policy-upgrade",
                    new_session_generation_id="sg_" + "9" * 32,
                    handoff=rotation_handoff(
                        item,
                        store.get_session_generation(legacy_id),
                        target_id="sg_" + "9" * 32,
                        target_policy="d" * 64,
                        reason="legacy-policy-upgrade",
                    ),
                )
                self.assertEqual(SessionGenerationState.RETIRED, retired.state)
                self.assertEqual("d" * 64, replacement.policy_sha256)

        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                item = make_ready(store, make_item(17))
                current = store.plan_session_generation(
                    item.work_item_id,
                    role=SessionGenerationRole.IMPLEMENTATION,
                    policy_sha256="c" * 64,
                )
                store.record_session_generation_baseline(
                    current.session_generation_id,
                    issue_revision="revision",
                    issue_content_sha256="d" * 64,
                    task_spec_sha256="e" * 64,
                    prompt_sha256="f" * 64,
                    approved_comment_ids=("IC_fixture",),
                    approved_context_sha256="1" * 64,
                )
                store.transition_session_generation(
                    current.session_generation_id, SessionGenerationState.STARTING
                )
                store.bind_session_generation_codex_session(current.session_generation_id, SESSION)
                active = store.transition_session_generation(
                    current.session_generation_id, SessionGenerationState.ACTIVE
                )
                store.plan_turn(
                    item.work_item_id,
                    turn_id="turn_" + "9" * 32,
                    issue_revision="active-turn",
                    prompt_sha256="b" * 64,
                    input_head_sha="a" * 40,
                )
                with self.assertRaisesRegex(ValueError, "active Turn"):
                    store.rotate_session_generation(
                        item.work_item_id,
                        current_session_generation_id=active.session_generation_id,
                        current_generation_number=1,
                        expected_current_policy_sha256="c" * 64,
                        new_policy_sha256="c" * 64,
                        rotation_reason="blocked-by-turn",
                    )
                self.assertEqual(
                    SessionGenerationState.ACTIVE,
                    store.get_session_generation(active.session_generation_id).state,
                )

    def test_migration_imports_legacy_session_and_turns_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.db"
            item = make_item(13)
            with closing(sqlite3.connect(path)) as connection:
                for version in range(1, 7):
                    migration = files("codex_dispatcher.migrations").joinpath(
                        f"{version:03d}_" + {
                            1: "initial", 2: "work_items", 3: "task_branch_source",
                            4: "turn_context", 5: "turn_allowed_paths", 6: "slack_outbox",
                        }[version] + ".sql"
                    ).read_text(encoding="utf-8")
                    connection.executescript(migration)
                connection.execute(
                    "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                connection.executemany(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'legacy')",
                    [(version,) for version in range(1, 7)],
                )
                connection.execute(
                    "INSERT INTO work_items (work_item_id, repository, issue_number, issue_node_id, "
                    "state, base_branch, task_branch, runner_directory, codex_session_id, base_sha, "
                    "last_published_sha, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (item.work_item_id, item.repository, item.issue_number, item.issue_node_id,
                     item.state.value, item.base_branch, item.task_branch, item.runner_directory, SESSION,
                     item.base_sha, "b" * 40, item.created_at, item.updated_at),
                )
                connection.execute(
                    "INSERT INTO turns (turn_id, work_item_id, turn_number, state, issue_revision, "
                    "prompt_sha256, input_head_sha, created_at, updated_at) VALUES (?, ?, 1, 'finished', ?, ?, ?, ?, ?)",
                    ("turn_" + "c" * 32, item.work_item_id, "legacy", "d" * 64,
                     "a" * 40, item.created_at, item.updated_at),
                )
                connection.commit()
            with StateStore(path) as store:
                store.migrate()
                imported = store.get_live_session_generation(item.work_item_id)
                self.assertIsNotNone(imported)
                assert imported is not None
                self.assertEqual("sg_" + item.work_item_id[3:], imported.session_generation_id)
                self.assertEqual(SESSION, imported.codex_session_id)
                self.assertEqual("b" * 40, imported.last_published_sha)
                self.assertIsNone(imported.policy_sha256)
                self.assertEqual(imported, store.get_turn_session_generation("turn_" + "c" * 32))

    def test_session_generation_database_checks_reject_bypassed_invalid_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                item = make_item(14)
                store.create_work_item(item)

                def insert_generation(
                    generation_id: str,
                    *,
                    state: str,
                    codex_session_id: str | None = None,
                    policy_sha256: str | None = "c" * 64,
                    rotation_reason: str | None = None,
                    started_at: str | None = None,
                    baseline_issue_revision: str | None = None,
                    baseline_issue_content_sha256: str | None = None,
                    baseline_task_spec_sha256: str | None = None,
                    baseline_prompt_sha256: str | None = None,
                    baseline_approved_comment_ids_json: str | None = None,
                    baseline_approved_context_sha256: str | None = None,
                ) -> None:
                    store._connection.execute(
                        "INSERT INTO session_generations ("
                        "session_generation_id, work_item_id, generation_number, state, role, "
                        "codex_session_id, start_head_sha, policy_sha256, rotation_reason, "
                        "baseline_issue_revision, baseline_issue_content_sha256, "
                        "baseline_task_spec_sha256, baseline_prompt_sha256, "
                        "baseline_approved_comment_ids_json, "
                        "baseline_approved_context_sha256, created_at, started_at, updated_at"
                        ") VALUES (?, ?, 1, ?, 'implementation', ?, ?, ?, ?, ?, ?, ?, ?, "
                        "?, ?, ?, ?, ?)",
                        (
                            generation_id,
                            item.work_item_id,
                            state,
                            codex_session_id,
                            "a" * 40,
                            policy_sha256,
                            rotation_reason,
                            baseline_issue_revision,
                            baseline_issue_content_sha256,
                            baseline_task_spec_sha256,
                            baseline_prompt_sha256,
                            baseline_approved_comment_ids_json,
                            baseline_approved_context_sha256,
                            item.created_at,
                            started_at,
                            item.updated_at,
                        ),
                    )

                with self.assertRaises(sqlite3.IntegrityError):
                    insert_generation(
                        "sg_" + "1" * 32,
                        state="planned",
                        baseline_issue_revision="partial",
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    insert_generation(
                        "sg_" + "2" * 32,
                        state="active",
                        policy_sha256=None,
                        rotation_reason="legacy_migration",
                        started_at=item.created_at,
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    insert_generation(
                        "sg_" + "3" * 32,
                        state="starting",
                        baseline_issue_revision="revision",
                        baseline_issue_content_sha256="d" * 64,
                        baseline_task_spec_sha256="e" * 64,
                        baseline_prompt_sha256="f" * 64,
                        baseline_approved_comment_ids_json='["IC_baseline"]',
                        baseline_approved_context_sha256="1" * 64,
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    insert_generation(
                        "sg_" + "4" * 32,
                        state="active",
                        codex_session_id=SESSION,
                        started_at=item.created_at,
                    )
                insert_generation(
                    "sg_" + "5" * 32,
                    state="active",
                    codex_session_id=SESSION,
                    policy_sha256=None,
                    rotation_reason="legacy_migration",
                    started_at=item.created_at,
                )
                self.assertIsNotNone(store.get_session_generation("sg_" + "5" * 32))

    def test_persists_a_verified_migrated_task_branch_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.db"
            item = WorkItem.from_existing_branch_binding(
                repository="owner/repo",
                issue_number=7,
                issue_node_id="I_kwDOFixture7",
                base_branch="main",
                base_sha="a" * 40,
                task_branch="codex/issue-7-8e3775879000",
                at="2026-01-07T00:00:00.000000Z",
            )
            with StateStore(path) as store:
                store.migrate()
                store.create_work_item(item)
            with StateStore(path, read_only=True) as reopened:
                loaded = reopened.get_work_item(item.work_item_id)

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(TaskBranchSource.MIGRATED, loaded.task_branch_source)
            self.assertEqual(item.task_branch, loaded.task_branch)

    def test_database_enforces_one_active_turn_globally_and_orders_followups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                first = make_ready(store, make_item(1))
                second = make_ready(store, make_item(2))
                self.assertEqual((first, second), store.list_work_items())
                self.assertIsNone(store.get_active_turn())
                self.assertEqual(1, store.next_turn_number(first.work_item_id))
                turn = store.plan_turn(
                    first.work_item_id,
                    turn_id="turn_" + "1" * 32,
                    issue_revision="revision-1",
                    prompt_sha256="b" * 64,
                    input_head_sha="a" * 40,
                    included_comment_ids=("IC_fixture",),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.plan_turn(
                        second.work_item_id,
                        turn_id="turn_" + "2" * 32,
                        issue_revision="revision-2",
                        prompt_sha256="c" * 64,
                        input_head_sha="a" * 40,
                    )
                store.update_turn_state(turn.turn_id, TurnState.STARTING)
                self.assertEqual(turn.turn_id, store.get_active_turn().turn_id)
                self.assertEqual(
                    TurnState.STARTING,
                    store.update_turn_state(turn.turn_id, TurnState.STARTING).state,
                )
                store.update_turn_state(turn.turn_id, TurnState.RUNNING)
                recorded = store.record_turn_result(
                    turn.turn_id,
                    output_sha256="e" * 64,
                    output_head_sha="b" * 40,
                    result_status="needs_input",
                    result_summary="Need input\nfrom the Issue",
                )
                self.assertEqual("e" * 64, recorded.output_sha256)
                self.assertEqual(("IC_fixture",), recorded.included_comment_ids)
                self.assertEqual(
                    recorded,
                    store.record_turn_result(
                        turn.turn_id,
                        output_sha256="e" * 64,
                        output_head_sha="b" * 40,
                        result_status="needs_input",
                        result_summary="Need input\nfrom the Issue",
                    ),
                )
                with self.assertRaisesRegex(ValueError, "different recorded result"):
                    store.record_turn_result(
                        turn.turn_id,
                        output_sha256="f" * 64,
                        output_head_sha="b" * 40,
                        result_status="needs_input",
                        result_summary="different",
                    )
                store.update_turn_state(turn.turn_id, TurnState.NEEDS_INPUT)

                next_turn = store.plan_turn(
                    first.work_item_id,
                    turn_id="turn_" + "3" * 32,
                    issue_revision="revision-3",
                    prompt_sha256="d" * 64,
                    input_head_sha="a" * 40,
                )
                self.assertEqual(2, next_turn.turn_number)
                self.assertEqual(
                    [1, 2], [item.turn_number for item in store.list_turns(first.work_item_id)]
                )

    def test_begin_turn_rejects_a_stale_prompt_turn_number_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                item = make_ready(store, make_item(6))

                with self.assertRaisesRegex(RuntimeError, "Prompt snapshot"):
                    store.begin_turn(
                        item.work_item_id,
                        issue_revision="revision-1",
                        prompt_sha256="b" * 64,
                        input_head_sha="a" * 40,
                        expected_turn_number=2,
                    )

                self.assertEqual((), store.list_turns(item.work_item_id))
                self.assertIsNone(store.get_active_turn())
                self.assertEqual(WorkItemState.READY, store.get_work_item(item.work_item_id).state)

    def test_records_bounded_turn_error_idempotently_before_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                item = make_ready(store, make_item(8))
                _, turn = store.begin_turn(
                    item.work_item_id,
                    turn_id="turn_" + "8" * 32,
                    issue_revision="revision-error",
                    prompt_sha256="b" * 64,
                    input_head_sha="a" * 40,
                    included_comment_ids=("IC_fixture_1", "IC_fixture_2"),
                    issue_allowed_paths=("src", "tests"),
                )
                self.assertEqual(
                    ("IC_fixture_1", "IC_fixture_2"),
                    store.get_turn(turn.turn_id).included_comment_ids,
                )
                self.assertEqual(
                    ("src", "tests"),
                    store.get_turn(turn.turn_id).issue_allowed_paths,
                )
                store.update_turn_state(turn.turn_id, TurnState.STARTING)
                recorded = store.record_turn_error(
                    turn.turn_id, error_code="agent_result_invalid"
                )
                self.assertEqual("agent_result_invalid", recorded.error_code)
                self.assertEqual(
                    recorded,
                    store.record_turn_error(
                        turn.turn_id, error_code="agent_result_invalid"
                    ),
                )
                with self.assertRaisesRegex(ValueError, "different recorded error"):
                    store.record_turn_error(turn.turn_id, error_code="different_error")

    def test_completed_or_nonready_work_item_cannot_plan_a_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                item = make_item(3)
                store.create_work_item(item)
                with self.assertRaisesRegex(ValueError, "must be ready"):
                    store.plan_turn(
                        item.work_item_id,
                        issue_revision="revision",
                        prompt_sha256="b" * 64,
                        input_head_sha="a" * 40,
                    )

    def test_one_draft_pr_cannot_be_bound_to_two_work_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                first = make_item(4)
                second = make_item(5)
                store.create_work_item(first)
                store.create_work_item(second)
                store.bind_draft_pr(first.work_item_id, 21)
                with self.assertRaises(sqlite3.IntegrityError):
                    store.bind_draft_pr(second.work_item_id, 21)


if __name__ == "__main__":
    unittest.main()
