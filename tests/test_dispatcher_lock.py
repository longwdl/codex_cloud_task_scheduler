from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.dispatcher_lock import (
    DispatcherLockError,
    DispatcherLockUnavailable,
    DispatcherProcessLock,
)


class DispatcherProcessLockTests(unittest.TestCase):
    def test_only_one_owner_can_enter_and_release_allows_next_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "dispatcher.lock"
            first = DispatcherProcessLock(lock_path)
            second = DispatcherProcessLock(lock_path)

            with first:
                self.assertTrue(first.acquired)
                with self.assertRaises(DispatcherLockUnavailable):
                    second.acquire()
                self.assertFalse(second.acquired)

            self.assertFalse(first.acquired)
            with second:
                self.assertTrue(second.acquired)

    def test_context_exception_still_releases_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "dispatcher.lock"
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                with DispatcherProcessLock(lock_path):
                    raise RuntimeError("fixture")

            with DispatcherProcessLock(lock_path) as recovered:
                self.assertTrue(recovered.acquired)

    def test_rejects_relative_missing_symlink_and_weakly_protected_paths(self) -> None:
        with self.assertRaises(ValueError):
            DispatcherProcessLock(Path("dispatcher.lock"))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(DispatcherLockError, "directory"):
                DispatcherProcessLock(root / "missing" / "lock").acquire()

            target = root / "target"
            target.write_text("", encoding="utf-8")
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaisesRegex(DispatcherLockError, "opened safely"):
                DispatcherProcessLock(link).acquire()

            weak = root / "weak.lock"
            weak.write_text("", encoding="utf-8")
            weak.chmod(0o622)
            with self.assertRaisesRegex(DispatcherLockError, "group/world writable"):
                DispatcherProcessLock(weak).acquire()

            weak_directory = root / "weak-directory"
            weak_directory.mkdir()
            weak_directory.chmod(0o777)
            try:
                with self.assertRaisesRegex(DispatcherLockError, "directory"):
                    DispatcherProcessLock(weak_directory / "lock").acquire()
            finally:
                weak_directory.chmod(0o700)

    def test_existing_lock_must_be_owned_by_dispatcher_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "dispatcher.lock"
            lock_path.touch(mode=0o600)
            original = os.fstat

            class ForeignOwner:
                st_mode = 0o100600
                st_uid = os.geteuid() + 1

            def fake_fstat(descriptor: int):
                original(descriptor)
                return ForeignOwner()

            with patch("codex_dispatcher.dispatcher_lock.os.fstat", side_effect=fake_fstat):
                with self.assertRaisesRegex(DispatcherLockError, "owned"):
                    DispatcherProcessLock(lock_path).acquire()


if __name__ == "__main__":
    unittest.main()
