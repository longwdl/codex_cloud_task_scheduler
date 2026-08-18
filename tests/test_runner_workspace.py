from __future__ import annotations

import subprocess
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from codex_dispatcher.runner_protocol import RunnerOperation, RunnerRequest
from codex_dispatcher.runner_transport import parse_runner_export_reply
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


if __name__ == "__main__":
    unittest.main()
