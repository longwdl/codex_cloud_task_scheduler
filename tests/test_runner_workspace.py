from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from codex_dispatcher.runner_protocol import (
    NEXT_PROTOCOL_VERSION,
    RunnerOperation,
    RunnerRequest,
)
from codex_dispatcher.runner_transport import (
    RunnerArchiveState,
    parse_runner_export_reply,
)
from codex_dispatcher.runner_workspace import RunnerWorkspace, RunnerWorkspaceError
from codex_dispatcher.source_bundle import GitSourceBundleBuilder


GIT = "/usr/bin/git"
WORK_ITEM = "wi_" + "a" * 24


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        (GIT, "-C", str(repository), *arguments),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    return completed.stdout.strip()


def fixture(root: Path, *, attributes: str | None = None) -> tuple[bytes, str]:
    source = root / "source"
    git(root, "init", "--initial-branch=main", str(source))
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    (source / "README.md").write_text("base\n", encoding="utf-8")
    git(source, "add", "README.md")
    if attributes is not None:
        (source / ".gitattributes").write_text(attributes, encoding="utf-8")
        git(source, "add", ".gitattributes")
    git(source, "commit", "-m", "base")
    base_sha = git(source, "rev-parse", "HEAD")
    mirror = root / "mirrors" / "owner" / "repo.git"
    mirror.parent.mkdir(parents=True)
    git(root, "clone", "--mirror", str(source), str(mirror))
    bundle = GitSourceBundleBuilder(
        git_path=GIT,
        mirror_root=root / "mirrors",
        temporary_root=root / "source-temporary",
    ).build(repository="owner/repo", base_sha=base_sha)
    return bundle.artifact, base_sha


def prepare_request(artifact: bytes, base_sha: str) -> RunnerRequest:
    return RunnerRequest(
        RunnerOperation.PREPARE,
        WORK_ITEM,
        repository="owner/repo",
        issue_number=42,
        task_branch="codex/issue-42-aaaaaaaaaaaa",
        base_sha=base_sha,
        source_bundle_sha256=sha256(artifact).hexdigest(),
        source_bundle_size=len(artifact),
    )


