"""Non-blocking singleton process lock for the Control Host dispatcher."""

from __future__ import annotations

import fcntl
import os
import stat
from pathlib import Path
from types import TracebackType


class DispatcherLockError(RuntimeError):
    """Base class for invalid or unavailable dispatcher locks."""


class DispatcherLockUnavailable(DispatcherLockError):
    """Raised when another dispatcher process already owns the global lock."""


class DispatcherProcessLock:
    """Hold one protected ``flock`` for the entire scheduling sweep."""

    def __init__(self, path: Path) -> None:
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or ".." in path.parts
            or path.name in {"", ".", ".."}
        ):
            raise ValueError("lock path must be a normalized absolute file path")
        self._path = path
        self._descriptor: int | None = None

    @property
    def acquired(self) -> bool:
        return self._descriptor is not None

    def acquire(self) -> None:
        if self._descriptor is not None:
            raise DispatcherLockError("dispatcher lock is already acquired by this object")
        try:
            parent_stat = self._path.parent.stat(follow_symlinks=False)
        except OSError as exc:
            raise DispatcherLockError("dispatcher lock directory is unavailable") from exc
        if not stat.S_ISDIR(parent_stat.st_mode) or self._path.parent.is_symlink():
            raise DispatcherLockError("dispatcher lock directory must be a real directory")
        if parent_stat.st_mode & 0o022:
            raise DispatcherLockError("dispatcher lock directory must not be group/world writable")

        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise DispatcherLockError("dispatcher lock file could not be opened safely") from exc
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise DispatcherLockError("dispatcher lock must be a regular file")
            if file_stat.st_uid != os.geteuid():
                raise DispatcherLockError("dispatcher lock must be owned by the dispatcher user")
            if file_stat.st_mode & 0o022:
                raise DispatcherLockError("dispatcher lock must not be group/world writable")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DispatcherLockUnavailable(
                    "another dispatcher process owns the global lock"
                ) from exc
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "DispatcherProcessLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
