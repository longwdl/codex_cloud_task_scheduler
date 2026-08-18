from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from codex_dispatcher.git_bundle_verifier import (
    GitBundleQuarantineVerifier,
    GitBundleVerificationError,
)
from codex_dispatcher.runner_transport import RunnerExportReply
from codex_dispatcher.work_items import WorkItem


GIT = "/usr/bin/git"


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


def make_bundle(
    root: Path,
    *,
    changed_path: str = "src/main.py",
    content: bytes = b"print('ok')\n",
    symlink: bool = False,
) -> tuple[bytes, str, str]:
    repository = root / "source"
    git(root, "init", "--initial-branch=main", str(repository))
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    git(repository, "add", "README.md")
    git(repository, "commit", "-m", "base")
    base_sha = git(repository, "rev-parse", "HEAD")

    candidate = repository / changed_path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if symlink:
        os.symlink("../README.md", candidate)
    else:
        candidate.write_bytes(content)
    git(repository, "add", "--", changed_path)
    git(repository, "commit", "-m", "checkpoint")
    head_sha = git(repository, "rev-parse", "HEAD")
    bundle_path = root / "result.bundle"
    git(repository, "bundle", "create", str(bundle_path), "refs/heads/main")
    return bundle_path.read_bytes(), base_sha, head_sha


def item(base_sha: str) -> WorkItem:
    return WorkItem.new(
        repository="owner/repo",
        issue_number=42,
        issue_node_id="I_kwDOFixture42",
        base_branch="main",
        base_sha=base_sha,
        at="2026-01-01T00:00:00.000000Z",
    )


def manifest(work_item: WorkItem, artifact: bytes, head_sha: str) -> RunnerExportReply:
    return RunnerExportReply(
        work_item_id=work_item.work_item_id,
        head_sha=head_sha,
        bundle_sha256=sha256(artifact).hexdigest(),
        size_bytes=len(artifact),
    )


class GitBundleQuarantineVerifierTests(unittest.TestCase):
    def test_imports_self_contained_bundle_and_returns_verified_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, base_sha, head_sha = make_bundle(root)
            work_item = item(base_sha)
            result = GitBundleQuarantineVerifier(
                git_path=GIT,
                quarantine_root=root / "quarantine",
            ).verify(
                artifact,
                manifest=manifest(work_item, artifact, head_sha),
                work_item=work_item,
            )

            self.assertEqual(head_sha, result.head_sha)
            self.assertEqual(base_sha, result.parent_anchor_sha)
            self.assertEqual(("src/main.py",), result.changed_paths)
            self.assertEqual(1, result.commit_count)
            self.assertEqual(len(artifact), result.size_bytes)
            self.assertEqual([], list((root / "quarantine").iterdir()))

    def test_rejects_missing_anchor_and_unsafe_changed_objects(self) -> None:
        cases = (
            (".gitattributes", b"*.bin diff=external\n", False, "behavior"),
            ("src/link.py", b"", True, "non-regular"),
            ("src/blob.bin", b"before\x00after", False, "binary"),
            ("src/key.txt", ("ghp_" + "A" * 36).encode(), False, "credential"),
        )
        for path, content, symlink, message in cases:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                artifact, base_sha, head_sha = make_bundle(
                    root,
                    changed_path=path,
                    content=content,
                    symlink=symlink,
                )
                work_item = item(base_sha)
                verifier = GitBundleQuarantineVerifier(
                    git_path=GIT,
                    quarantine_root=root / "quarantine",
                )
                with self.assertRaisesRegex(GitBundleVerificationError, message):
                    verifier.verify(
                        artifact,
                        manifest=manifest(work_item, artifact, head_sha),
                        work_item=work_item,
                    )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, _, head_sha = make_bundle(root)
            wrong_anchor = item("d" * 40)
            with self.assertRaisesRegex(GitBundleVerificationError, "anchor"):
                GitBundleQuarantineVerifier(
                    git_path=GIT,
                    quarantine_root=root / "quarantine",
                ).verify(
                    artifact,
                    manifest=manifest(wrong_anchor, artifact, head_sha),
                    work_item=wrong_anchor,
                )


if __name__ == "__main__":
    unittest.main()
