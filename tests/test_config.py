from __future__ import annotations

import tempfile
import unittest
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

[[repositories]]
slug = "owner/repo"
base_branch = "main"
cloud_environment_id = "env-1"
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
runner_root = "/srv/codex-runner/work-items"
connect_timeout_seconds = 10
operation_timeout_seconds = 3900
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
        self.assertEqual("owner/repo", config.repositories[0].slug)
        self.assertEqual((), config.repositories[0].denied_paths)

    def test_rejects_unknown_secret_like_field(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown field"):
            self._load(
                VALID.replace(
                    'codex_version = "0.1.0"',
                    'codex_version = "0.1.0"\ntoken = "secret"',
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


if __name__ == "__main__":
    unittest.main()
