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


if __name__ == "__main__":
    unittest.main()