class RunnerWorkspaceTests(unittest.TestCase):
    def test_archive_is_permanent_idempotent_and_head_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, base_sha = fixture(root)
            workspace = RunnerWorkspace(
                git_path=GIT, work_items_root=root / "runner"
            )
            prepare = prepare_request(artifact, base_sha)
            workspace.prepare(prepare, artifact)
            with self.assertRaisesRegex(RunnerWorkspaceError, "protocol-v2"):
                workspace.archive(
                    RunnerRequest(RunnerOperation.ARCHIVE, WORK_ITEM)
                )
            status_request = RunnerRequest(
                RunnerOperation.ARCHIVE_STATUS,
                WORK_ITEM,
                version=NEXT_PROTOCOL_VERSION,
                expected_head_sha=base_sha,
            )
            self.assertIs(
                RunnerArchiveState.ACTIVE,
                workspace.archive_status(status_request).state,
            )
            archive_request = RunnerRequest(
                RunnerOperation.ARCHIVE,
                WORK_ITEM,
                version=NEXT_PROTOCOL_VERSION,
                expected_head_sha=base_sha,
            )
            archived = workspace.archive(archive_request)
            repeated = workspace.archive(archive_request)
            self.assertIs(RunnerArchiveState.ARCHIVED, archived.state)
            self.assertEqual(archived.archived_at, repeated.archived_at)
            self.assertGreater(archived.reclaimed_bytes or 0, 0)
            self.assertFalse(
                (root / "runner" / "owner__repo" / "issue-42").exists()
            )
            self.assertTrue(
                (root / "runner" / ".registry" / f"{WORK_ITEM}.json").is_file()
            )
            self.assertIs(
                RunnerArchiveState.ARCHIVED,
                workspace.archive_status(status_request).state,
            )
            with self.assertRaisesRegex(RunnerWorkspaceError, "permanent"):
                workspace.prepare(prepare, artifact)

    def test_archive_rejects_dirty_or_wrong_head_before_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, base_sha = fixture(root)
            workspace = RunnerWorkspace(
                git_path=GIT, work_items_root=root / "runner"
            )
            workspace.prepare(prepare_request(artifact, base_sha), artifact)
            with self.assertRaisesRegex(RunnerWorkspaceError, "conflicts"):
                workspace.archive(
                    RunnerRequest(
                        RunnerOperation.ARCHIVE,
                        WORK_ITEM,
                        version=NEXT_PROTOCOL_VERSION,
                        expected_head_sha="f" * 40,
                    )
                )
            (workspace.paths(WORK_ITEM).repository / "dirty.txt").write_text(
                "dirty\n", encoding="utf-8"
            )
            with self.assertRaises(RunnerWorkspaceError):
                workspace.archive(
                    RunnerRequest(
                        RunnerOperation.ARCHIVE,
                        WORK_ITEM,
                        version=NEXT_PROTOCOL_VERSION,
                        expected_head_sha=base_sha,
                    )
                )
            self.assertFalse(
                (root / "runner" / ".archives" / f"{WORK_ITEM}.json").exists()
            )

    def test_archive_rejects_registry_payload_for_another_work_item(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, base_sha = fixture(root)
            workspace = RunnerWorkspace(
                git_path=GIT, work_items_root=root / "runner"
            )
            second_work_item = "wi_" + "b" * 24
            first_request = prepare_request(artifact, base_sha)
            second_request = RunnerRequest(
                RunnerOperation.PREPARE,
                second_work_item,
                repository="owner/repo",
                issue_number=43,
                task_branch="codex/issue-43-bbbbbbbbbbbb",
                base_sha=base_sha,
                source_bundle_sha256=sha256(artifact).hexdigest(),
                source_bundle_size=len(artifact),
            )
            workspace.prepare(first_request, artifact)
            workspace.prepare(second_request, artifact)
            first_root = root / "runner" / "owner__repo" / "issue-42"
            second_root = root / "runner" / "owner__repo" / "issue-43"
            registry = root / "runner" / ".registry"
            (registry / f"{WORK_ITEM}.json").write_bytes(
                (registry / f"{second_work_item}.json").read_bytes()
            )

            with self.assertRaisesRegex(RunnerWorkspaceError, "filename"):
                workspace.archive(
                    RunnerRequest(
                        RunnerOperation.ARCHIVE,
                        WORK_ITEM,
                        version=NEXT_PROTOCOL_VERSION,
                        expected_head_sha=base_sha,
                    )
                )

            self.assertTrue(first_root.is_dir())
            self.assertTrue(second_root.is_dir())
            self.assertFalse(
                (root / "runner" / ".archives" / f"{WORK_ITEM}.json").exists()
            )

    def test_partial_archive_binds_registry_identity_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, base_sha = fixture(root)
            workspace = RunnerWorkspace(
                git_path=GIT, work_items_root=root / "runner"
            )
            workspace.prepare(prepare_request(artifact, base_sha), artifact)
            request = RunnerRequest(
                RunnerOperation.ARCHIVE,
                WORK_ITEM,
                version=NEXT_PROTOCOL_VERSION,
                expected_head_sha=base_sha,
            )
            with (
                patch.object(
                    workspace,
                    "_archive_directory_tree",
                    side_effect=RunnerWorkspaceError("fixture crash"),
                ),
                self.assertRaisesRegex(RunnerWorkspaceError, "fixture crash"),
            ):
                workspace.archive(request)
            registry = root / "runner" / ".registry" / f"{WORK_ITEM}.json"
            payload = json.loads(registry.read_text(encoding="utf-8"))
            payload["issue_number"] = 43
            registry.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RunnerWorkspaceError, "tombstone"):
                workspace.archive(request)

    def test_archive_rejects_unexpected_nested_mount_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, base_sha = fixture(root)
            workspace = RunnerWorkspace(
                git_path=GIT, work_items_root=root / "runner"
            )
            workspace.prepare(prepare_request(artifact, base_sha), artifact)
            request = RunnerRequest(
                RunnerOperation.ARCHIVE,
                WORK_ITEM,
                version=NEXT_PROTOCOL_VERSION,
                expected_head_sha=base_sha,
            )
            repository = workspace.paths(WORK_ITEM).repository
            original_ismount = os.path.ismount

            def mounted(path: object) -> bool:
                return Path(path) == repository or original_ismount(path)

            with (
                patch("codex_dispatcher.runner_workspace.os.path.ismount", mounted),
                self.assertRaisesRegex(RunnerWorkspaceError, "mount"),
            ):
                workspace.archive(request)
            self.assertTrue(repository.is_dir())

    def test_prepare_is_idempotent_and_creates_no_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, base_sha = fixture(root)
            workspace = RunnerWorkspace(
                git_path=GIT, work_items_root=root / "runner"
            )
            request = prepare_request(artifact, base_sha)
            first = workspace.prepare(request, artifact)
            second = workspace.prepare(request, artifact)
            paths = workspace.paths(WORK_ITEM)

            self.assertEqual(first, second)
            self.assertEqual(
                root / "runner" / "owner__repo" / "issue-42", paths.root
            )
            self.assertEqual(base_sha, workspace.current_head(WORK_ITEM))
            self.assertEqual(
                "codex/issue-42-aaaaaaaaaaaa",
                git(paths.repository, "branch", "--show-current"),
            )
            self.assertEqual("", git(paths.repository, "remote"))
            self.assertFalse((paths.state / "source.bundle").exists())
            self.assertFalse((paths.root / "codex-home").exists())

    def test_exports_exact_clean_checkpoint_as_self_contained_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, base_sha = fixture(root)
            workspace = RunnerWorkspace(
                git_path=GIT, work_items_root=root / "runner"
            )
            workspace.prepare(prepare_request(artifact, base_sha), artifact)
            repository = workspace.paths(WORK_ITEM).repository
            (repository / "result.txt").write_text("done\n", encoding="utf-8")
            git(repository, "add", "result.txt")
            git(repository, "commit", "-m", "checkpoint")
            head = git(repository, "rev-parse", "HEAD")
            output = workspace.export(
                RunnerRequest(
                    RunnerOperation.EXPORT,
                    WORK_ITEM,
                    expected_head_sha=head,
                )
            )
            manifest = parse_runner_export_reply(output.payload)

            self.assertEqual(head, manifest.head_sha)
            self.assertEqual(output.artifact, manifest.validate_artifact(output.artifact))
            bundle_path = root / "exported.bundle"
            assert output.artifact is not None
            bundle_path.write_bytes(output.artifact)
            self.assertEqual(
                f"{head} refs/heads/codex/issue-42-aaaaaaaaaaaa",
                git(root, "bundle", "list-heads", str(bundle_path)),
            )

            (repository / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(RunnerWorkspaceError, "uncommitted"):
                workspace.export(
                    RunnerRequest(
                        RunnerOperation.EXPORT,
                        WORK_ITEM,
                        expected_head_sha=head,
                    )
                )

    def test_rejects_conflicting_or_behavior_changing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, base_sha = fixture(root)
            workspace = RunnerWorkspace(
                git_path=GIT, work_items_root=root / "runner"
            )
            request = prepare_request(artifact, base_sha)
            with self.assertRaisesRegex(RunnerWorkspaceError, "match"):
                workspace.prepare(request, artifact + b"x")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, base_sha = fixture(root, attributes="*.txt filter=malicious\n")
            workspace = RunnerWorkspace(
                git_path=GIT, work_items_root=root / "runner"
            )
            with self.assertRaisesRegex(RunnerWorkspaceError, "attribute"):
                workspace.prepare(prepare_request(artifact, base_sha), artifact)
            self.assertFalse((root / "runner" / ".registry" / f"{WORK_ITEM}.json").exists())

    def test_bounded_prepare_uses_one_committed_work_item_filesystem(self) -> None:
        class FakeBoundedDisk:
            def __init__(self, root: Path) -> None:
                self.root = root
                self.committed: set[str] = set()
                self.ensure_calls: list[tuple[str, Path]] = []

            def final_image_exists(self, work_item_id: str) -> bool:
                return work_item_id in self.committed

            @contextmanager
            def provision(
                self, work_item_id: str, mountpoint: Path
            ) -> Iterator[Path]:
                staging = self.root / ".bounded-staging" / work_item_id
                staging.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                staging.mkdir(mode=0o700)
                yield staging
                mountpoint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                staging.rename(mountpoint)
                self.committed.add(work_item_id)

            def ensure_mounted(self, work_item_id: str, mountpoint: Path) -> None:
                if work_item_id not in self.committed or not mountpoint.is_dir():
                    raise AssertionError("bounded disk was not committed")
                self.ensure_calls.append((work_item_id, mountpoint))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, base_sha = fixture(root)
            disk = FakeBoundedDisk(root)
            workspace = RunnerWorkspace(
                git_path=GIT,
                work_items_root=root / "runner",
                work_item_disk=disk,  # type: ignore[arg-type]
            )
            request = prepare_request(artifact, base_sha)

            first = workspace.prepare(request, artifact)
            second = workspace.prepare(request, artifact)
            paths = workspace.paths(WORK_ITEM)

            self.assertEqual(first, second)
            self.assertIn(WORK_ITEM, disk.committed)
            self.assertEqual(base_sha, workspace.current_head(WORK_ITEM))
            self.assertTrue(paths.repository.is_dir())
            self.assertTrue(paths.state.is_dir())
            self.assertGreaterEqual(len(disk.ensure_calls), 3)


if __name__ == "__main__":
    unittest.main()
