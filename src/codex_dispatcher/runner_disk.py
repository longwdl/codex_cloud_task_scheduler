"""Fail-closed per-WorkItem disk images for the rootless Docker Runner."""

from __future__ import annotations

import os
import shlex
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from codex_dispatcher.command_runner import CommandResult, run_command
from codex_dispatcher.work_items import validate_work_item_id


MINIMUM_IMAGE_BYTES = 64 * 1024 * 1024
MAXIMUM_IMAGE_BYTES = 64 * 1024 * 1024 * 1024
_MOUNT_TIMEOUT_SECONDS = 3.0
_COMMAND_TIMEOUT_SECONDS = 30.0


class RunnerDiskError(RuntimeError):
    """Raised when a bounded WorkItem filesystem cannot be proven safe."""


@dataclass(frozen=True, slots=True)
class WorkItemDiskRuntime:
    image_directory: Path
    image_size_bytes: int
    host_reserve_bytes: int
    mkfs_ext4_path: Path
    fuse2fs_path: Path
    fusermount_path: Path
    e2fsck_path: Path
    findmnt_path: Path

    def __post_init__(self) -> None:
        for field in (
            "image_directory",
            "mkfs_ext4_path",
            "fuse2fs_path",
            "fusermount_path",
            "e2fsck_path",
            "findmnt_path",
        ):
            value = getattr(self, field)
            if (
                not isinstance(value, Path)
                or not value.is_absolute()
                or ".." in value.parts
                or any(character.isspace() for character in str(value))
                or "\x00" in str(value)
            ):
                raise ValueError(f"{field} must be a normalized absolute path")
        if (
            type(self.image_size_bytes) is not int
            or not MINIMUM_IMAGE_BYTES
            <= self.image_size_bytes
            <= MAXIMUM_IMAGE_BYTES
            or self.image_size_bytes % 4096
        ):
            raise ValueError("image_size_bytes is outside the supported boundary")
        if (
            type(self.host_reserve_bytes) is not int
            or self.host_reserve_bytes < MINIMUM_IMAGE_BYTES
            or self.host_reserve_bytes % 4096
        ):
            raise ValueError("host_reserve_bytes is outside the supported boundary")


@dataclass(frozen=True, slots=True)
class _MountRecord:
    source: Path
    filesystem_type: str
    options: frozenset[str]
    target: Path


