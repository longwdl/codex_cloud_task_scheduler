from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codex_dispatcher.config import (
    SessionRuntimeConfig,
    SlackRuntimeConfig,
    SshRuntimeConfig,
)
from codex_dispatcher.contract import ContractCheck
from codex_dispatcher.control_sweep import (
    ControlSweepResult,
    ControlSweepStatus,
    SshControlSweep,
)
from codex_dispatcher.fixture_faults import (
    FIXTURE_REPOSITORY,
    FixtureFaultInjection,
    FixtureFaultPoint,
)
from codex_dispatcher.git_publisher import GitTaskBranchPublisher
from codex_dispatcher.github_actions_evidence import GitHubActionsEvidenceImporter
from codex_dispatcher.github_api_metrics import (
    GitHubApiMetrics,
    GitHubApiSweepOutcome,
)
from codex_dispatcher.slack_web_api import SlackWebApiPublisher
from codex_dispatcher.ssh_preflight import SshPreflightStatus
from codex_dispatcher.ssh_runtime import (
    SshRuntimeError,
    build_ssh_control_sweep,
    build_ssh_fixture_fault_sweep,
    load_protected_ssh_config,
    run_ssh_control_sweep,
    run_ssh_preflight,
    validate_runtime_state_path,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.testing.fakes import FakeTracker
from codex_dispatcher.trackers.github_cli import GitHubCliTracker
from tests.test_config import SSH_RUNTIME, VALID
from tests.test_scheduler import make_config


TOKEN = "github_pat_runtime_fixture"


def _protected_file(path: Path, *, executable: bool = False) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n" if executable else "fixture\n", encoding="utf-8")
    path.chmod(0o700 if executable else 0o600)
    return path


class SshRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir(mode=0o700)
        self.git_path = _protected_file(self.bin_dir / "git", executable=True)
        self.gh_path = _protected_file(self.bin_dir / "gh", executable=True)
        self.ssh_path = _protected_file(self.bin_dir / "ssh", executable=True)
        self.known_hosts = _protected_file(self.root / "known_hosts")
        self.identity = _protected_file(self.root / "identity")
        self.lock_dir = self.root / "run"
        self.lock_dir.mkdir(mode=0o700)
        runtime = SshRuntimeConfig(
            git_path=self.git_path,
            gh_path=self.gh_path,
            ssh_path=self.ssh_path,
            host="runner.internal",
            user="codex",
            port=22,
            known_hosts_path=self.known_hosts,
            identity_file=self.identity,
            lock_path=self.lock_dir / "dispatcher.lock",
            mirror_root=self.root / "mirrors",
            source_temporary_root=self.root / "source-temporary",
            quarantine_root=self.root / "quarantine",
            publisher_temporary_root=self.root / "publisher-temporary",
            runner_root="/srv/codex-runner/work-items",
            connect_timeout_seconds=10,
            operation_timeout_seconds=3900,
        )
        base = make_config(global_max_active=1, repository_max_active=1)
        self.config = replace(
            base,
            tools=replace(base.tools, ssh_version="9.6"),
            scheduler=replace(
                base.scheduler,
                database_path=self.root / "state.db",
                workspace_root=self.root / "workspace",
            ),
            ssh_runtime=runtime,
            session_runtime=SessionRuntimeConfig(
                protocol_version=2,
                agent_policy_digest="a" * 64,
                max_turns_per_session=4,
                rotate_after_input_tokens=120000,
                rotate_after_session_age_seconds=14400,
                rotate_before_final_audit=True,
                use_incremental_resume_prompts=True,
                max_session_generations=3,
                max_total_turns=10,
                max_no_progress_turns=2,
                max_repair_cycles=3,
                max_audit_cycles=3,
                max_total_tokens=1000000,
                max_work_item_age_seconds=604800,
            ),
        )
        self.store = StateStore(self.config.scheduler.database_path)
        self.store.migrate()

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_build_assembles_ports_without_network_or_runner_invocation(self) -> None:
        sweep = build_ssh_control_sweep(
            config=self.config,
            store=self.store,
            github_token=TOKEN,
        )

        self.assertIsInstance(sweep, SshControlSweep)

        self.assertIsInstance(sweep._tracker, GitHubCliTracker)
        self.assertIsInstance(sweep._publisher, GitTaskBranchPublisher)
        self.assertIsInstance(
            sweep._dispatch._ci_evidence_importer,
            GitHubActionsEvidenceImporter,
        )
        self.assertIs(
            sweep._tracker._metrics,
            sweep._dispatch._ci_evidence_importer._metrics,
        )
        self.assertFalse((self.root / "mirrors").exists())
        self.assertFalse((self.root / "quarantine").exists())
        self.assertFalse((self.root / "publisher-temporary").exists())
        self.assertFalse((self.lock_dir / "dispatcher.lock").exists())

    def test_configured_slack_runtime_requires_token_and_assembles_real_publisher(self) -> None:
        config = replace(
            self.config,
            slack_runtime=SlackRuntimeConfig(
                issue_channel_id="C0BR2D0MS8Y",
                system_channel_id="C0BS3LPG43G",
                request_timeout_seconds=10,
                idempotency_contract="client_msg_id-live-fixture-verified-v1",
            ),
        )
        with self.assertRaisesRegex(SshRuntimeError, "Slack runtime"):
            build_ssh_control_sweep(
                config=config,
                store=self.store,
                github_token=TOKEN,
            )

        sweep = build_ssh_control_sweep(
            config=config,
            store=self.store,
            github_token=TOKEN,
            slack_token="xoxb-1234567890-fixture",
        )

        self.assertIsNotNone(sweep._slack_delivery)
        assert sweep._slack_delivery is not None
        self.assertIsInstance(
            sweep._slack_delivery._publisher,
            SlackWebApiPublisher,
        )
        self.assertEqual(
            "C0BR2D0MS8Y",
            sweep._slack_delivery._channel_id,
        )
        self.assertFalse((self.lock_dir / "dispatcher.lock").exists())

        with self.assertRaisesRegex(SshRuntimeError, "idempotency proof"):
            build_ssh_control_sweep(
                config=replace(
                    config,
                    slack_runtime=replace(
                        config.slack_runtime,
                        idempotency_contract="unverified",
                    ),
                ),
                store=self.store,
                github_token=TOKEN,
                slack_token="xoxb-1234567890-fixture",
            )

    def test_fixture_fault_builder_requires_only_the_hard_coded_repository(self) -> None:
        injection = FixtureFaultInjection(
            FixtureFaultPoint.PUBLISHER_RECEIPT,
            7,
        )
        with self.assertRaisesRegex(SshRuntimeError, "fixed Fixture"):
            build_ssh_fixture_fault_sweep(
                config=self.config,
                store=self.store,
                github_token=TOKEN,
                injection=injection,
            )

        fixture_config = replace(
            self.config,
            repositories=(
                replace(
                    self.config.repositories[0],
                    slug=FIXTURE_REPOSITORY,
                    allowed_paths=("README.md",),
                    denied_paths=(),
                    maintainers=("longwdl",),
                    required_checks=("fixture",),
                ),
            ),
        )
        with self.assertRaisesRegex(SshRuntimeError, "contract"):
            build_ssh_fixture_fault_sweep(
                config=replace(
                    fixture_config,
                    repositories=(
                        replace(
                            fixture_config.repositories[0],
                            allowed_paths=("src",),
                        ),
                    ),
                ),
                store=self.store,
                github_token=TOKEN,
                injection=injection,
            )
        with patch(
            "codex_dispatcher.ssh_runtime.run_control_host_contract_checks",
            return_value=(ContractCheck("tools", True, "fixture"),),
        ):
            sweep = build_ssh_fixture_fault_sweep(
                config=fixture_config,
                store=self.store,
                github_token=TOKEN,
                injection=injection,
            )

        self.assertIsInstance(sweep, SshControlSweep)

        ssh_injection = FixtureFaultInjection(
            FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL,
            7,
        )
        with patch(
            "codex_dispatcher.ssh_runtime.run_control_host_contract_checks",
            return_value=(ContractCheck("tools", True, "fixture"),),
        ):
            ssh_sweep = build_ssh_fixture_fault_sweep(
                config=fixture_config,
                store=self.store,
                github_token=TOKEN,
                injection=ssh_injection,
            )
        guarded_transport = ssh_sweep._dispatch._orchestrator._transport
        self.assertIsNotNone(guarded_transport._delegate._process_started_hook)

        slack_config = replace(
            fixture_config,
            slack_runtime=SlackRuntimeConfig(
                issue_channel_id="C0BR2D0MS8Y",
                system_channel_id="C0BS3LPG43G",
                request_timeout_seconds=10,
                idempotency_contract="client_msg_id-live-fixture-verified-v1",
            ),
        )
        slack_injection = FixtureFaultInjection(
            FixtureFaultPoint.SLACK_ROOT_RECEIPT,
            7,
        )
        with (
            patch(
                "codex_dispatcher.ssh_runtime.run_control_host_contract_checks",
                return_value=(ContractCheck("tools", True, "fixture"),),
            ),
            self.assertRaisesRegex(SshRuntimeError, "Slack runtime"),
        ):
            build_ssh_fixture_fault_sweep(
                config=slack_config,
                store=self.store,
                github_token=TOKEN,
                injection=slack_injection,
            )
        with patch(
            "codex_dispatcher.ssh_runtime.run_control_host_contract_checks",
            return_value=(ContractCheck("tools", True, "fixture"),),
        ):
            slack_sweep = build_ssh_fixture_fault_sweep(
                config=slack_config,
                store=self.store,
                github_token=TOKEN,
                slack_token="xoxb-1234567890-fixture",
                injection=slack_injection,
            )
        assert slack_sweep._slack_delivery is not None
        self.assertIsInstance(
            slack_sweep._slack_delivery._publisher._delegate,
            SlackWebApiPublisher,
        )

    def test_live_config_and_sqlite_paths_must_be_owned_and_protected(self) -> None:
        config_path = self.root / "dispatcher.toml"
        configured = VALID.replace(
            'codex_version = "0.1.0"',
            'codex_version = "0.1.0"\nssh_version = "9.6"',
        )
        config_path.write_text(configured + SSH_RUNTIME, encoding="utf-8")
        config_path.chmod(0o600)

        loaded = load_protected_ssh_config(config_path)
        self.assertIsNotNone(loaded.ssh_runtime)
        validate_runtime_state_path(self.config.scheduler.database_path)

        config_path.chmod(0o666)
        with self.assertRaisesRegex(SshRuntimeError, "config"):
            load_protected_ssh_config(config_path)
        config_path.chmod(0o600)
        self.root.chmod(0o777)
        with self.assertRaisesRegex(SshRuntimeError, "config directory"):
            load_protected_ssh_config(config_path)
        self.root.chmod(0o700)
        self.config.scheduler.database_path.chmod(0o666)
        with self.assertRaisesRegex(SshRuntimeError, "database file"):
            validate_runtime_state_path(self.config.scheduler.database_path)

    def test_runtime_rejects_missing_settings_concurrency_and_weak_tools(self) -> None:
        for config, message in (
            (replace(self.config, ssh_runtime=None), "configuration is required"),
            (replace(self.config, session_runtime=None), "session_runtime"),
            (
                replace(
                    self.config,
                    scheduler=replace(
                        self.config.scheduler,
                        global_max_active=2,
                    ),
                ),
                "max_active",
            ),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(SshRuntimeError, message):
                    build_ssh_control_sweep(
                        config=config,
                        store=self.store,
                        github_token=TOKEN,
                    )

        self.git_path.chmod(0o777)
        with self.assertRaisesRegex(SshRuntimeError, "git_path"):
            build_ssh_control_sweep(
                config=self.config,
                store=self.store,
                github_token=TOKEN,
            )

    def test_tool_contract_failure_never_enters_sweep(self) -> None:
        sweep = SimpleNamespace(run_once=lambda: self.fail("sweep must not run"))
        with (
            patch(
                "codex_dispatcher.ssh_runtime._assemble_ssh_control_sweep",
                return_value=sweep,
            ),
            patch(
                "codex_dispatcher.ssh_runtime.run_control_host_contract_checks",
                return_value=(ContractCheck("git", False, "version drift"),),
            ),
        ):
            with self.assertRaisesRegex(SshRuntimeError, "git"):
                run_ssh_control_sweep(
                    config=self.config,
                    store=self.store,
                    github_token=TOKEN,
                )

    def test_verified_contract_executes_exactly_one_sweep(self) -> None:
        result = ControlSweepResult(ControlSweepStatus.IDLE)
        sweep = SimpleNamespace(run_once=Mock(return_value=result))
        with (
            patch(
                "codex_dispatcher.ssh_runtime._assemble_ssh_control_sweep",
                return_value=sweep,
            ),
            patch(
                "codex_dispatcher.ssh_runtime.run_control_host_contract_checks",
                return_value=(
                    ContractCheck("git", True, "2.55.0"),
                    ContractCheck("gh", True, "2.97.0"),
                ),
            ),
        ):
            observed = run_ssh_control_sweep(
                config=self.config,
                store=self.store,
                github_token=TOKEN,
            )

        self.assertEqual(result, observed)
        sweep.run_once.assert_called_once_with()

    def test_verified_sweep_persists_success_and_failure_metrics(self) -> None:
        metrics = GitHubApiMetrics(
            command_count=2,
            read_count=2,
            write_count=0,
            failure_count=0,
            elapsed_milliseconds=5,
            core_remaining=4990,
            core_limit=5000,
            core_reset_epoch=2_000_000_000,
            graphql_remaining=4980,
            graphql_limit=5000,
            graphql_reset_epoch=2_000_000_001,
        )
        result = ControlSweepResult(ControlSweepStatus.IDLE)
        success = SimpleNamespace(
            run_once=Mock(return_value=result),
            collect_github_api_metrics=Mock(return_value=metrics),
        )
        checks = (ContractCheck("git", True, "2.55.0"),)
        with (
            patch(
                "codex_dispatcher.ssh_runtime._assemble_ssh_control_sweep",
                return_value=success,
            ),
            patch(
                "codex_dispatcher.ssh_runtime.run_control_host_contract_checks",
                return_value=checks,
            ),
        ):
            observed = run_ssh_control_sweep(
                config=self.config,
                store=self.store,
                github_token=TOKEN,
            )
        self.assertEqual(metrics, observed.github_api)
        latest = self.store.get_latest_github_api_sweep()
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(GitHubApiSweepOutcome.SUCCESS, latest.outcome)
        self.assertEqual("idle", latest.sweep_status)

        failed_metrics = replace(metrics, failure_count=1)
        failure = SimpleNamespace(
            run_once=Mock(side_effect=RuntimeError("bounded fixture failure")),
            collect_github_api_metrics=Mock(return_value=failed_metrics),
        )
        with (
            patch(
                "codex_dispatcher.ssh_runtime._assemble_ssh_control_sweep",
                return_value=failure,
            ),
            patch(
                "codex_dispatcher.ssh_runtime.run_control_host_contract_checks",
                return_value=checks,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "bounded fixture"):
                run_ssh_control_sweep(
                    config=self.config,
                    store=self.store,
                    github_token=TOKEN,
                )
        latest = self.store.get_latest_github_api_sweep()
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(GitHubApiSweepOutcome.FAILURE, latest.outcome)
        self.assertEqual("github_sweep_failed", latest.error_code)

    def test_preflight_uses_temporary_snapshot_and_only_tracker_reads(self) -> None:
        tracker = FakeTracker()
        database_bytes = self.config.scheduler.database_path.read_bytes()
        checks = (
            ContractCheck("git", True, "2.55.0"),
            ContractCheck("gh", True, "2.97.0"),
            ContractCheck("ssh", True, "9.6"),
        )
        with (
            patch(
                "codex_dispatcher.ssh_runtime.run_control_host_contract_checks",
                return_value=checks,
            ),
            patch(
                "codex_dispatcher.ssh_runtime.GitHubCliTracker",
                return_value=tracker,
            ),
        ):
            inspection = run_ssh_preflight(
                config=self.config,
                github_token=TOKEN,
            )

        self.assertIs(SshPreflightStatus.IDLE, inspection.plan.status)
        self.assertTrue(inspection.database_preexisting)
        self.assertEqual(checks, inspection.checks)
        self.assertEqual(database_bytes, self.config.scheduler.database_path.read_bytes())
        self.assertFalse((self.root / "mirrors").exists())
        self.assertFalse((self.root / "quarantine").exists())
        self.assertFalse((self.root / "publisher-temporary").exists())
        self.assertFalse((self.lock_dir / "dispatcher.lock").exists())
        self.assertFalse(
            any(
                call.method
                in {"claim", "set_state", "upsert_run_comment", "create_draft_pr"}
                for call in tracker.calls
            )
        )

    def test_preflight_does_not_create_a_missing_configured_database(self) -> None:
        tracker = FakeTracker()
        missing_path = self.root / "missing-state.db"
        config = replace(
            self.config,
            scheduler=replace(
                self.config.scheduler,
                database_path=missing_path,
            ),
        )
        with (
            patch(
                "codex_dispatcher.ssh_runtime.run_control_host_contract_checks",
                return_value=(ContractCheck("git", True, "2.55.0"),),
            ),
            patch(
                "codex_dispatcher.ssh_runtime.GitHubCliTracker",
                return_value=tracker,
            ),
        ):
            inspection = run_ssh_preflight(config=config, github_token=TOKEN)

        self.assertFalse(inspection.database_preexisting)
        self.assertFalse(missing_path.exists())

    def test_preflight_tool_failure_happens_before_github_or_state_snapshot(self) -> None:
        missing_path = self.root / "preflight-must-not-create.db"
        config = replace(
            self.config,
            scheduler=replace(
                self.config.scheduler,
                database_path=missing_path,
            ),
        )
        with (
            patch(
                "codex_dispatcher.ssh_runtime.run_control_host_contract_checks",
                return_value=(ContractCheck("ssh", False, "version drift"),),
            ),
            patch("codex_dispatcher.ssh_runtime.GitHubCliTracker") as tracker,
        ):
            with self.assertRaisesRegex(SshRuntimeError, "ssh"):
                run_ssh_preflight(config=config, github_token=TOKEN)

        tracker.assert_not_called()
        self.assertFalse(missing_path.exists())


if __name__ == "__main__":
    unittest.main()
