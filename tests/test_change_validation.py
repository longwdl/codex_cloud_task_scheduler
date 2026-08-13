from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_dispatcher.change_validation import ChangeValidationError, WorktreeChangeValidator


GIT = "/usr/bin/git"


def git(worktree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        (GIT, "-C", str(worktree), *arguments),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    return completed.stdout.strip()


def repository(root: Path) -> tuple[Path, str]:
    worktree = root / "repo"
    git(root, "init", "--initial-branch=main", str(worktree))
    git(worktree, "config", "user.name", "Test User")
    git(worktree, "config", "user.email", "test@example.invalid")
    (worktree / "README.md").write_text("base\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    git(worktree, "commit", "-m", "base")
    return worktree, git(worktree, "rev-parse", "HEAD")


class WorktreeChangeValidatorTests(unittest.TestCase):
    def test_accepts_text_changes_in_intersection_of_both_allowlists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree, base_sha = repository(Path(temp_dir))
            (worktree / "README.md").write_text("base\nchanged\n", encoding="utf-8")
            docs = worktree / "docs"
            docs.mkdir()
            (docs / "note.md").write_text("new\n", encoding="utf-8")

            result = WorktreeChangeValidator(git_path=GIT).validate(
                worktree=worktree,
                base_sha=base_sha,
                issue_allowed_paths=("README.md", "docs"),
                repository_allowed_paths=("README.md", "docs"),
            )

            self.assertEqual(("README.md", "docs/note.md"), result.changed_paths)
            self.assertEqual((), result.binary_paths)

    def test_rejects_issue_repository_and_hard_deny_policy_violations(self) -> None:
        cases = (
            ("outside.txt", ("README.md",), ("README.md", "outside.txt"), ()),
            ("outside.txt", ("README.md", "outside.txt"), ("README.md",), ()),
            (".env.local", (".env.local",), (".env.local",), ()),
            ("docs/private.txt", ("docs",), ("docs",), ("docs/private.txt",)),
        )
        for path, issue_paths, repository_paths, denied_paths in cases:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as temp_dir:
                worktree, base_sha = repository(Path(temp_dir))
                candidate = worktree / path
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text("new\n", encoding="utf-8")
                with self.assertRaises(ChangeValidationError):
                    WorktreeChangeValidator(git_path=GIT).validate(
                        worktree=worktree,
                        base_sha=base_sha,
                        issue_allowed_paths=issue_paths,
                        repository_allowed_paths=repository_paths,
                        repository_denied_paths=denied_paths,
                    )

    def test_rejects_git_behavior_binary_symlink_and_whitespace_changes(self) -> None:
        cases: tuple[tuple[str, bytes | None], ...] = (
            (".gitattributes", b"*.bin diff=external\n"),
            ("asset.bin", b"before\x00after"),
            ("bad.txt", b"trailing \n"),
            ("link.txt", None),
        )
        for path, content in cases:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as temp_dir:
                worktree, base_sha = repository(Path(temp_dir))
                candidate = worktree / path
                if content is None:
                    os.symlink("README.md", candidate)
                else:
                    candidate.write_bytes(content)
                with self.assertRaises(ChangeValidationError):
                    WorktreeChangeValidator(git_path=GIT).validate(
                        worktree=worktree,
                        base_sha=base_sha,
                        issue_allowed_paths=(path,),
                        repository_allowed_paths=(path,),
                    )

    def test_rejects_empty_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            worktree, base_sha = repository(Path(temp_dir))
            with self.assertRaisesRegex(ChangeValidationError, "no changes"):
                WorktreeChangeValidator(git_path=GIT).validate(
                    worktree=worktree,
                    base_sha=base_sha,
                    issue_allowed_paths=("README.md",),
                    repository_allowed_paths=("README.md",),
                )

    def test_ignored_untracked_file_and_symlink_parent_are_not_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worktree, base_sha = repository(root)
            (worktree / ".gitignore").write_text("hidden.txt\n", encoding="utf-8")
            (worktree / "hidden.txt").write_text("hidden\n", encoding="utf-8")
            with self.assertRaisesRegex(ChangeValidationError, "Issue policy"):
                WorktreeChangeValidator(git_path=GIT).validate(
                    worktree=worktree,
                    base_sha=base_sha,
                    issue_allowed_paths=(".gitignore",),
                    repository_allowed_paths=(".gitignore", "hidden.txt"),
                )

            outside = root / "outside"
            outside.mkdir()
            (outside / "escaped.txt").write_text("escaped\n", encoding="utf-8")
            os.symlink(outside, worktree / "linked-dir")
            with self.assertRaisesRegex(ChangeValidationError, "symbolic-link"):
                WorktreeChangeValidator(git_path=GIT).validate(
                    worktree=worktree,
                    base_sha=base_sha,
                    issue_allowed_paths=(".gitignore", "hidden.txt", "linked-dir"),
                    repository_allowed_paths=(".gitignore", "hidden.txt", "linked-dir"),
                )


if __name__ == "__main__":
    unittest.main()
