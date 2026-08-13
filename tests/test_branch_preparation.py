from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.branch_preparation import BranchPreparationService
from codex_dispatcher.domain import Run, RunState
from codex_dispatcher.git_workspace import GitWorkspace, GitWorkspaceError
from codex_dispatcher.state_store import StateStore


GIT = "/usr/bin/git"


def git(*arguments: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        (GIT, *arguments),
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    return completed.stdout.strip()


def create_remote(root: Path) -> tuple[Path, str]:
    remote = root / "remote.git"
    seed = root / "seed"
    git("init", "--bare", "--initial-branch=main", str(remote))
    git("init", "--initial-branch=main", str(seed))
    git("config", "user.name", "Test User", cwd=seed)
    git("config", "user.email", "test@example.invalid", cwd=seed)
    (seed / "README.md").write_text("fixture\n", encoding="utf-8")
    git("add", "README.md", cwd=seed)
    git("commit", "-m", "initial", cwd=seed)
    sha = git("rev-parse", "HEAD", cwd=seed)
    git("remote", "add", "origin", remote.as_uri(), cwd=seed)
    git("push", "origin", "main", cwd=seed)
    return remote, sha


class BranchPreparationTests(unittest.TestCase):
    def test_recovers_after_remote_push_before_state_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, base_sha = create_remote(root)
            database = root / "state.db"
            workspace = GitWorkspace(git_path=GIT, workspace_root=root / "workspace")
            with StateStore(database) as store:
                store.migrate()
                run = Run.new(
                    run_id="recovery-run",
                    repository="owner/repo",
                    issue_number=7,
                    prompt_sha256="a" * 64,
                    base_branch="main",
                    cloud_environment_id="env-test",
                )
                store.create_run(run)
                store.update_state(run.run_id, RunState.CLAIMED)
                service = BranchPreparationService(store=store, workspace=workspace)
                with patch(
                    "codex_dispatcher.git_workspace._validate_remote",
                    side_effect=lambda remote_url, _repository: remote_url,
                ):
                    with patch.object(
                        store,
                        "mark_branch_prepared",
                        side_effect=RuntimeError("simulated crash after push"),
                    ):
                        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                            service.prepare_run(run.run_id, remote_url=remote.as_uri())

                    interrupted = store.get_run(run.run_id)
                    assert interrupted is not None
                    self.assertEqual(RunState.CLAIMED, interrupted.state)
                    self.assertEqual(base_sha, interrupted.base_sha)
                    assert interrupted.task_branch is not None
                    self.assertEqual(
                        base_sha,
                        git(
                            f"--git-dir={remote}",
                            "rev-parse",
                            f"refs/heads/{interrupted.task_branch}",
                        ),
                    )

                    recovered = service.prepare_run(run.run_id, remote_url=remote.as_uri())

                self.assertTrue(recovered.published.reused)
                self.assertEqual(RunState.BRANCH_PREPARED, recovered.run.state)
                self.assertEqual(base_sha, recovered.run.head_sha)

            with closing(sqlite3.connect(database)) as connection:
                events = connection.execute(
                    "SELECT event_type FROM run_events WHERE run_id = ? ORDER BY event_id",
                    ("recovery-run",),
                ).fetchall()
            self.assertEqual(
                [("branch_anchor_recorded",), ("branch_prepared",)],
                events,
            )

    def test_recovery_uses_persisted_base_after_main_advances(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, base_sha = create_remote(root)
            seed = root / "seed"
            (seed / "README.md").write_text("fixture\nmain advanced\n", encoding="utf-8")
            git("add", "README.md", cwd=seed)
            git("commit", "-m", "advance main", cwd=seed)
            git("push", "origin", "main", cwd=seed)

            workspace = GitWorkspace(git_path=GIT, workspace_root=root / "workspace")
            with patch(
                "codex_dispatcher.git_workspace._validate_remote",
                side_effect=lambda remote_url, _repository: remote_url,
            ):
                prepared = workspace.prepare(
                    repository="owner/repo",
                    remote_url=remote.as_uri(),
                    base_branch="main",
                    task_branch="codex/issue-8-persisted",
                    expected_base_sha=base_sha,
                )
                published = workspace.publish_task_branch(prepared)

            self.assertEqual(base_sha, prepared.base_sha)
            self.assertEqual(base_sha, published.head_sha)
            self.assertFalse(published.reused)

    def test_remote_branch_race_does_not_overwrite_existing_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote, base_sha = create_remote(root)
            workspace = GitWorkspace(git_path=GIT, workspace_root=root / "workspace")
            branch = "codex/issue-9-race"
            with patch(
                "codex_dispatcher.git_workspace._validate_remote",
                side_effect=lambda remote_url, _repository: remote_url,
            ):
                prepared = workspace.prepare(
                    repository="owner/repo",
                    remote_url=remote.as_uri(),
                    base_branch="main",
                    task_branch=branch,
                )
                seed = root / "seed"
                (seed / "README.md").write_text("fixture\nracing writer\n", encoding="utf-8")
                git("add", "README.md", cwd=seed)
                git("commit", "-m", "racing task branch", cwd=seed)
                competing_sha = git("rev-parse", "HEAD", cwd=seed)
                git("push", "origin", f"HEAD:refs/heads/{branch}", cwd=seed)

                with self.assertRaises(GitWorkspaceError):
                    workspace.publish_task_branch(prepared)

            self.assertNotEqual(base_sha, competing_sha)
            self.assertEqual(
                competing_sha,
                git(f"--git-dir={remote}", "rev-parse", f"refs/heads/{branch}"),
            )
