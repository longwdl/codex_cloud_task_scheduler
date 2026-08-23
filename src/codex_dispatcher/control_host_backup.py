"""Protected SQLite Online Backup for the Linux Control Host."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
import tempfile

from codex_dispatcher.ssh_runtime import validate_runtime_state_path
from codex_dispatcher.state_store import StateStore


@dataclass(frozen=True, slots=True)
class StateBackupResult:
    path: Path
    size_bytes: int
    integrity: str


@dataclass(frozen=True, slots=True)
class StateBackupRetentionResult:
    retained_paths: tuple[Path, ...]
    deleted_paths: tuple[Path, ...]
    deleted_bytes: int


@dataclass(frozen=True, slots=True)
class StateRestoreDrillResult:
    source_path: Path
    source_size_bytes: int
    restored_size_bytes: int
    integrity: str
    foreign_key_violations: int
    schema_migrations: tuple[int, ...]
    source_age_seconds: int


RETAIN_NEWEST_BACKUPS = 7
_BACKUP_NAME_RE = re.compile(r"state-([0-9]{8}T[0-9]{6}\.[0-9]{6}Z)\.db")


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
    staging_sidecars = (
        staging.with_name(f"{staging.name}-wal"),
        staging.with_name(f"{staging.name}-shm"),
    )
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
        for sidecar in staging_sidecars:
            if sidecar.exists() or sidecar.is_symlink():
                sidecar.unlink()


def rotate_state_backups(
    backup_directory: Path,
    *,
    retain_newest: int = RETAIN_NEWEST_BACKUPS,
    preserve_oldest: bool = True,
) -> StateBackupRetentionResult:
    """Delete only verified canonical backups beyond the protected retention set."""
    if isinstance(retain_newest, bool) or not isinstance(retain_newest, int):
        raise TypeError("retain_newest must be an integer")
    if retain_newest < 3:
        raise ValueError("retain_newest must preserve at least three backups")
    if type(preserve_oldest) is not bool:
        raise TypeError("preserve_oldest must be a boolean")
    _validate_backup_directory(backup_directory)
    backups = _canonical_backups(backup_directory)
    snapshots: dict[Path, os.stat_result] = {}
    for backup in backups:
        snapshots[backup] = _validate_good_backup(backup)

    protected = set(backups[-retain_newest:])
    if preserve_oldest and backups:
        protected.add(backups[0])
    targets = tuple(path for path in backups if path not in protected)
    if len(backups) - len(targets) < min(retain_newest, len(backups)):
        raise RuntimeError("backup retention would violate the minimum-good invariant")

    deleted: list[Path] = []
    deleted_bytes = 0
    for target in targets:
        expected = snapshots[target]
        current = target.stat(follow_symlinks=False)
        if (
            current.st_dev != expected.st_dev
            or current.st_ino != expected.st_ino
            or current.st_mode != expected.st_mode
            or current.st_uid != expected.st_uid
            or current.st_nlink != expected.st_nlink
            or current.st_size != expected.st_size
        ):
            raise RuntimeError("backup changed after retention validation")
        target.unlink()
        deleted.append(target)
        deleted_bytes += expected.st_size
    if deleted:
        _fsync_directory(backup_directory)
    retained = tuple(path for path in backups if path not in set(deleted))
    return StateBackupRetentionResult(retained, tuple(deleted), deleted_bytes)


def drill_latest_state_backup(
    database_path: Path,
    backup_directory: Path,
    *,
    now: datetime | None = None,
) -> StateRestoreDrillResult:
    """Restore the newest protected backup to a temporary database and verify it."""
    validate_runtime_state_path(database_path)
    _validate_backup_directory(backup_directory)
    backups = _canonical_backups(backup_directory)
    if not backups:
        raise RuntimeError("no canonical state backup is available for a restore drill")
    source_path = backups[-1]
    source_metadata = _validate_good_backup(source_path)
    source_timestamp = _backup_timestamp(source_path)
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("restore drill timestamp must be timezone-aware")
    moment = moment.astimezone(timezone.utc)
    source_age_seconds = max(0, int((moment - source_timestamp).total_seconds()))

    descriptor, staging_name = tempfile.mkstemp(
        prefix=".state-restore-drill-",
        suffix=".db",
        dir=database_path.parent,
    )
    os.close(descriptor)
    staging = Path(staging_name)
    staging_sidecars = (
        staging.with_name(f"{staging.name}-wal"),
        staging.with_name(f"{staging.name}-shm"),
    )
    try:
        with StateStore(source_path, read_only=True) as source:
            source_migrations = source.schema_migration_versions()
            if source_migrations != StateStore.supported_schema_migration_versions():
                raise RuntimeError(
                    "newest state backup migration ledger differs from this release"
                )
            source.backup(staging)
        os.chmod(staging, 0o600, follow_symlinks=False)
        _validate_backup_file(staging)
        _fsync_file(staging)
        with StateStore(staging, read_only=True) as restored:
            integrity = restored.integrity_check()
            foreign_key_violations = restored.foreign_key_violation_count()
            restored_migrations = restored.schema_migration_versions()
        if integrity != "ok":
            raise RuntimeError("restored state database integrity check failed")
        if foreign_key_violations:
            raise RuntimeError("restored state database has foreign-key violations")
        if restored_migrations != source_migrations:
            raise RuntimeError("restored state database migration ledger changed")
        return StateRestoreDrillResult(
            source_path=source_path,
            source_size_bytes=source_metadata.st_size,
            restored_size_bytes=staging.stat(follow_symlinks=False).st_size,
            integrity=integrity,
            foreign_key_violations=foreign_key_violations,
            schema_migrations=restored_migrations,
            source_age_seconds=source_age_seconds,
        )
    finally:
        if staging.exists() or staging.is_symlink():
            staging.unlink()
        for sidecar in staging_sidecars:
            if sidecar.exists() or sidecar.is_symlink():
                sidecar.unlink()


def _canonical_backups(backup_directory: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in backup_directory.iterdir()
            if _BACKUP_NAME_RE.fullmatch(path.name) is not None
        )
    )


def _backup_timestamp(path: Path) -> datetime:
    match = _BACKUP_NAME_RE.fullmatch(path.name)
    if match is None:
        raise ValueError("state backup filename is not canonical")
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S.%fZ").replace(
        tzinfo=timezone.utc
    )


def _validate_good_backup(path: Path) -> os.stat_result:
    _validate_backup_file(path)
    metadata = path.stat(follow_symlinks=False)
    with StateStore(path, read_only=True) as backup:
        if backup.integrity_check() != "ok":
            raise RuntimeError("retained state backup integrity check failed")
        if backup.foreign_key_violation_count():
            raise RuntimeError("retained state backup has foreign-key violations")
    return metadata


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
