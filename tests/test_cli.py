from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_dispatcher.cli import main, run_once_dry_run
from codex_dispatcher.config import SlackRuntimeConfig
from codex_dispatcher.contract import ContractCheck
from codex_dispatcher.control_host_backup import StateBackupResult
from codex_dispatcher.control_sweep import ControlSweepResult, ControlSweepStatus
from codex_dispatcher.domain import Run
from codex_dispatcher.ssh_preflight import (
    SshPreflightPlan,
    SshPreflightStatus,
)
from codex_dispatcher.ssh_recovery import SshRecoveryAction
from codex_dispatcher.ssh_runtime import SshPreflightInspection
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.testing.fakes import FakeTracker
from tests.test_scheduler import make_config


class CliTests(unittest.TestCase):
    def test_runner_capacity_reports_turn_and_provision_boundaries(self) -> None:
        snapshot = SimpleNamespace(
            capacity_bytes=1000,
            available_bytes=200,
            image_size_bytes=100,
            host_reserve_bytes=150,
            turn_admissible=True,
            provision_admissible=False,
            provision_shortfall_bytes=50,
        )
        configuration = SimpleNamespace(
            work_item_disk=object(),
            work_items_root=Path("/srv/codex-runner/work-items"),
        )
        disk = SimpleNamespace(capacity_snapshot=lambda: snapshot)
        stdout = io.StringIO()
        with (
            patch(
                "codex_dispatcher.runner_main.load_runner_configuration",
                return_value=configuration,
            ),
            patch(
                "codex_dispatcher.runner_disk.FusedWorkItemDisk",
                return_value=disk,
            ),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                ["runner-capacity", "--config", "/srv/codex-runner/etc/config.json", "--json"]
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(payload["turn_admissible"])
        self.assertFalse(payload["provision_admissible"])
        self.assertEqual(50, payload["provision_shortfall_bytes"])

    def test_doctor_json_is_read_only_and_machine_readable(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(["doctor", "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("offline-core", payload["phase"])
        self.assertIn("tool:gh", {item["name"] for item in payload["checks"]})

    def test_doctor_contract_requires_config_and_reports_checks(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(["doctor", "--contract", "--json"])
        self.assertEqual(1, exit_code)
        self.assertFalse(json.loads(stdout.getvalue())["ok"])

        stdout = io.StringIO()
        with (
            patch("codex_dispatcher.cli.load_config", return_value=make_config()),
            patch(
                "codex_dispatcher.contract.run_contract_checks",
                return_value=(ContractCheck("git", True, "2.55.0"),),
            ),
            patch(
                "codex_dispatcher.cli.shutil.which",
                side_effect=lambda tool: f"/usr/bin/{tool}",
            ),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                ["doctor", "--config", "config.toml", "--contract", "--json"]
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertIn("contract:git", {item["name"] for item in payload["checks"]})

    def test_status_rejects_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "missing.db"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(["status", "--database", str(database), "--json"])

        self.assertEqual(1, exit_code)
        self.assertFalse(json.loads(stdout.getvalue())["ok"])

    def test_injected_dry_run_does_not_require_external_adapters(self) -> None:
        tracker = FakeTracker()
        plan = run_once_dry_run(make_config(), tracker)
        self.assertEqual((), plan.selected)
        self.assertEqual(1, len(tracker.calls))

    def test_status_reads_an_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "state.db"
            with StateStore(database) as store:
                store.migrate()
                store.create_run(
                    Run.new(
                        run_id="run-1",
                        repository="owner/repo",
                        issue_number=1,
                        prompt_sha256="a" * 64,
                        base_branch="main",
                        cloud_environment_id="env-1",
                    )
                )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["status", "--database", str(database), "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual("ok", payload["integrity"])
        self.assertEqual(["run-1"], payload["active_runs"])

    def test_state_backup_uses_protected_config_without_credentials(self) -> None:
        config = make_config()
        database = Path("/var/lib/codex-dispatcher/state.db")
        config = replace(
            config,
            scheduler=replace(config.scheduler, database_path=database),
        )
        result = StateBackupResult(
            database.parent / "backups/state-20260820T123232.123456Z.db",
            4096,
            "ok",
        )
        stdout = io.StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "codex_dispatcher.ssh_runtime.load_protected_ssh_config",
                return_value=config,
            ),
            patch(
                "codex_dispatcher.control_host_backup.create_state_backup",
                return_value=result,
            ) as create,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                ["state-backup", "--config", "/etc/codex-dispatcher/config.toml", "--json"]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(payload["state_backup"])
        self.assertEqual("ok", payload["integrity"])
        create.assert_called_once_with(database, database.parent / "backups")

    def test_run_once_requires_explicit_dry_run(self) -> None:
        with self.assertRaises(SystemExit):
            main(["run-once", "--config", "config.toml"])

    def test_ssh_run_once_requires_cli_and_environment_opt_in(self) -> None:
        with self.assertRaises(SystemExit):
            main(["ssh-run-once", "--config", "config.toml"])

        stdout = io.StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("codex_dispatcher.ssh_runtime.load_protected_ssh_config") as load,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "ssh-run-once",
                    "--apply",
                    "--config",
                    "config.toml",
                    "--json",
                ]
            )
        self.assertEqual(1, exit_code)
        self.assertIn("ENABLE_SSH_WRITES", json.loads(stdout.getvalue())["error"])
        load.assert_not_called()

    def test_ssh_absence_requires_apply_gate_and_emits_bounded_receipt(self) -> None:
        with self.assertRaises(SystemExit):
            main(
                [
                    "ssh-reconcile-absence",
                    "--config",
                    "config.toml",
                    "--repository",
                    "owner/repo",
                    "--issue-number",
                    "24",
                ]
            )

        token = "github_pat_absence_fixture"
        with tempfile.TemporaryDirectory() as temp_dir:
            base = make_config(global_max_active=1)
            config = replace(
                base,
                scheduler=replace(
                    base.scheduler, database_path=Path(temp_dir) / "state.db"
                ),
            )
            receipt = SimpleNamespace(
                work_item_id="wi_" + "a" * 24,
                expected_head_sha="b" * 40,
                evidence_sha256="c" * 64,
                observed_by="runner_protocol_v2",
                observed_at="2026-08-23T00:00:00+00:00",
            )
            stdout = io.StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {
                        "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                        "GITHUB_TOKEN": token,
                    },
                    clear=True,
                ),
                patch(
                    "codex_dispatcher.ssh_runtime.load_protected_ssh_config",
                    return_value=config,
                ),
                patch(
                    "codex_dispatcher.ssh_runtime.run_ssh_absence_reconciliation",
                    return_value=receipt,
                ) as reconcile,
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "ssh-reconcile-absence",
                        "--apply",
                        "--config",
                        "config.toml",
                        "--repository",
                        "owner/repo",
                        "--issue-number",
                        "24",
                        "--json",
                    ]
                )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ssh_absence_reconciliation"])
        self.assertEqual("c" * 64, payload["evidence_sha256"])
        self.assertNotIn(token, stdout.getvalue())
        self.assertEqual(24, reconcile.call_args.kwargs["issue_number"])

    def test_ssh_run_once_requires_recognized_explicit_token(self) -> None:
        stdout = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {
                    "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                    "GITHUB_TOKEN": "unknown-token",
                },
                clear=True,
            ),
            patch("codex_dispatcher.ssh_runtime.load_protected_ssh_config") as load,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "ssh-run-once",
                    "--apply",
                    "--config",
                    "config.toml",
                    "--json",
                ]
            )
        self.assertEqual(1, exit_code)
        self.assertIn("token", json.loads(stdout.getvalue())["error"])
        load.assert_not_called()

    def test_ssh_preflight_is_read_only_and_needs_no_write_opt_in(self) -> None:
        token = "github_pat_ssh_preflight_fixture"
        config = replace(
            make_config(global_max_active=1),
            slack_runtime=SlackRuntimeConfig(
                channel_id="C0BR2D0MS8Y",
                request_timeout_seconds=10,
                idempotency_contract="client_msg_id-live-fixture-verified-v1",
            ),
        )
        inspection = SshPreflightInspection(
            SshPreflightPlan(
                status=SshPreflightStatus.IDLE,
                recovery_action=SshRecoveryAction.IDLE,
            ),
            (ContractCheck("git", True, "2.55.0"),),
            False,
        )
        stdout = io.StringIO()
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": token}, clear=True),
            patch(
                "codex_dispatcher.ssh_runtime.load_protected_ssh_config",
                return_value=config,
            ),
            patch("codex_dispatcher.ssh_runtime.validate_runtime_state_path"),
            patch(
                "codex_dispatcher.ssh_runtime.run_ssh_preflight",
                return_value=inspection,
            ) as run_preflight,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                ["ssh-preflight", "--config", "config.toml", "--json"]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["external_writes"])
        self.assertFalse(payload["authorizes_apply"])
        self.assertEqual("idle", payload["status"])
        self.assertNotIn(token, stdout.getvalue())
        self.assertEqual(token, run_preflight.call_args.kwargs["github_token"])

    def test_ssh_preflight_blocks_before_config_without_token(self) -> None:
        stdout = io.StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("codex_dispatcher.ssh_runtime.load_protected_ssh_config") as load,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                ["ssh-preflight", "--config", "config.toml", "--json"]
            )

        self.assertEqual(1, exit_code)
        self.assertIn("token", json.loads(stdout.getvalue())["error"])
        load.assert_not_called()

    def test_ssh_preflight_blocked_plan_exits_nonzero(self) -> None:
        token = "github_pat_ssh_preflight_fixture"
        config = make_config(global_max_active=1)
        inspection = SshPreflightInspection(
            SshPreflightPlan(
                status=SshPreflightStatus.BLOCKED,
                recovery_action=SshRecoveryAction.BLOCK,
                reason="multiple_remote_claims",
            ),
            (ContractCheck("git", True, "2.55.0"),),
            True,
        )
        stdout = io.StringIO()
        with (
            patch.dict("os.environ", {"GH_TOKEN": token}, clear=True),
            patch(
                "codex_dispatcher.ssh_runtime.load_protected_ssh_config",
                return_value=config,
            ),
            patch("codex_dispatcher.ssh_runtime.validate_runtime_state_path"),
            patch(
                "codex_dispatcher.ssh_runtime.run_ssh_preflight",
                return_value=inspection,
            ),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                ["ssh-preflight", "--config", "config.toml", "--json"]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ok"])
        self.assertEqual("multiple_remote_claims", payload["reason"])

    def test_ssh_run_once_migrates_state_and_redacts_failures(self) -> None:
        token = "github_pat_ssh_runtime_fixture"
        with tempfile.TemporaryDirectory() as temp_dir:
            base = make_config(global_max_active=1)
            config = replace(
                base,
                scheduler=replace(
                    base.scheduler,
                    database_path=Path(temp_dir) / "state.db",
                ),
            )
            stdout = io.StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {
                        "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                        "GITHUB_TOKEN": token,
                    },
                    clear=True,
                ),
                patch(
                    "codex_dispatcher.ssh_runtime.load_protected_ssh_config",
                    return_value=config,
                ),
                patch(
                    "codex_dispatcher.ssh_runtime.run_ssh_control_sweep",
                    return_value=ControlSweepResult(ControlSweepStatus.IDLE),
                ) as run_sweep,
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "ssh-run-once",
                        "--apply",
                        "--config",
                        "config.toml",
                        "--json",
                    ]
                )
            payload = json.loads(stdout.getvalue())
            self.assertEqual(0, exit_code)
            self.assertEqual("idle", payload["status"])
            self.assertTrue((Path(temp_dir) / "state.db").is_file())
            self.assertEqual(token, run_sweep.call_args.kwargs["github_token"])
            self.assertNotIn(token, stdout.getvalue())

            stdout = io.StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {
                        "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                        "GITHUB_TOKEN": token,
                    },
                    clear=True,
                ),
                patch(
                    "codex_dispatcher.ssh_runtime.load_protected_ssh_config",
                    return_value=config,
                ),
                patch(
                    "codex_dispatcher.ssh_runtime.run_ssh_control_sweep",
                    side_effect=RuntimeError(f"fixture failure {token}"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "ssh-run-once",
                        "--apply",
                        "--config",
                        "config.toml",
                        "--json",
                    ]
                )
            self.assertEqual(1, exit_code)
            self.assertNotIn(token, stdout.getvalue())
            self.assertIn("[REDACTED]", stdout.getvalue())

    def test_ssh_run_once_requires_separate_slack_opt_in_and_bot_token(self) -> None:
        github_token = "github_pat_slack_runtime_fixture"
        slack_token = "xoxb-1234567890-fixture"
        base = make_config(global_max_active=1)
        config = replace(
            base,
            slack_runtime=SlackRuntimeConfig(
                channel_id="C0BR2D0MS8Y",
                request_timeout_seconds=10,
                idempotency_contract="client_msg_id-live-fixture-verified-v1",
            ),
        )
        for environment, expected in (
            (
                {
                    "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                    "GITHUB_TOKEN": github_token,
                    "SLACK_BOT_TOKEN": slack_token,
                },
                "ENABLE_SLACK_WRITES",
            ),
            (
                {
                    "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                    "CODEX_DISPATCHER_ENABLE_SLACK_WRITES": "1",
                    "GITHUB_TOKEN": github_token,
                    "SLACK_BOT_TOKEN": "xoxp-not-a-bot-token",
                },
                "Slack bot token",
            ),
        ):
            with self.subTest(expected=expected):
                stdout = io.StringIO()
                with (
                    patch.dict("os.environ", environment, clear=True),
                    patch(
                        "codex_dispatcher.ssh_runtime.load_protected_ssh_config",
                        return_value=config,
                    ),
                    patch(
                        "codex_dispatcher.ssh_runtime.validate_runtime_state_path"
                    ) as validate_state,
                    patch(
                        "codex_dispatcher.ssh_runtime.run_ssh_control_sweep"
                    ) as run_sweep,
                    contextlib.redirect_stdout(stdout),
                ):
                    exit_code = main(
                        [
                            "ssh-run-once",
                            "--apply",
                            "--config",
                            "config.toml",
                            "--json",
                        ]
                    )
                self.assertEqual(1, exit_code)
                self.assertIn(expected, json.loads(stdout.getvalue())["error"])
                self.assertNotIn(slack_token, stdout.getvalue())
                validate_state.assert_not_called()
                run_sweep.assert_not_called()

    def test_ssh_run_once_passes_slack_token_without_printing_it(self) -> None:
        github_token = "github_pat_slack_runtime_fixture"
        slack_token = "xoxb-1234567890-fixture"
        with tempfile.TemporaryDirectory() as temp_dir:
            base = make_config(global_max_active=1)
            config = replace(
                base,
                scheduler=replace(
                    base.scheduler,
                    database_path=Path(temp_dir) / "state.db",
                ),
                slack_runtime=SlackRuntimeConfig(
                    channel_id="C0BR2D0MS8Y",
                    request_timeout_seconds=10,
                    idempotency_contract="client_msg_id-live-fixture-verified-v1",
                ),
            )
            stdout = io.StringIO()
            with (
                patch.dict(
                    "os.environ",
                    {
                        "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                        "CODEX_DISPATCHER_ENABLE_SLACK_WRITES": "1",
                        "GITHUB_TOKEN": github_token,
                        "SLACK_BOT_TOKEN": slack_token,
                    },
                    clear=True,
                ),
                patch(
                    "codex_dispatcher.ssh_runtime.load_protected_ssh_config",
                    return_value=config,
                ),
                patch(
                    "codex_dispatcher.ssh_runtime.validate_runtime_state_path"
                ),
                patch(
                    "codex_dispatcher.ssh_runtime.run_ssh_control_sweep",
                    return_value=ControlSweepResult(ControlSweepStatus.IDLE),
                ) as run_sweep,
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "ssh-run-once",
                        "--apply",
                        "--config",
                        "config.toml",
                        "--json",
                    ]
                )

        self.assertEqual(0, exit_code)
        self.assertEqual(slack_token, run_sweep.call_args.kwargs["slack_token"])
        self.assertNotIn(slack_token, stdout.getvalue())

    def test_slack_fixture_requires_apply_and_separate_environment_gate(self) -> None:
        arguments = [
            "slack-idempotency-fixture",
            "--workspace-id",
            "T0BQ60N9WH4",
            "--channel-id",
            "C0BR2D0MS8Y",
            "--fixture-id",
            "067190d4-6948-4b75-8c90-d39c964e4f0b",
            "--json",
        ]
        with self.assertRaises(SystemExit):
            main(arguments)

        stdout = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {"SLACK_BOT_TOKEN": "xoxb-1234567890-fixture"},
                clear=True,
            ),
            patch(
                "codex_dispatcher.slack_live_fixture.run_slack_idempotency_fixture"
            ) as run_fixture,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(arguments[:-1] + ["--apply", "--json"])

        self.assertEqual(1, exit_code)
        self.assertIn("ENABLE_SLACK_FIXTURE_WRITES", stdout.getvalue())
        run_fixture.assert_not_called()

    def test_slack_fixture_returns_bounded_receipt_without_token(self) -> None:
        token = "xoxb-1234567890-fixture"
        fixture_id = "067190d4-6948-4b75-8c90-d39c964e4f0b"
        receipt = SimpleNamespace(
            message_ts="1700000000.000001",
            permalink=(
                "https://fixture.slack.com/archives/C0BR2D0MS8Y/"
                "p1700000000000001"
            ),
        )
        result = SimpleNamespace(
            workspace_id="T0BQ60N9WH4",
            channel_id="C0BR2D0MS8Y",
            fixture_id=fixture_id,
            client_msg_id="067190d4-6948-4b75-8c90-d39c964e4f0b",
            receipt=receipt,
        )
        stdout = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {
                    "CODEX_DISPATCHER_ENABLE_SLACK_FIXTURE_WRITES": "1",
                    "SLACK_BOT_TOKEN": token,
                },
                clear=True,
            ),
            patch(
                "codex_dispatcher.slack_live_fixture.run_slack_idempotency_fixture",
                return_value=result,
            ) as run_fixture,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "slack-idempotency-fixture",
                    "--workspace-id",
                    "T0BQ60N9WH4",
                    "--channel-id",
                    "C0BR2D0MS8Y",
                    "--fixture-id",
                    fixture_id,
                    "--apply",
                    "--json",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(payload["exact_retry_receipt_match"])
        self.assertTrue(payload["manual_confirmation_required"])
        self.assertFalse(payload["authorizes_runtime"])
        self.assertEqual(1, payload["expected_visible_messages"])
        self.assertNotIn(token, stdout.getvalue())
        self.assertEqual(token, run_fixture.call_args.kwargs["bot_token"])

    def test_slack_fixture_redacts_failure(self) -> None:
        token = "xoxb-1234567890-fixture"
        stdout = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {
                    "CODEX_DISPATCHER_ENABLE_SLACK_FIXTURE_WRITES": "1",
                    "SLACK_BOT_TOKEN": token,
                },
                clear=True,
            ),
            patch(
                "codex_dispatcher.slack_live_fixture.run_slack_idempotency_fixture",
                side_effect=RuntimeError(f"provider detail {token}"),
            ),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "slack-idempotency-fixture",
                    "--workspace-id",
                    "T0BQ60N9WH4",
                    "--channel-id",
                    "C0BR2D0MS8Y",
                    "--fixture-id",
                    "067190d4-6948-4b75-8c90-d39c964e4f0b",
                    "--apply",
                    "--json",
                ]
            )

        self.assertEqual(1, exit_code)
        self.assertNotIn(token, stdout.getvalue())
        self.assertIn("[REDACTED]", stdout.getvalue())

    def test_run_once_reports_missing_gh_without_external_write(self) -> None:
        stdout = io.StringIO()
        with patch("codex_dispatcher.cli.shutil.which", return_value=None):
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    ["run-once", "--dry-run", "--config", "missing.toml", "--json"]
                )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, exit_code)
        self.assertEqual("gh executable not found", payload["error"])

    def test_run_once_passes_environment_token_without_printing_it(self) -> None:
        stdout = io.StringIO()
        token = "github_pat_test_fixture"
        with (
            patch("codex_dispatcher.cli.shutil.which", return_value="/usr/bin/gh"),
            patch("codex_dispatcher.cli.load_config", return_value=make_config()),
            patch("codex_dispatcher.cli.run_once_dry_run") as dry_run,
            patch(
                "codex_dispatcher.trackers.github_cli.GitHubCliTracker"
            ) as tracker_class,
            patch.dict("os.environ", {"GITHUB_TOKEN": token}, clear=True),
            contextlib.redirect_stdout(stdout),
        ):
            dry_run.return_value = SimpleNamespace(selected=(), rejected=())
            exit_code = main(
                ["run-once", "--dry-run", "--config", "config.toml", "--json"]
            )

        self.assertEqual(0, exit_code)
        tracker_class.assert_called_once_with(gh_path=Path("/usr/bin/gh"), token=token)
        self.assertNotIn(token, stdout.getvalue())

    def test_run_once_ignores_unrecognized_environment_token(self) -> None:
        stdout = io.StringIO()
        with (
            patch("codex_dispatcher.cli.shutil.which", return_value="/usr/bin/gh"),
            patch("codex_dispatcher.cli.load_config", return_value=make_config()),
            patch("codex_dispatcher.cli.run_once_dry_run") as dry_run,
            patch(
                "codex_dispatcher.trackers.github_cli.GitHubCliTracker"
            ) as tracker_class,
            patch.dict("os.environ", {"GITHUB_TOKEN": "unrecognized-token"}, clear=True),
            contextlib.redirect_stdout(stdout),
        ):
            dry_run.return_value = SimpleNamespace(selected=(), rejected=())
            exit_code = main(
                ["run-once", "--dry-run", "--config", "config.toml", "--json"]
            )

        self.assertEqual(0, exit_code)
        tracker_class.assert_called_once_with(gh_path=Path("/usr/bin/gh"), token=None)


if __name__ == "__main__":
    unittest.main()
