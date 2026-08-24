from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from codex_dispatcher.config import load_config


VALID = '''
[scheduler]
database_path = "state.db"
workspace_root = "repos"
poll_interval_seconds = 60
global_max_active = 2

[tools]
git_version = "2.45.0"
gh_version = "2.60.0"
codex_version = "0.1.0"

[repository_admission]
recovery_profiles = ["fixture-live-v1"]
target_readback_profiles = ["fixture-exact-v1"]

[[repositories]]
slug = "owner/repo"
repository_class = "fixture"
base_branch = "main"
max_active = 1
allowed_paths = ["src"]
denied_paths = []
maintainers = ["duke"]
required_checks = ["tests"]
'''

SSH_RUNTIME = '''

[ssh_runtime]
git_path = "/usr/bin/git"
gh_path = "/usr/bin/gh"
ssh_path = "/usr/bin/ssh"
host = "runner.internal"
user = "codex"
port = 22
known_hosts_path = "/etc/codex-dispatcher/runner_known_hosts"
identity_file = "/etc/codex-dispatcher/runner_ed25519"
lock_path = "/run/codex-dispatcher/dispatcher.lock"
mirror_root = "/var/lib/codex-dispatcher/mirrors"
source_temporary_root = "/var/lib/codex-dispatcher/source-temporary"
quarantine_root = "/var/lib/codex-dispatcher/quarantine"
publisher_temporary_root = "/var/lib/codex-dispatcher/publisher-temporary"
runner_root = "/srv/codex-runner/work-items"
connect_timeout_seconds = 10
operation_timeout_seconds = 3900
completed_retention_seconds = 604800
terminal_branch_retention_seconds = 2592000
terminal_branch_retention_cutover_at = "2026-08-23T19:29:16.047801Z"
'''

SLACK_RUNTIME = '''

[slack_runtime]
issue_channel_id = "C0BR2D0MS8Y"
system_channel_id = "C0BS3LPG43G"
request_timeout_seconds = 10
idempotency_contract = "client_msg_id-live-fixture-verified-v1"
'''

SESSION_RUNTIME = '''

[session_runtime]
protocol_version = 2
agent_policy_digest = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
max_turns_per_session = 4
rotate_after_input_tokens = 120000
rotate_after_session_age_seconds = 14400
rotate_before_final_audit = false
use_incremental_resume_prompts = true
max_session_generations = 3
max_total_turns = 10
max_no_progress_turns = 2
max_repair_cycles = 3
max_audit_cycles = 3
max_total_tokens = 1000000
max_work_item_age_seconds = 604800
'''


