from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from importlib.resources import files
from pathlib import Path

from codex_dispatcher.state_store import StateStore
from codex_dispatcher.work_items import TurnState, WorkItem, WorkItemState


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


def make_ready(store: StateStore, item: WorkItem) -> WorkItem:
    store.create_work_item(item)
    store.update_work_item_state(item.work_item_id, WorkItemState.PREPARING)
    return store.update_work_item_state(item.work_item_id, WorkItemState.READY)


class WorkItemStateStoreTests(unittest.TestCase):
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
            self.assertEqual([(1,), (2,)], versions)
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
            self.assertEqual([(1,), (2,)], versions)

    def test_database_enforces_one_active_turn_globally_and_orders_followups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                first = make_ready(store, make_item(1))
                second = make_ready(store, make_item(2))
                turn = store.plan_turn(
                    first.work_item_id,
                    turn_id="turn_" + "1" * 32,
                    issue_revision="revision-1",
                    prompt_sha256="b" * 64,
                    input_head_sha="a" * 40,
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
