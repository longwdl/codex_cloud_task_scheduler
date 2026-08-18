from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.git_publisher import (
    GitPublicationRejected,
    GitTaskBranchPublisher,
)
from codex_dispatcher.publisher import (
    PublicationPlan,
    PublishRequest,
    VerifiedBundle,
    plan_publication,
)
from codex_dispatcher.work_items import WorkItem, WorkItemState


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


def fixture(root: Path) -> tuple[WorkItem, bytes, str, Path]:
    remote = root / "remote.git"
    seed = root / "seed"
    git(root, "init", "--bare", "--initial-branch=main", str(remote))
    git(root, "init", "--initial-branch=main", str(seed))
    git(seed, "config", "user.name", "Fixture")
    git(seed, "config", "user.email", "fixture@example.invalid")
    (seed / "README.md").write_text("base\n", encoding="utf-8")
    git(seed, "add", "README.md")
    git(seed, "commit", "-m", "base")
    base_sha = git(seed, "rev-parse", "HEAD")
    git(seed, "remote", "add", "origin", remote.as_uri())
    git(seed, "push", "origin", "main")
    mirror = root / "mirrors" / "owner" / "repo.git"
    mirror.parent.mkdir(parents=True)
    git(root, "clone", "--mirror", remote.as_uri(), str(mirror))

    item = WorkItem.new(
        repository="owner/repo",
        issue_number=42,
        issue_node_id="I_kwDOFixture42",
        base_branch="main",
        base_sha=base_sha,
        at="2026-01-01T00:00:00.000000Z",
    )
    item = item.transition_to(WorkItemState.PREPARING)
    item = item.transition_to(WorkItemState.READY)
    item = item.transition_to(WorkItemState.RUNNING)
    git(seed, "checkout", "-b", item.task_branch)
    (seed / "src").mkdir()
    (seed / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    git(seed, "add", "src/main.py")
    git(seed, "commit", "-m", "checkpoint")
    head_sha = git(seed, "rev-parse", "HEAD")
    bundle_path = root / "result.bundle"
    git(seed, "bundle", "create", str(bundle_path), f"refs/heads/{item.task_branch}")
    return item, bundle_path.read_bytes(), head_sha, remote


def plan(item: WorkItem, artifact: bytes, head_sha: str) -> PublicationPlan:
    verified = VerifiedBundle(
        bundle_sha256=sha256(artifact).hexdigest(),
        head_sha=head_sha,
        parent_anchor_sha=item.last_published_sha or item.base_sha,
        changed_paths=("src/main.py",),
        commit_count=1,
        size_bytes=len(artifact),
    )
    return plan_publication(
        request=PublishRequest(item.work_item_id, head_sha),
        work_item=item,
        bundle=verified,
        issue_allowed_paths=("src",),
        repository_allowed_paths=("src",),
    )


class GitTaskBranchPublisherTests(unittest.TestCase):
    def test_pushes_exact_sha_and_recovers_idempotently_after_lost_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item, artifact, head_sha, remote = fixture(root)
            publisher = GitTaskBranchPublisher(
                git_path=GIT,
                mirror_root=root / "mirrors",
                temporary_root=root / "temporary",
            )
            publication_plan = plan(item, artifact, head_sha)
            with patch(
                "codex_dispatcher.git_publisher._validate_github_origin",
                side_effect=lambda origin, _repository: origin,
            ):
                first = publisher.publish(
                    artifact, plan=publication_plan, work_item=item
                )
                repeated = publisher.publish(
                    artifact, plan=publication_plan, work_item=item
                )

            self.assertFalse(first.reused)
            self.assertTrue(repeated.reused)
            self.assertEqual(head_sha, repeated.observed_remote_sha)
            self.assertEqual(
                head_sha,
                git(remote, "rev-parse", f"refs/heads/{item.task_branch}"),
            )
            self.assertEqual([], list((root / "temporary").iterdir()))

    def test_rejects_remote_race_and_tampered_plan_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item, artifact, head_sha, remote = fixture(root)
            seed = root / "seed"
            git(seed, "checkout", "main")
            (seed / "README.md").write_text("racing\n", encoding="utf-8")
            git(seed, "commit", "-am", "racing")
            competing_sha = git(seed, "rev-parse", "HEAD")
            git(seed, "push", "origin", f"HEAD:refs/heads/{item.task_branch}")
            publisher = GitTaskBranchPublisher(
                git_path=GIT,
                mirror_root=root / "mirrors",
                temporary_root=root / "temporary",
            )
            publication_plan = plan(item, artifact, head_sha)
            with patch(
                "codex_dispatcher.git_publisher._validate_github_origin",
                side_effect=lambda origin, _repository: origin,
            ):
                with self.assertRaisesRegex(GitPublicationRejected, "lease"):
                    publisher.publish(
                        artifact, plan=publication_plan, work_item=item
                    )
                with self.assertRaisesRegex(GitPublicationRejected, "plan"):
                    publisher.publish(
                        artifact,
                        plan=replace(publication_plan, target_ref="refs/heads/main"),
                        work_item=item,
                    )
            self.assertEqual(
                competing_sha,
                git(remote, "rev-parse", f"refs/heads/{item.task_branch}"),
            )


if __name__ == "__main__":
    unittest.main()
