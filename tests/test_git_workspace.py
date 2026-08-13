from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.command_runner import CommandResult
from codex_dispatcher.git_workspace import GitWorkspace, GitWorkspaceError


GIT = "/usr/bin/git"
REPOSITORY = "owner/repo"
REMOTE = "https://github.com/owner/repo.git"


class GitWorkspaceTests(unittest.TestCase):
    def test_prepare_uses_safe_fixed_config_and_returns_clean_worktree(self) -> None:
        sha = "a" * 40
        calls: list[tuple[str, ...]] = []
        environments: list[dict[str, str]] = []

        def fake_run(argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            calls.append(argv)
            environments.append(dict(kwargs["env"]))  # type: ignore[arg-type]
            if "ls-remote" in argv:
                return CommandResult(0, "", "")
            if "--git-common-dir" in argv:
                return CommandResult(0, str(root / "mirrors" / "owner" / "repo.git") + "\n", "")
            if "symbolic-ref" in argv:
                return CommandResult(0, "agent/issue-1-run\n", "")
            if "rev-parse" in argv:
                return CommandResult(0, sha + "\n", "")
            if "ls-tree" in argv:
                return CommandResult(0, "README.md\x00", "")
            return CommandResult(0, "", "")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch("codex_dispatcher.git_workspace.run_command", side_effect=fake_run):
                prepared = GitWorkspace(
                    git_path=GIT,
                    workspace_root=root,
                    github_token="github_pat_test_fixture",
                ).prepare(
                    repository=REPOSITORY,
                    remote_url=REMOTE,
                    base_branch="main",
                    task_branch="agent/issue-1-run",
                )

        self.assertEqual(sha, prepared.base_sha)
        self.assertTrue(any("clone" in call for call in calls))
        self.assertTrue(any("--recurse-submodules=no" in call for call in calls))
        for call in calls:
            self.assertIn("core.hooksPath=/dev/null", call)
            self.assertIn("protocol.allow=never", call)
            self.assertIn("submodule.recurse=false", call)
            self.assertFalse(any("github_pat_test_fixture" in item for item in call))
            self.assertFalse(any("extraheader=Authorization: Basic" in item for item in call))
        for environment in environments:
            self.assertEqual("http.https://github.com/.extraheader", environment["GIT_CONFIG_KEY_0"])
            self.assertTrue(environment["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic "))

    def test_unsafe_remote_branch_and_existing_worktree_fail_before_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = GitWorkspace(git_path=GIT, workspace_root=root)
            with patch("codex_dispatcher.git_workspace.run_command") as runner:
                with self.assertRaises(ValueError):
                    workspace.prepare(
                        repository=REPOSITORY,
                        remote_url="file:///tmp/repo.git",
                        base_branch="main",
                        task_branch="agent/run",
                    )
                with self.assertRaises(ValueError):
                    workspace.prepare(
                        repository=REPOSITORY,
                        remote_url=REMOTE,
                        base_branch="../main",
                        task_branch="agent/run",
                    )
            runner.assert_not_called()

            with patch("codex_dispatcher.git_workspace.run_command") as runner:
                with self.assertRaisesRegex(ValueError, "expected_base_sha"):
                    workspace.prepare(
                        repository=REPOSITORY,
                        remote_url=REMOTE,
                        base_branch="main",
                        task_branch="agent/run",
                        expected_base_sha=123,  # type: ignore[arg-type]
                    )
            runner.assert_not_called()

            worktree = root / "worktrees" / "owner" / "repo" / "agent__run"
            worktree.mkdir(parents=True)
            with patch("codex_dispatcher.git_workspace.run_command") as runner:
                with self.assertRaisesRegex(GitWorkspaceError, "linked Git worktree"):
                    workspace.prepare(
                        repository=REPOSITORY,
                        remote_url=REMOTE,
                        base_branch="main",
                        task_branch="agent/run",
                    )
            runner.assert_not_called()

    def test_submodules_and_attribute_drivers_are_rejected_before_checkout(self) -> None:
        sha = "a" * 40

        def run_with_tree(tree: str, attributes: str = "") -> GitWorkspaceError:
            def fake_run(argv: tuple[str, ...], **_: object) -> CommandResult:
                if "rev-parse" in argv:
                    return CommandResult(0, sha + "\n", "")
                if "ls-tree" in argv:
                    return CommandResult(0, tree, "")
                if "show" in argv:
                    return CommandResult(0, attributes, "")
                return CommandResult(0, "", "")

            with tempfile.TemporaryDirectory() as temp_dir:
                with patch("codex_dispatcher.git_workspace.run_command", side_effect=fake_run):
                    with self.assertRaises(GitWorkspaceError) as caught:
                        GitWorkspace(git_path=GIT, workspace_root=Path(temp_dir)).prepare(
                            repository=REPOSITORY,
                            remote_url=REMOTE,
                            base_branch="main",
                            task_branch="agent/run",
                        )
            return caught.exception

        self.assertIn("submodules", str(run_with_tree(".gitmodules\x00README.md\x00")))
        self.assertIn(
            "attribute driver",
            str(run_with_tree(".gitattributes\x00", "*.bin filter=malicious\n")),
        )


if __name__ == "__main__":
    unittest.main()
