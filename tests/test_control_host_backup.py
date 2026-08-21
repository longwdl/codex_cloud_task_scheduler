from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from codex_dispatcher.control_host_backup import create_state_backup
from codex_dispatcher.domain import Run
from codex_dispatcher.state_store import StateStore


NOW = datetime(2026, 8, 20, 12, 32, 32, 123456, tzinfo=timezone.utc)


class ControlHostBackupTests(unittest.TestCase):
    def _state(self, root: Path) -> tuple[Path, Path]:
        database = root / "state.db"
        backups = root / "backups"
        backups.mkdir(mode=0o700)
        with StateStore(database) as store:
            store.migrate()
            store.create_run(
                Run.new(
                    run_id="backup-run",
                    repository="owner/repo",
                    issue_number=1,
                    prompt_sha256="a" * 64,
                    base_branch="main",
                    cloud_environment_id="env-1",
                )
            )
        return database, backups

    def test_creates_one_atomic_mode_0600_integrity_checked_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database, backups = self._state(Path(temp_dir))
            result = create_state_backup(database, backups, now=NOW)

            self.assertEqual(
                backups / "state-20260820T123232.123456Z.db", result.path
            )
            self.assertEqual("ok", result.integrity)
            self.assertGreater(result.size_bytes, 0)
            metadata = result.path.stat(follow_symlinks=False)
            self.assertEqual(0o600, stat.S_IMODE(metadata.st_mode))
            self.assertEqual(os.geteuid(), metadata.st_uid)
            self.assertEqual(1, metadata.st_nlink)
            self.assertEqual([], list(backups.glob(".state-backup-*")))
            with StateStore(result.path, read_only=True) as restored:
                self.assertEqual("ok", restored.integrity_check())
                self.assertIsNotNone(restored.get_run("backup-run"))

    def test_refuses_collision_weak_directory_and_naive_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database, backups = self._state(Path(temp_dir))
            create_state_backup(database, backups, now=NOW)
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                create_state_backup(database, backups, now=NOW)

            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                create_state_backup(
                    database,
                    backups,
                    now=datetime(2026, 8, 20, 12, 32, 32),
                )

            backups.chmod(0o750)
            with self.assertRaisesRegex(RuntimeError, "mode-0700"):
                create_state_backup(database, backups, now=NOW.replace(second=33))

    def test_refuses_missing_or_symlinked_source_and_cleans_failed_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database, backups = self._state(root)
            missing = root / "missing.db"
            with self.assertRaisesRegex(RuntimeError, "does not exist"):
                create_state_backup(missing, backups, now=NOW)

            link = root / "state-link.db"
            link.symlink_to(database)
            with self.assertRaisesRegex(RuntimeError, "protected regular file"):
                create_state_backup(link, backups, now=NOW)

            def fail_with_sidecars(staging: Path) -> None:
                staging.with_name(f"{staging.name}-wal").write_bytes(b"")
                staging.with_name(f"{staging.name}-shm").write_bytes(b"")
                raise RuntimeError("injected")

            with (
                patch.object(StateStore, "backup", side_effect=fail_with_sidecars),
                self.assertRaisesRegex(RuntimeError, "injected"),
            ):
                create_state_backup(database, backups, now=NOW)
            self.assertEqual([], list(backups.iterdir()))


if __name__ == "__main__":
    unittest.main()
