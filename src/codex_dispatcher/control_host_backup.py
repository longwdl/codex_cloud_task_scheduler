"""Protected SQLite Online Backup for the Linux Control Host."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import tempfile

from codex_dispatcher.ssh_runtime import validate_runtime_state_path
from codex_dispatcher.state_store import StateStore


@dataclass(frozen=True, slots=True)
class StateBackupResult:
    path: Path
    size_bytes: int
    integrity: str


def create_state_backup(
    database_path: Path,
    backup_directory: Path,
    *,
    now: datetime | None = None,
) -> StateBackupResult:
    """Create and atomically publish one integrity-checked mode-0600 backup."""
    validate_runtime_state_path(database_path)
    if not database_path.is_file() or database_path.is_symlink():
        raise RuntimeError("state database does not exist as a protected regular file")
    _validate_backup_directory(backup_directory)

    timestamp = _timestamp(now)
    destination = backup_directory / f"state-{timestamp}.db"
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("state backup destination already exists")

    descriptor, staging_name = tempfile.mkstemp(
        prefix=".state-backup-",
        suffix=".tmp",
        dir=backup_directory,
    )
    os.close(descriptor)
    staging = Path(staging_name)
    try:
        with StateStore(database_path, read_only=True) as source:
            if source.integrity_check() != "ok":
                raise RuntimeError("state database integrity check failed")
            source.backup(staging)

        os.chmod(staging, 0o600, follow_symlinks=False)
        with StateStore(staging, read_only=True) as backup:
            integrity = backup.integrity_check()
        if integrity != "ok":
            raise RuntimeError("state backup integrity check failed")
        _fsync_file(staging)

        # link(2) publishes without replacing an existing same-timestamp backup.
        os.link(staging, destination, follow_symlinks=False)
        staging.unlink()
        _fsync_directory(backup_directory)
        _validate_backup_file(destination)
        return StateBackupResult(
            path=destination,
            size_bytes=destination.stat(follow_symlinks=False).st_size,
            integrity=integrity,
        )
    finally:
        if staging.exists() or staging.is_symlink():
            staging.unlink()


def _timestamp(value: datetime | None) -> str:
    moment = datetime.now(timezone.utc) if value is None else value
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("backup timestamp must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _validate_backup_directory(path: Path) -> None:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise ValueError("backup directory must be a normalized absolute path")
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError("backup directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RuntimeError("backup directory must be an owned mode-0700 directory")


def _validate_backup_file(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
    ):
        raise RuntimeError("published state backup is not a protected regular file")


def _fsync_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
