from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from codex_dispatcher.config import (
    Config,
    RepositoryConfig,
    SchedulerConfig,
    SshRuntimeConfig,
    ToolPins,
)
from codex_dispatcher.lifecycle_health import (
    SYSTEMD_SERVICE_UNITS,
    SYSTEMD_TIMER_UNITS,
    SystemdUnitState,
    inspect_lifecycle_health,
    inspect_runner_capacity,
    inspect_systemd_health,
)
from codex_dispatcher.github_api_metrics import (
    GitHubApiMetrics,
    GitHubApiSweepOutcome,
)
from codex_dispatcher.runner_transport import (
    RunnerCapacityReply,
    RunnerInactiveContainerState,
    RunnerTransportInterrupted,
    RunnerWireOutput,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.work_items import (
    PromptKind,
    SessionGenerationRole,
    WorkItem,
    WorkItemState,
)


HEAD = "b" * 40


def _config(database: Path) -> Config:
    return Config(
        scheduler=SchedulerConfig(database, database.parent / "repos", 120, 1),
        tools=ToolPins("git", "gh", "codex", "ssh"),
        repositories=(
            RepositoryConfig(
                "owner/repo", "main", "env", 1, ("README.md",), (), ("owner",), ("ci",)
            ),
        ),
        ssh_runtime=SshRuntimeConfig(
            git_path=Path("/usr/bin/git"),
            gh_path=Path("/usr/bin/gh"),
            ssh_path=Path("/usr/bin/ssh"),
            host="runner.internal",
            user="codex-runner",
            port=22,
            known_hosts_path=Path("/etc/known_hosts"),
            identity_file=Path("/etc/runner_key"),
            lock_path=Path("/run/codex-dispatcher/dispatcher.lock"),
            mirror_root=Path("/var/lib/codex-dispatcher/mirrors"),
            source_temporary_root=Path("/var/lib/codex-dispatcher/source-temporary"),
            quarantine_root=Path("/var/lib/codex-dispatcher/quarantine"),
            publisher_temporary_root=Path("/var/lib/codex-dispatcher/publisher-temporary"),
            runner_root="/srv/codex-runner/work-items",
            connect_timeout_seconds=10,
            operation_timeout_seconds=3900,
            completed_retention_seconds=7 * 24 * 60 * 60,
        ),
    )


def _item(issue_number: int, at: str) -> WorkItem:
    return WorkItem.new(
        repository="owner/repo",
        issue_number=issue_number,
        issue_node_id=f"I_fixture_{issue_number}",
        base_branch="main",
        base_sha="a" * 40,
        at=at,
    )


def _complete(store: StateStore, item: WorkItem, at: str) -> None:
    store.create_work_item(item)
    store._connection.execute(
        "UPDATE work_items SET state = 'completed', last_published_sha = ?, updated_at = ? "
        "WHERE work_item_id = ?",
        (HEAD, at, item.work_item_id),
    )
    store._connection.execute(
        "INSERT INTO work_item_events "
        "(work_item_id, turn_id, event_type, event_time, payload_json) "
        "VALUES (?, NULL, 'work_item_state_changed', ?, ?)",
        (item.work_item_id, at, '{"from":"review","to":"completed"}'),
    )
    store._connection.commit()


def _seed_observability(store: StateStore, at: str) -> None:
    store.record_sweep_cursor("terminal_github_audit", completed_at=at)
    store.record_github_api_sweep(
        GitHubApiMetrics(
            command_count=1,
            read_count=1,
            write_count=0,
            failure_count=0,
            elapsed_milliseconds=10,
            core_remaining=4900,
            core_limit=5000,
            core_reset_epoch=2_000_000_000,
            graphql_remaining=4800,
            graphql_limit=5000,
            graphql_reset_epoch=2_000_000_001,
        ),
        started_at=at,
        completed_at=at,
        outcome=GitHubApiSweepOutcome.SUCCESS,
        sweep_status="idle",
    )


class LifecycleHealthTests(unittest.TestCase):
    def test_inactive_turn_abandonment_is_immediately_visible(self) -> None:
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        at = now.isoformat()
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "state.db"
            with StateStore(database) as store:
                store.migrate()
                _seed_observability(store, at)
                item = _item(6, at)
                store.create_work_item(item)
                store.update_work_item_state(
                    item.work_item_id, WorkItemState.PREPARING, updated_at=at
                )
                store.update_work_item_state(
                    item.work_item_id, WorkItemState.READY, updated_at=at
                )
                generation = store.plan_session_generation(
                    item.work_item_id,
                    role=SessionGenerationRole.IMPLEMENTATION,
                    policy_sha256="c" * 64,
                    created_at=at,
                )
                _, generation, turn, _ = store.begin_session_generation_turn(
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
                store.record_generation_turn_unknown(
                    turn.turn_id,
                    session_generation_id=generation.session_generation_id,
                    generation_number=1,
                    policy_sha256="c" * 64,
                    updated_at=at,
                )
                store.record_generation_turn_abandoned(
                    turn.turn_id,
                    session_generation_id=generation.session_generation_id,
                    generation_number=1,
                    policy_sha256="c" * 64,
                    session_id=None,
                    inactive_container_state=RunnerInactiveContainerState.ABSENT,
                    inactive_observed_at=at,
                    recorded_at=at,
                )
                snapshot = inspect_lifecycle_health(
                    _config(database), store, now=now
                )
        self.assertEqual(1, snapshot.abandoned_turns)
        self.assertEqual(
            {"runner_turn_abandoned_inactive"},
            {alert.code for alert in snapshot.alerts},
        )

    def test_runner_capacity_health_reports_thresholds_and_unavailability(self) -> None:
        class CapacityTransport:
            def invoke(self, request, *, stdin=b"", source_artifact=None):
                return RunnerWireOutput(
                    RunnerCapacityReply(
                        capacity_bytes=1_000,
                        available_bytes=200,
                        image_size_bytes=200,
                        host_reserve_bytes=200,
                        turn_admissible=True,
                        provision_admissible=False,
                        provision_shortfall_bytes=200,
                    ).to_json().encode("utf-8")
                )

        reply, alerts = inspect_runner_capacity(
            _config(Path("/tmp/state.db")), transport=CapacityTransport()
        )
        self.assertIsNotNone(reply)
        self.assertEqual({"runner_provision_capacity_low"}, {item.code for item in alerts})

        class UnavailableTransport:
            def invoke(self, request, *, stdin=b"", source_artifact=None):
                raise RunnerTransportInterrupted("fixture unavailable")

        reply, alerts = inspect_runner_capacity(
            _config(Path("/tmp/state.db")), transport=UnavailableTransport()
        )
        self.assertIsNone(reply)
        self.assertEqual("runner_capacity_unavailable", alerts[0].code)
    def test_reports_long_blocked_overdue_completed_and_ambiguous_archive(self) -> None:
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        old = "2026-08-01T00:00:00+00:00"
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "state.db"
            with StateStore(database) as store:
                store.migrate()
                _seed_observability(store, now.isoformat())
                blocked = _item(1, old)
                store.create_work_item(blocked)
                store.update_work_item_state(
                    blocked.work_item_id, WorkItemState.BLOCKED, updated_at=old
                )

                overdue = _item(2, old)
                _complete(store, overdue, old)

                ambiguous = _item(3, old)
                _complete(store, ambiguous, old)
                store.prepare_work_item_archive(
                    ambiguous.work_item_id,
                    expected_head_sha=HEAD,
                    eligible_at=old,
                    request_sha256="c" * 64,
                    updated_at=old,
                )
                store.mark_work_item_archive_ambiguous(
                    ambiguous.work_item_id, updated_at=old
                )

                snapshot = inspect_lifecycle_health(
                    _config(database), store, now=now
                )

            self.assertFalse(snapshot.ok)
            self.assertEqual("ok", snapshot.integrity)
            self.assertEqual(0, snapshot.foreign_key_violations)
            self.assertEqual(1, snapshot.blocked_work_items)
            self.assertEqual(1, snapshot.pending_archives)
            self.assertEqual(1, snapshot.ambiguous_archives)
            self.assertEqual(
                {
                    "archive_ambiguous",
                    "completed_archive_overdue",
                    "work_item_blocked_too_long",
                },
                {alert.code for alert in snapshot.alerts},
            )

    def test_recent_completed_and_blocked_items_are_healthy(self) -> None:
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        recent = "2026-08-22T12:00:00+00:00"
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "state.db"
            with StateStore(database) as store:
                store.migrate()
                _seed_observability(store, now.isoformat())
                blocked = _item(4, recent)
                store.create_work_item(blocked)
                store.update_work_item_state(
                    blocked.work_item_id, WorkItemState.BLOCKED, updated_at=recent
                )
                completed = _item(5, recent)
                _complete(store, completed, recent)
                snapshot = inspect_lifecycle_health(
                    _config(database), store, now=now
                )

            self.assertTrue(snapshot.ok)
            self.assertEqual((), snapshot.alerts)

    def test_absence_receipt_is_one_terminal_storage_state(self) -> None:
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        at = now.isoformat()
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "state.db"
            with StateStore(database) as store:
                store.migrate()
                _seed_observability(store, at)
                item = _item(7, at)
                _complete(store, item, at)
                store.prepare_work_item_archive(
                    item.work_item_id,
                    expected_head_sha=HEAD,
                    eligible_at=at,
                    request_sha256="c" * 64,
                    updated_at=at,
                )
                store.record_work_item_absence_reconciliation(
                    item.work_item_id,
                    expected_head_sha=HEAD,
                    evidence_sha256="d" * 64,
                    observed_by="operator",
                    observed_at=at,
                    created_at=at,
                )
                snapshot = inspect_lifecycle_health(
                    _config(database), store, now=now
                )

        self.assertTrue(snapshot.ok)
        self.assertEqual(1, snapshot.absence_reconciliations)
        self.assertEqual(0, snapshot.pending_archives)
        self.assertEqual(
            1,
            dict(snapshot.terminal_storage_effective_counts)[
                "absence_reconciled"
            ],
        )
        self.assertEqual(
            snapshot.terminal_storage_effective_counts,
            tuple(
                snapshot.to_mapping()["terminal_storage_effective_counts"].items()
            ),
        )

    def test_observability_health_uses_persisted_metric_and_cursor_age(self) -> None:
        now = datetime(2026, 8, 23, tzinfo=timezone.utc)
        old = "2026-08-20T00:00:00+00:00"
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "state.db"
            with StateStore(database) as store:
                store.migrate()
                missing = inspect_lifecycle_health(_config(database), store, now=now)
                self.assertEqual(
                    {"github_api_metrics_missing", "terminal_github_audit_missing"},
                    {alert.code for alert in missing.alerts},
                )
                store.record_sweep_cursor(
                    "terminal_github_audit", completed_at=old
                )
                store.record_github_api_sweep(
                    GitHubApiMetrics(
                        command_count=2,
                        read_count=2,
                        write_count=0,
                        failure_count=1,
                        elapsed_milliseconds=20,
                        core_remaining=4900,
                        core_limit=5000,
                        core_reset_epoch=2_000_000_000,
                        graphql_remaining=4800,
                        graphql_limit=5000,
                        graphql_reset_epoch=2_000_000_001,
                    ),
                    started_at=now.isoformat(),
                    completed_at=now.isoformat(),
                    outcome=GitHubApiSweepOutcome.FAILURE,
                    error_code="github_sweep_failed",
                )
                observed = inspect_lifecycle_health(
                    _config(database), store, now=now
                )

        self.assertEqual(1, observed.github_api_sweeps)
        self.assertEqual(0, observed.github_api_latest_age_seconds)
        self.assertGreater(observed.terminal_github_audit_age_seconds or 0, 0)
        self.assertEqual(
            {"github_api_last_sweep_failed", "terminal_github_audit_stale"},
            {alert.code for alert in observed.alerts},
        )

    def test_systemd_inspection_requires_enabled_timers_and_successful_services(self) -> None:
        def reader(unit: str) -> SystemdUnitState:
            if unit == "codex-dispatcher.timer":
                return SystemdUnitState(unit, "loaded", "inactive", "disabled", "success")
            if unit == "codex-dispatcher.service":
                return SystemdUnitState(unit, "loaded", "failed", "static", "exit-code")
            return SystemdUnitState(
                unit,
                "loaded",
                "active" if unit in SYSTEMD_TIMER_UNITS else "inactive",
                "enabled" if unit in SYSTEMD_TIMER_UNITS else "static",
                "success",
            )

        states, alerts = inspect_systemd_health(reader=reader)

        self.assertEqual(len(SYSTEMD_TIMER_UNITS) + len(SYSTEMD_SERVICE_UNITS), len(states))
        self.assertEqual(
            {"systemd_timer_not_active", "systemd_service_failed"},
            {alert.code for alert in alerts},
        )


if __name__ == "__main__":
    unittest.main()