class ConfigTests(unittest.TestCase):
    def _load(self, content: str):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.toml"
            path.write_text(content, encoding="utf-8")
            return load_config(path)

    def test_loads_complete_strict_configuration(self) -> None:
        config = self._load(VALID)
        self.assertEqual(Path("state.db"), config.scheduler.database_path)
        self.assertEqual(Path("repos"), config.scheduler.workspace_root)
        self.assertEqual(2, config.scheduler.global_max_active)
        self.assertEqual(86400, config.scheduler.terminal_full_scan_interval_seconds)
        self.assertEqual("owner/repo", config.repositories[0].slug)
        self.assertEqual("fixture", config.repositories[0].repository_class.value)
        self.assertEqual((), config.repositories[0].denied_paths)
        assert config.repository_admission is not None
        self.assertEqual(
            {"fixture-live-v1"},
            {item.value for item in config.repository_admission.recovery_profiles},
        )
        self.assertEqual(
            {"fixture-exact-v1"},
            {
                item.value
                for item in config.repository_admission.target_readback_profiles
            },
        )
        self.assertIsNone(config.session_runtime)

        unclassified = self._load(
            VALID.replace(
                '[repository_admission]\nrecovery_profiles = ["fixture-live-v1"]\n'
                'target_readback_profiles = ["fixture-exact-v1"]\n\n',
                "",
            ).replace('repository_class = "fixture"\n', "")
        )
        self.assertIsNone(unclassified.repository_admission)
        self.assertEqual(
            "unclassified", unclassified.repositories[0].repository_class.value
        )

        configured = self._load(
            VALID.replace(
                "global_max_active = 2",
                "global_max_active = 2\nterminal_full_scan_interval_seconds = 900",
            )
        )
        self.assertEqual(900, configured.scheduler.terminal_full_scan_interval_seconds)

        with self.assertRaisesRegex(ValueError, "between 900 and 604800"):
            self._load(
                VALID.replace(
                    "global_max_active = 2",
                    "global_max_active = 2\nterminal_full_scan_interval_seconds = 60",
                )
            )

    def test_rejects_unknown_secret_like_field(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown field"):
            self._load(
                VALID.replace(
                    'codex_version = "0.1.0"',
                    'codex_version = "0.1.0"\ntoken = "secret"',
                )
            )
        with self.assertRaisesRegex(ValueError, "unsupported profile"):
            self._load(
                VALID.replace("fixture-live-v1", "self-attested-production-v1")
            )
        with self.assertRaisesRegex(ValueError, "repository_class must be one of"):
            self._load(
                VALID.replace(
                    'repository_class = "fixture"', 'repository_class = "prod"'
                )
            )
        with self.assertRaisesRegex(ValueError, "unknown field"):
            self._load(
                VALID.replace(
                    'base_branch = "main"',
                    'base_branch = "main"\ncloud_environment_id = "retired"',
                )
            )

    def test_rejects_missing_and_invalid_types(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing required"):
            self._load(VALID.replace('max_active = 1\n', ''))
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self._load(VALID.replace('max_active = 1', 'max_active = "one"'))

    def test_rejects_duplicate_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self._load(VALID.replace('maintainers = ["duke"]', 'maintainers = ["duke", "duke"]'))

    def test_rejects_glob_and_hard_denied_allow_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid repository path"):
            self._load(VALID.replace('allowed_paths = ["src"]', 'allowed_paths = ["src/**"]'))
        with self.assertRaisesRegex(ValueError, "hard-denied"):
            self._load(
                VALID.replace(
                    'allowed_paths = ["src"]',
                    'allowed_paths = [".github/workflows"]',
                )
            )
    def test_optional_ssh_runtime_is_strict_and_secret_free(self) -> None:
        configured = VALID.replace(
            'codex_version = "0.1.0"',
            'codex_version = "0.1.0"\nssh_version = "9.6"',
        )
        runtime = self._load(configured + SSH_RUNTIME).ssh_runtime
        self.assertIsNotNone(runtime)
        assert runtime is not None
        self.assertEqual(Path("/usr/bin/ssh"), runtime.ssh_path)
        self.assertEqual("/srv/codex-runner/work-items", runtime.runner_root)
        self.assertIsNone(runtime.assh_proxy_path)
        self.assertEqual(604800, runtime.completed_retention_seconds)
        self.assertEqual(2592000, runtime.terminal_branch_retention_seconds)
        self.assertEqual(
            datetime(2026, 8, 23, 19, 29, 16, 47801, tzinfo=timezone.utc),
            runtime.terminal_branch_retention_cutover_at,
        )

        without_retention = self._load(
            (configured + SSH_RUNTIME).replace(
                "completed_retention_seconds = 604800\n", ""
            )
        ).ssh_runtime
        assert without_retention is not None
        self.assertIsNone(without_retention.completed_retention_seconds)
        without_branch_retention = self._load(
            (configured + SSH_RUNTIME)
            .replace("terminal_branch_retention_seconds = 2592000\n", "")
            .replace(
                'terminal_branch_retention_cutover_at = "2026-08-23T19:29:16.047801Z"\n',
                "",
            )
        ).ssh_runtime
        assert without_branch_retention is not None
        self.assertIsNone(without_branch_retention.terminal_branch_retention_seconds)
        self.assertIsNone(without_branch_retention.terminal_branch_retention_cutover_at)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self._load(
                (configured + SSH_RUNTIME).replace(
                    "completed_retention_seconds = 604800",
                    "completed_retention_seconds = 0",
                )
            )
        with self.assertRaisesRegex(ValueError, "ten years"):
            self._load(
                (configured + SSH_RUNTIME).replace(
                    "completed_retention_seconds = 604800",
                    "completed_retention_seconds = 9999999999",
                )
            )
        with self.assertRaisesRegex(ValueError, "requires"):
            self._load(
                (configured + SSH_RUNTIME).replace(
                    "terminal_branch_retention_seconds = 2592000\n", ""
                )
            )
        with self.assertRaisesRegex(ValueError, "RFC 3339 UTC"):
            self._load(
                (configured + SSH_RUNTIME).replace(
                    "2026-08-23T19:29:16.047801Z",
                    "2026-08-23T20:29:16+01:00",
                )
            )

        with self.assertRaisesRegex(ValueError, "configured together"):
            self._load(
                configured
                + SSH_RUNTIME
                + 'assh_proxy_path = "/opt/homebrew/bin/assh"\n'
            )
        with self.assertRaisesRegex(ValueError, "unknown field"):
            self._load(configured + SSH_RUNTIME + 'github_token = "secret"\n')
        with self.assertRaisesRegex(ValueError, "normalized absolute"):
            self._load(
                (configured + SSH_RUNTIME).replace(
                    'identity_file = "/etc/codex-dispatcher/runner_ed25519"',
                    'identity_file = "../runner_ed25519"',
                )
            )
        with self.assertRaisesRegex(ValueError, "ssh_version"):
            self._load(VALID + SSH_RUNTIME)

    def test_optional_slack_runtime_requires_exact_proof_and_contains_no_secret(self) -> None:
        configured = VALID.replace(
            'codex_version = "0.1.0"',
            'codex_version = "0.1.0"\nssh_version = "9.6"',
        )
        loaded = self._load(configured + SSH_RUNTIME + SLACK_RUNTIME)
        runtime = loaded.slack_runtime
        self.assertIsNotNone(runtime)
        assert runtime is not None
        self.assertEqual("C0BR2D0MS8Y", runtime.issue_channel_id)
        self.assertEqual("C0BS3LPG43G", runtime.system_channel_id)
        self.assertEqual(10, runtime.request_timeout_seconds)

        with self.assertRaisesRegex(ValueError, "live-fixture proof"):
            self._load(
                (configured + SSH_RUNTIME + SLACK_RUNTIME).replace(
                    "client_msg_id-live-fixture-verified-v1",
                    "unverified",
                )
            )
        with self.assertRaisesRegex(ValueError, "must not exceed 60"):
            self._load(
                (configured + SSH_RUNTIME + SLACK_RUNTIME).replace(
                    "request_timeout_seconds = 10",
                    "request_timeout_seconds = 61",
                )
            )
        with self.assertRaisesRegex(ValueError, "issue_channel_id"):
            self._load(
                (configured + SSH_RUNTIME + SLACK_RUNTIME).replace(
                    'issue_channel_id = "C0BR2D0MS8Y"',
                    'issue_channel_id = "#project"',
                )
            )
        with self.assertRaisesRegex(ValueError, "must be different"):
            self._load(
                (configured + SSH_RUNTIME + SLACK_RUNTIME).replace(
                    'system_channel_id = "C0BS3LPG43G"',
                    'system_channel_id = "C0BR2D0MS8Y"',
                )
            )
        with self.assertRaisesRegex(ValueError, "unknown field"):
            self._load(
                (configured + SSH_RUNTIME + SLACK_RUNTIME).replace(
                    'issue_channel_id = "C0BR2D0MS8Y"',
                    'channel_id = "C0BR2D0MS8Y"',
                )
            )
        with self.assertRaisesRegex(ValueError, "unknown field"):
            self._load(
                configured
                + SSH_RUNTIME
                + SLACK_RUNTIME
                + 'bot_token = "xoxb-secret-must-not-be-configured"\n'
            )
        with self.assertRaisesRegex(ValueError, "requires ssh_runtime"):
            self._load(configured + SLACK_RUNTIME)

    def test_optional_session_runtime_is_explicit_strict_v2_policy(self) -> None:
        configured = VALID.replace(
            'codex_version = "0.1.0"',
            'codex_version = "0.1.0"\nssh_version = "9.6"',
        )
        loaded = self._load(configured + SSH_RUNTIME + SESSION_RUNTIME)
        runtime = loaded.session_runtime
        self.assertIsNotNone(runtime)
        assert runtime is not None
        self.assertEqual(2, runtime.protocol_version)
        self.assertEqual("a" * 64, runtime.agent_policy_digest)
        self.assertEqual(4, runtime.max_turns_per_session)
        self.assertEqual(120_000, runtime.rotate_after_input_tokens)
        self.assertEqual(14_400, runtime.rotate_after_session_age_seconds)
        self.assertFalse(runtime.rotate_before_final_audit)
        self.assertTrue(runtime.use_incremental_resume_prompts)
        self.assertEqual(3, runtime.max_session_generations)
        self.assertEqual(10, runtime.max_total_turns)
        self.assertEqual(2, runtime.max_no_progress_turns)

        with self.assertRaisesRegex(ValueError, "requires ssh_runtime"):
            self._load(configured + SESSION_RUNTIME)
        with self.assertRaisesRegex(ValueError, "unknown field"):
            self._load(
                configured + SSH_RUNTIME + SESSION_RUNTIME + "unexpected = 1\n"
            )
        with self.assertRaisesRegex(ValueError, "missing required"):
            self._load(
                (configured + SSH_RUNTIME + SESSION_RUNTIME).replace(
                    "max_no_progress_turns = 2\n",
                    "",
                )
            )

    def test_session_runtime_rejects_protocol_digest_and_type_boundaries(self) -> None:
        configured = VALID.replace(
            'codex_version = "0.1.0"',
            'codex_version = "0.1.0"\nssh_version = "9.6"',
        )
        content = configured + SSH_RUNTIME + SESSION_RUNTIME
        enabled = self._load(
            content.replace(
                "rotate_before_final_audit = false",
                "rotate_before_final_audit = true",
            )
        )
        self.assertTrue(enabled.session_runtime.rotate_before_final_audit)
        for invalid_protocol in ("1", "3", "true"):
            with self.subTest(protocol=invalid_protocol):
                with self.assertRaisesRegex(ValueError, "must equal 2"):
                    self._load(
                        content.replace(
                            "protocol_version = 2",
                            f"protocol_version = {invalid_protocol}",
                        )
                    )
        for invalid_digest in ("A" * 64, "a" * 63, "g" * 64):
            with self.subTest(digest=invalid_digest[:8]):
                with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
                    self._load(
                        content.replace(
                            'agent_policy_digest = "' + "a" * 64 + '"',
                            f'agent_policy_digest = "{invalid_digest}"',
                        )
                    )

        positive_fields = (
            "max_turns_per_session",
            "rotate_after_input_tokens",
            "rotate_after_session_age_seconds",
            "max_session_generations",
            "max_total_turns",
            "max_no_progress_turns",
        )
        original_values = ("4", "120000", "14400", "3", "10", "2")
        for field, original in zip(positive_fields, original_values, strict=True):
            for invalid in ("0", "-1", "true"):
                with self.subTest(field=field, invalid=invalid):
                    with self.assertRaisesRegex(ValueError, "positive integer"):
                        self._load(
                            content.replace(
                                f"{field} = {original}",
                                f"{field} = {invalid}",
                            )
                        )
        for field, original in (
            ("rotate_before_final_audit", "false"),
            ("use_incremental_resume_prompts", "true"),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "boolean"):
                    self._load(
                        content.replace(f"{field} = {original}", f"{field} = 1")
                    )


if __name__ == "__main__":
    unittest.main()