class FusedWorkItemDisk:
    """Provision and remount one preallocated ext4 image per WorkItem."""

    def __init__(
        self,
        runtime: WorkItemDiskRuntime,
        *,
        work_items_root: Path,
    ) -> None:
        if not isinstance(runtime, WorkItemDiskRuntime):
            raise TypeError("runtime must be a WorkItemDiskRuntime")
        if (
            not isinstance(work_items_root, Path)
            or not work_items_root.is_absolute()
            or ".." in work_items_root.parts
        ):
            raise ValueError("work_items_root must be a normalized absolute path")
        try:
            runtime.image_directory.relative_to(work_items_root)
        except ValueError:
            pass
        else:
            raise ValueError("disk images must remain outside work_items_root")
        try:
            work_items_root.relative_to(runtime.image_directory)
        except ValueError:
            pass
        else:
            raise ValueError("work_items_root must remain outside disk images")
        self._runtime = runtime
        self._work_items_root = work_items_root

    def final_image_exists(self, work_item_id: str) -> bool:
        work_item_id = validate_work_item_id(work_item_id)
        self._prepare_image_root()
        self._reject_staging_ambiguity(work_item_id)
        path = self._image_path(work_item_id)
        if not path.exists():
            if path.is_symlink():
                raise RunnerDiskError("WorkItem disk image is a dangling symbolic link")
            return False
        self._validate_image(path)
        return True

    def ensure_mounted(self, work_item_id: str, mountpoint: Path) -> None:
        work_item_id = validate_work_item_id(work_item_id)
        self._prepare_image_root()
        self._validate_mountpoint_path(mountpoint)
        self._reject_staging_ambiguity(work_item_id)
        image = self._image_path(work_item_id)
        self._validate_image(image)
        record = self._mount_record(mountpoint)
        if record is not None:
            self._validate_mount_record(record, image=image, mountpoint=mountpoint)
            self._validate_mount_root(mountpoint)
            self._validate_mounted_capacity(mountpoint)
            return
        self._ensure_empty_mountpoint(mountpoint)
        self._check_filesystem(image, repair=True)
        self._mount(image, mountpoint)
        record = self._mount_record(mountpoint)
        if record is None:
            raise RunnerDiskError("WorkItem disk mount did not appear")
        self._validate_mount_record(record, image=image, mountpoint=mountpoint)
        self._validate_mount_root(mountpoint)
        self._validate_mounted_capacity(mountpoint)

    @contextmanager
    def provision(self, work_item_id: str, mountpoint: Path) -> Iterator[Path]:
        """Yield a new staging filesystem and commit it only after clean unmount."""
        work_item_id = validate_work_item_id(work_item_id)
        self._prepare_image_root()
        self._validate_mountpoint_path(mountpoint)
        self._reject_staging_ambiguity(work_item_id)
        final_image = self._image_path(work_item_id)
        if final_image.exists() or final_image.is_symlink():
            raise RunnerDiskError("WorkItem disk image already exists")
        self._ensure_empty_mountpoint(mountpoint)
        staging = self._staging_directory()
        staging_image = staging / f"{work_item_id}.ext4"
        staging_mount = staging / f"{work_item_id}.mount"
        if (
            staging_image.exists()
            or staging_image.is_symlink()
            or staging_mount.exists()
            or staging_mount.is_symlink()
        ):
            raise RunnerDiskError("WorkItem disk staging state is ambiguous")
        self._require_host_capacity()
        staging_mount.mkdir(mode=0o700)
        self._create_image(staging_image)
        self._format_image(staging_image)
        self._mount(staging_image, staging_mount)
        try:
            os.chown(staging_mount, os.geteuid(), os.getegid())
            os.chmod(staging_mount, 0o700)
            self._validate_mount_root(staging_mount)
            self._validate_mounted_capacity(staging_mount)
            yield staging_mount
        except BaseException:
            self._best_effort_unmount(staging_mount)
            raise
        self._unmount(staging_mount)
        self._check_filesystem(staging_image, repair=False)
        try:
            os.replace(staging_image, final_image)
            self._fsync_directory(self._runtime.image_directory)
            staging_mount.rmdir()
        except OSError as exc:
            raise RunnerDiskError("WorkItem disk image could not be committed") from exc
        self.ensure_mounted(work_item_id, mountpoint)

    def _prepare_image_root(self) -> None:
        self._protected_directory(self._runtime.image_directory, "disk image directory")
        staging = self._staging_directory()
        try:
            staging.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise RunnerDiskError("disk staging directory is unavailable") from exc
        self._protected_directory(staging, "disk staging directory")

    def _reject_staging_ambiguity(self, work_item_id: str) -> None:
        staging = self._staging_directory()
        for path in (
            staging / f"{work_item_id}.ext4",
            staging / f"{work_item_id}.mount",
        ):
            if path.exists() or path.is_symlink():
                raise RunnerDiskError("WorkItem disk staging state is ambiguous")

    def _create_image(self, path: Path) -> None:
        self._require_host_capacity()
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            os.fchmod(descriptor, 0o600)
            allocator = getattr(os, "posix_fallocate", None)
            if allocator is None:
                raise RunnerDiskError("hard disk preallocation is unavailable")
            allocator(descriptor, 0, self._runtime.image_size_bytes)
            os.fsync(descriptor)
        except RunnerDiskError:
            raise
        except OSError as exc:
            raise RunnerDiskError("WorkItem disk image could not be allocated") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self._validate_image(path)
        if self._available_bytes(self._runtime.image_directory) < self._runtime.host_reserve_bytes:
            raise RunnerDiskError("host reserve was crossed while allocating WorkItem disk")

    def _require_host_capacity(self) -> None:
        available = self._available_bytes(self._runtime.image_directory)
        required = self._runtime.image_size_bytes + self._runtime.host_reserve_bytes
        if available < required:
            raise RunnerDiskError("host free space is below the disk admission boundary")

    def _format_image(self, image: Path) -> None:
        self._run_fixed(
            (
                str(self._runtime.mkfs_ext4_path),
                "-q",
                "-F",
                "-m",
                "0",
                "-L",
                "codex-work-item",
                str(image),
            ),
            stage="format",
        )

    def _mount(self, image: Path, mountpoint: Path) -> None:
        if self._mount_record(mountpoint) is not None:
            raise RunnerDiskError("WorkItem mountpoint is already occupied")
        self._run_fixed(
            (
                str(self._runtime.fuse2fs_path),
                "-o",
                "fakeroot,rw,nosuid,nodev",
                str(image),
                str(mountpoint),
            ),
            stage="mount",
        )
        deadline = time.monotonic() + _MOUNT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._mount_record(mountpoint) is not None:
                return
            time.sleep(0.05)
        raise RunnerDiskError("WorkItem disk mount did not become visible")

    def _unmount(self, mountpoint: Path) -> None:
        self._run_fixed(
            (str(self._runtime.fusermount_path), "-u", str(mountpoint)),
            stage="unmount",
        )
        if self._mount_record(mountpoint) is not None:
            raise RunnerDiskError("WorkItem disk remained mounted after unmount")

    def _best_effort_unmount(self, mountpoint: Path) -> None:
        try:
            if self._mount_record(mountpoint) is not None:
                self._unmount(mountpoint)
        except RunnerDiskError:
            pass

    def _check_filesystem(self, image: Path, *, repair: bool) -> None:
        arguments = (
            ("-p", "-f") if repair else ("-f", "-n")
        )
        accepted = {0, 1} if repair else {0}
        self._run_fixed(
            (str(self._runtime.e2fsck_path), *arguments, str(image)),
            stage="filesystem_check",
            accepted_returncodes=accepted,
        )

    def _mount_record(self, mountpoint: Path) -> _MountRecord | None:
        result = run_command(
            (
                str(self._runtime.findmnt_path),
                "--mountpoint",
                str(mountpoint),
                "--noheadings",
                "--pairs",
                "--output",
                "SOURCE,FSTYPE,OPTIONS,TARGET",
            ),
            timeout_seconds=5.0,
            max_output_bytes=4096,
        )
        if (
            result.timed_out
            or result.error is not None
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise RunnerDiskError("mount identity inspection failed")
        if result.returncode == 1 and not result.stdout.strip():
            return None
        if result.returncode != 0:
            raise RunnerDiskError("mount identity inspection failed")
        try:
            tokens = shlex.split(result.stdout.strip(), posix=True)
            pairs = dict(token.split("=", 1) for token in tokens)
            if set(pairs) != {"SOURCE", "FSTYPE", "OPTIONS", "TARGET"}:
                raise ValueError("unexpected findmnt fields")
            return _MountRecord(
                Path(pairs["SOURCE"]),
                pairs["FSTYPE"],
                frozenset(pairs["OPTIONS"].split(",")),
                Path(pairs["TARGET"]),
            )
        except (TypeError, ValueError) as exc:
            raise RunnerDiskError("mount identity output is malformed") from exc

    def _validate_mount_record(
        self,
        record: _MountRecord,
        *,
        image: Path,
        mountpoint: Path,
    ) -> None:
        required_options = {
            "rw",
            "nosuid",
            "nodev",
            f"user_id={os.geteuid()}",
            f"group_id={os.getegid()}",
        }
        if (
            record.source != image
            or record.target != mountpoint
            or record.filesystem_type != "fuse.ext4"
            or not required_options.issubset(record.options)
            or "ro" in record.options
        ):
            raise RunnerDiskError("WorkItem mount identity is invalid")

    def _validate_image(self, path: Path) -> None:
        try:
            image_stat = path.lstat()
        except OSError as exc:
            raise RunnerDiskError("WorkItem disk image is unavailable") from exc
        if (
            not stat.S_ISREG(image_stat.st_mode)
            or path.is_symlink()
            or image_stat.st_uid != os.geteuid()
            or image_stat.st_gid != os.getegid()
            or image_stat.st_mode & 0o077
            or image_stat.st_nlink != 1
            or image_stat.st_size != self._runtime.image_size_bytes
            or image_stat.st_blocks * 512 < image_stat.st_size
        ):
            raise RunnerDiskError("WorkItem disk image identity is invalid")

    def _validate_mount_root(self, path: Path) -> None:
        try:
            path_stat = path.lstat()
        except OSError as exc:
            raise RunnerDiskError("WorkItem mount root is unavailable") from exc
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or path.is_symlink()
            or path_stat.st_uid != os.geteuid()
            or path_stat.st_gid != os.getegid()
            or path_stat.st_mode & 0o077
        ):
            raise RunnerDiskError("WorkItem mount root is not owned and protected")

    def _validate_mounted_capacity(self, path: Path) -> None:
        try:
            filesystem = os.statvfs(path)
        except OSError as exc:
            raise RunnerDiskError("WorkItem disk capacity is unavailable") from exc
        capacity = filesystem.f_blocks * filesystem.f_frsize
        if not 0 < capacity <= self._runtime.image_size_bytes:
            raise RunnerDiskError("WorkItem disk capacity exceeds its configured boundary")

    def _ensure_empty_mountpoint(self, path: Path) -> None:
        self._validate_mountpoint_path(path)
        try:
            self._ensure_protected_parent_chain(path.parent)
            path.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise RunnerDiskError("WorkItem mountpoint could not be created") from exc
        self._protected_directory(path, "WorkItem mountpoint")
        try:
            if any(path.iterdir()):
                raise RunnerDiskError("unmounted WorkItem mountpoint is not empty")
        except OSError as exc:
            raise RunnerDiskError("WorkItem mountpoint is unavailable") from exc

    def _ensure_protected_parent_chain(self, parent: Path) -> None:
        self._protected_directory(self._work_items_root, "work_items_root")
        try:
            relative = parent.relative_to(self._work_items_root)
        except ValueError as exc:
            raise RunnerDiskError("WorkItem mountpoint escapes work_items_root") from exc
        current = self._work_items_root
        for component in relative.parts:
            current = current / component
            current.mkdir(mode=0o700, exist_ok=True)
            self._protected_directory(current, "WorkItem mountpoint parent")

    def _validate_mountpoint_path(self, path: Path) -> None:
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or ".." in path.parts
        ):
            raise RunnerDiskError("WorkItem mountpoint path is invalid")
        try:
            path.relative_to(self._work_items_root)
        except ValueError as exc:
            raise RunnerDiskError("WorkItem mountpoint escapes work_items_root") from exc

    def _protected_directory(self, path: Path, field: str) -> None:
        try:
            path_stat = path.lstat()
        except OSError as exc:
            raise RunnerDiskError(f"{field} is unavailable") from exc
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or path.is_symlink()
            or path_stat.st_uid != os.geteuid()
            or path_stat.st_gid != os.getegid()
            or path_stat.st_mode & 0o077
        ):
            raise RunnerDiskError(f"{field} must be an owned protected directory")

    def _run_fixed(
        self,
        argv: tuple[str, ...],
        *,
        stage: str,
        accepted_returncodes: set[int] | None = None,
    ) -> CommandResult:
        result = run_command(
            argv,
            timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
            max_output_bytes=4096,
        )
        accepted = accepted_returncodes or {0}
        if (
            result.returncode not in accepted
            or result.timed_out
            or result.error is not None
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise RunnerDiskError(f"WorkItem disk stage failed: {stage}")
        return result

    def _image_path(self, work_item_id: str) -> Path:
        return self._runtime.image_directory / f"{work_item_id}.ext4"

    def _staging_directory(self) -> Path:
        return self._runtime.image_directory / ".staging"

    @staticmethod
    def _available_bytes(path: Path) -> int:
        try:
            filesystem = os.statvfs(path)
        except OSError as exc:
            raise RunnerDiskError("host free space is unavailable") from exc
        return filesystem.f_bavail * filesystem.f_frsize

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise RunnerDiskError("disk image directory could not be synchronized") from exc
