from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.config import load_config
from codex_dispatcher.higher_value_canary import (
    HigherValueCanaryRejected,
    validate_higher_value_canary_config,
)
from codex_dispatcher.higher_value_canary_cli import _run
from codex_dispatcher.repository_admission import (
    HIGHER_VALUE_CANARY_REPOSITORY,
    HigherValueCanaryTarget,
)


CONFIG = """
[scheduler]
database_path = "/var/lib/codex-dispatcher/higher-value-canary/state.db"
workspace_root = "/var/lib/codex-dispatcher/higher-value-canary/repos"
poll_interval_seconds = 120
global_max_active = 1

[tools]
git_version = "2.55.0"
gh_version = "2.97.0"
codex_version = "0.147.0"
ssh_version = "9.6"

[ssh_runtime]
git_path = "/usr/bin/git"
gh_path = "/usr/bin/gh"
ssh_path = "/usr/bin/ssh"
host = "runner.internal"
user = "codex-runner"
port = 22
known_hosts_path = "/etc/codex-dispatcher/runner_known_hosts"
identity_file = "/etc/codex-dispatcher/runner_ed25519"
lock_path = "/run/codex-dispatcher/higher-value-canary.lock"
mirror_root = "/var/lib/codex-dispatcher/higher-value-canary/mirrors"
source_temporary_root = "/var/lib/codex-dispatcher/higher-value-canary/source-temporary"
quarantine_root = "/var/lib/codex-dispatcher/higher-value-canary/quarantine"
publisher_temporary_root = "/var/lib/codex-dispatcher/higher-value-canary/publisher-temporary"
runner_root = "/srv/codex-runner/work-items"
connect_timeout_seconds = 10
operation_timeout_seconds = 3900

[session_runtime]
protocol_version = 2
agent_policy_digest = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
max_turns_per_session = 4
rotate_after_input_tokens = 120000
rotate_after_session_age_seconds = 14400
rotate_before_final_audit = true
use_incremental_resume_prompts = true
max_session_generations = 3
max_total_turns = 10
max_no_progress_turns = 2
max_repair_cycles = 3
max_audit_cycles = 3
max_total_tokens = 1000000
max_work_item_age_seconds = 604800

[repository_admission]
recovery_profiles = ["higher-value-live-v1"]
target_readback_profiles = ["higher-value-exact-v1"]

[[repositories]]
slug = "longwdl/codex-dispatcher-fixture-2"
repository_class = "higher-value"
base_branch = "main"
cloud_environment_id = "higher-value-canary"
max_active = 1
allowed_paths = ["canary/target.txt"]
denied_paths = []
maintainers = ["longwdl"]
required_checks = ["higher-value-canary"]
"""


class HigherValueCanaryTests(unittest.TestCase):
    def _config(self):
        with tempfile.TemporaryDirectory() as root:
            config_path = Path(root) / "config.toml"
            config_path.write_text(CONFIG, encoding="utf-8")
            return load_config(config_path)

    def _target(self) -> HigherValueCanaryTarget:
        return HigherValueCanaryTarget(
            HIGHER_VALUE_CANARY_REPOSITORY,
            7,
            "I_kwDOHigherValue7",
            "a" * 40,
        )

    def test_exact_isolated_config_is_required(self) -> None:
        config = self._config()
        validate_higher_value_canary_config(config, self._target())

        unsafe = replace(
            config,
            scheduler=replace(config.scheduler, database_path=Path("/var/lib/state.db")),
        )
        with self.assertRaisesRegex(HigherValueCanaryRejected, "not isolated"):
            validate_higher_value_canary_config(unsafe, self._target())

        broad = replace(
            config,
            repositories=(
                replace(config.repositories[0], allowed_paths=("canary",)),
            ),
        )
        with self.assertRaisesRegex(HigherValueCanaryRejected, "policy is not exact"):
            validate_higher_value_canary_config(broad, self._target())

    def test_cli_requires_both_explicit_environment_guards(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            code, payload = _run(
                Path("/not/read"),
                issue_number=7,
                expected_issue_node_id="I_kwDOHigherValue7",
                expected_base_sha="a" * 40,
            )
        self.assertEqual(1, code)
        self.assertIn("CODEX_DISPATCHER_ENABLE_SSH_WRITES", payload["error"])

        with patch.dict(
            os.environ,
            {"CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1"},
            clear=True,
        ):
            code, payload = _run(
                Path("/not/read"),
                issue_number=7,
                expected_issue_node_id="I_kwDOHigherValue7",
                expected_base_sha="a" * 40,
            )
        self.assertEqual(1, code)
        self.assertIn("CODEX_DISPATCHER_ENABLE_HIGHER_VALUE_CANARY", payload["error"])


if __name__ == "__main__":
    unittest.main()
