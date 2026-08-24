from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_dispatcher.github_api_metrics import (
    GitHubApiMetrics,
    GitHubApiSweepOutcome,
)
from codex_dispatcher.state_store import StateStore


class StateStoreTests(unittest.TestCase):
    def test_migration_ledger_is_exact_and_database_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.db"
            expected = StateStore.supported_schema_migration_versions()
            with StateStore(path) as store:
                store.migrate()
                self.assertEqual(expected, store.schema_migration_versions())
                self.assertEqual("ok", store.integrity_check())
            with StateStore(path) as reopened:
                reopened.migrate()
                self.assertEqual(expected, reopened.schema_migration_versions())

    def test_github_api_sweep_metrics_are_strict_durable_and_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.db"
            with StateStore(path) as store:
                store.migrate()
                metrics = GitHubApiMetrics(
                    command_count=4,
                    read_count=3,
                    write_count=1,
                    failure_count=0,
                    elapsed_milliseconds=123,
                    core_remaining=4990,
                    core_limit=5000,
                    core_reset_epoch=2_000_000_000,
                    graphql_remaining=4980,
                    graphql_limit=5000,
                    graphql_reset_epoch=2_000_000_001,
                )
                first = store.record_github_api_sweep(
                    metrics,
                    started_at="2026-08-23T00:00:00+00:00",
                    completed_at="2026-08-23T00:00:01+00:00",
                    outcome=GitHubApiSweepOutcome.SUCCESS,
                    sweep_status="idle",
                )
                second = store.record_github_api_sweep(
                    GitHubApiMetrics(
                        command_count=1,
                        read_count=1,
                        write_count=0,
                        failure_count=1,
                        elapsed_milliseconds=10,
                        rate_limit_error="github_rate_limit_unavailable",
                    ),
                    started_at="2026-08-23T00:01:00+00:00",
                    completed_at="2026-08-23T00:01:01+00:00",
                    outcome=GitHubApiSweepOutcome.FAILURE,
                    error_code="github_sweep_failed",
                )
                self.assertEqual(1, first.sequence)
                self.assertEqual(2, second.sequence)
                self.assertEqual(second, store.get_latest_github_api_sweep())
                self.assertEqual(2, store.github_api_sweep_count())
                with self.assertRaisesRegex(ValueError, "precedes"):
                    store.record_github_api_sweep(
                        metrics,
                        started_at="2026-08-23T00:02:01+00:00",
                        completed_at="2026-08-23T00:02:00+00:00",
                        outcome=GitHubApiSweepOutcome.SUCCESS,
                        sweep_status="idle",
                    )

            with StateStore(path, read_only=True) as reopened:
                self.assertEqual(second, reopened.get_latest_github_api_sweep())

    def test_sweep_cursor_is_strict_and_monotonic_by_explicit_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                self.assertIsNone(store.get_sweep_cursor("terminal_github_audit"))
                first = store.record_sweep_cursor(
                    "terminal_github_audit",
                    completed_at="2026-08-23T00:00:00+00:00",
                )
                second = store.record_sweep_cursor(
                    "terminal_github_audit",
                    completed_at="2026-08-24T00:00:00+00:00",
                )
                self.assertEqual("2026-08-23T00:00:00+00:00", first)
                self.assertEqual(second, store.get_sweep_cursor("terminal_github_audit"))
                with self.assertRaisesRegex(ValueError, "backwards"):
                    store.record_sweep_cursor(
                        "terminal_github_audit",
                        completed_at="2026-08-22T00:00:00+00:00",
                    )
                with self.assertRaises(ValueError):
                    store.get_sweep_cursor("untrusted")


if __name__ == "__main__":
    unittest.main()
