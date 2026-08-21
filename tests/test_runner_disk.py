from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.command_runner import CommandResult
from codex_dispatcher.runner_disk import (
    FusedWorkItemDisk,
    RunnerDiskError,
    WorkItemDiskRuntime,
    _MountRecord,
)


WORK_ITEM = "wi_" + "d" * 24
IMAGE_BYTES = 64 * 1024 * 1024


def protected_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)


def allocate_dense(path: Path) -> None:
    block = b"\0" * (1024 * 1024)
    with path.open("x+b") as stream:
        os.fchmod(stream.fileno(), 0o600)
        for _ in range(IMAGE_BYTES // len(block)):
            stream.write(block)
        stream.flush()
        os.fsync(stream.fileno())


def runtime(root: Path) -> tuple[WorkItemDiskRuntime, Path]:
    images = root / "images"
    images.mkdir(mode=0o700)
    work_items = root / "work-items"
    work_items.mkdir(mode=0o700)
    tool = root / "tool"
    protected_executable(tool)
    return (
        WorkItemDiskRuntime(
            image_directory=images,
            image_size_bytes=IMAGE_BYTES,
            host_reserve_bytes=IMAGE_BYTES,
            mkfs_ext4_path=tool,
            fuse2fs_path=tool,
            fusermount_path=tool,
            e2fsck_path=tool,
            findmnt_path=tool,
        ),
        work_items,
    )


class RunnerDiskTests(unittest.TestCase):
    def test_format_preserves_dense_preallocation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured, work_items = runtime(root)
            disk = FusedWorkItemDisk(configured, work_items_root=work_items)
            image = configured.image_directory / f"{WORK_ITEM}.ext4"

            with patch.object(disk, "_run_fixed") as run_fixed:
                disk._format_image(image)

            run_fixed.assert_called_once_with(
                (
                    str(configured.mkfs_ext4_path),
                    "-q",
                    "-F",
                    "-m",
                    "0",
                    "-E",
                    "nodiscard",
                    "-L",
                    "codex-work-item",
                    str(image),
                ),
                stage="format",
            )

    def test_runtime_rejects_unbounded_or_overlapping_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured, work_items = runtime(root)
            with self.assertRaisesRegex(ValueError, "boundary"):
                WorkItemDiskRuntime(
                    image_directory=configured.image_directory,
                    image_size_bytes=1024,
                    host_reserve_bytes=IMAGE_BYTES,
                    mkfs_ext4_path=configured.mkfs_ext4_path,
                    fuse2fs_path=configured.fuse2fs_path,
                    fusermount_path=configured.fusermount_path,
                    e2fsck_path=configured.e2fsck_path,
                    findmnt_path=configured.findmnt_path,
                )
            with self.assertRaisesRegex(ValueError, "outside"):
                FusedWorkItemDisk(
                    configured,
                    work_items_root=configured.image_directory / "nested",
                )
            self.assertIsInstance(
                FusedWorkItemDisk(configured, work_items_root=work_items),
                FusedWorkItemDisk,
            )

    def test_mount_identity_requires_exact_source_type_options_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured, work_items = runtime(root)
            disk = FusedWorkItemDisk(configured, work_items_root=work_items)
            image = configured.image_directory / f"{WORK_ITEM}.ext4"
            mountpoint = work_items / "owner__repo" / "issue-1"
            valid = _MountRecord(
                image,
                "fuse.ext4",
                frozenset(
                    {
                        "rw",
                        "nosuid",
                        "nodev",
                        f"user_id={os.geteuid()}",
                        f"group_id={os.getegid()}",
                    }
                ),
                mountpoint,
            )
            disk._validate_mount_record(valid, image=image, mountpoint=mountpoint)
            for changed in (
                _MountRecord(root / "other", valid.filesystem_type, valid.options, mountpoint),
                _MountRecord(image, "ext4", valid.options, mountpoint),
                _MountRecord(image, valid.filesystem_type, frozenset({"rw"}), mountpoint),
                _MountRecord(image, valid.filesystem_type, valid.options, root / "other"),
            ):
                with self.assertRaisesRegex(RunnerDiskError, "identity"):
                    disk._validate_mount_record(
                        changed,
                        image=image,
                        mountpoint=mountpoint,
                    )

    def test_findmnt_output_is_bounded_and_strictly_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured, work_items = runtime(root)
            disk = FusedWorkItemDisk(configured, work_items_root=work_items)
            mountpoint = work_items / "owner__repo" / "issue-1"
            image = configured.image_directory / f"{WORK_ITEM}.ext4"
            output = (
                f'SOURCE="{image}" FSTYPE="fuse.ext4" '
                f'OPTIONS="rw,nosuid,nodev,user_id={os.geteuid()},group_id={os.getegid()}" '
                f'TARGET="{mountpoint}"\n'
            )
            with patch(
                "codex_dispatcher.runner_disk.run_command",
                return_value=CommandResult(0, output, ""),
            ):
                record = disk._mount_record(mountpoint)
            assert record is not None
            self.assertEqual(image, record.source)
            self.assertEqual(mountpoint, record.target)

            with patch(
                "codex_dispatcher.runner_disk.run_command",
                return_value=CommandResult(0, 'SOURCE="unexpected"\n', ""),
            ):
                with self.assertRaisesRegex(RunnerDiskError, "malformed"):
                    disk._mount_record(mountpoint)

    def test_sparse_or_weak_image_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured, work_items = runtime(root)
            disk = FusedWorkItemDisk(configured, work_items_root=work_items)
            image = configured.image_directory / f"{WORK_ITEM}.ext4"
            image.touch(mode=0o600)
            with image.open("r+b") as stream:
                stream.truncate(IMAGE_BYTES)
            with self.assertRaisesRegex(RunnerDiskError, "identity"):
                disk._validate_image(image)

            image.unlink()
            allocate_dense(image)
            disk._validate_image(image)
            image.chmod(0o640)
            with self.assertRaisesRegex(RunnerDiskError, "identity"):
                disk._validate_image(image)

    def test_failed_provision_preserves_exact_staging_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured, work_items = runtime(root)
            disk = FusedWorkItemDisk(configured, work_items_root=work_items)
            mountpoint = work_items / "owner__repo" / "issue-1"

            with (
                patch.object(disk, "_create_image", side_effect=allocate_dense),
                patch.object(disk, "_format_image"),
                patch.object(disk, "_mount"),
                patch.object(disk, "_validate_mount_root"),
                patch.object(disk, "_validate_mounted_capacity"),
                patch.object(disk, "_best_effort_unmount") as unmount,
            ):
                with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                    with disk.provision(WORK_ITEM, mountpoint):
                        raise RuntimeError("fixture failure")

            self.assertTrue(
                (configured.image_directory / ".staging" / f"{WORK_ITEM}.ext4").is_file()
            )
            self.assertTrue(
                (configured.image_directory / ".staging" / f"{WORK_ITEM}.mount").is_dir()
            )
            self.assertFalse(
                (configured.image_directory / f"{WORK_ITEM}.ext4").exists()
            )
            unmount.assert_called_once()
            with self.assertRaisesRegex(RunnerDiskError, "ambiguous"):
                disk.final_image_exists(WORK_ITEM)

    def test_capacity_rejection_leaves_no_staging_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured, work_items = runtime(root)
            disk = FusedWorkItemDisk(configured, work_items_root=work_items)
            mountpoint = work_items / "owner__repo" / "issue-1"
            with patch.object(disk, "_available_bytes", return_value=0):
                with self.assertRaisesRegex(RunnerDiskError, "free space"):
                    with disk.provision(WORK_ITEM, mountpoint):
                        self.fail("capacity rejection must happen before the yield")

            staging = configured.image_directory / ".staging"
            self.assertFalse((staging / f"{WORK_ITEM}.ext4").exists())
            self.assertFalse((staging / f"{WORK_ITEM}.mount").exists())

    def test_successful_provision_commits_then_requires_final_mount(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured, work_items = runtime(root)
            disk = FusedWorkItemDisk(configured, work_items_root=work_items)
            mountpoint = work_items / "owner__repo" / "issue-1"

            with (
                patch.object(disk, "_create_image", side_effect=allocate_dense),
                patch.object(disk, "_format_image"),
                patch.object(disk, "_mount"),
                patch.object(disk, "_unmount") as unmount,
                patch.object(disk, "_validate_mount_root"),
                patch.object(disk, "_validate_mounted_capacity"),
                patch.object(disk, "_check_filesystem") as check,
                patch.object(disk, "ensure_mounted") as ensure,
            ):
                with disk.provision(WORK_ITEM, mountpoint) as staging:
                    self.assertTrue(staging.is_dir())

            self.assertTrue(
                (configured.image_directory / f"{WORK_ITEM}.ext4").is_file()
            )
            self.assertFalse((configured.image_directory / ".staging" / f"{WORK_ITEM}.mount").exists())
            unmount.assert_called_once()
            check.assert_called_once()
            ensure.assert_called_once_with(WORK_ITEM, mountpoint)


if __name__ == "__main__":
    unittest.main()
