from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_dispatcher.source_bundle import GitSourceBundleBuilder, SourceBundleError


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


def create_mirror(root: Path) -> tuple[Path, str, str]:
    source = root / "source"
    git(root, "init", "--initial-branch=main", str(source))
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    (source / "README.md").write_text("base\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "base")
    base_sha = git(source, "rev-parse", "HEAD")
    (source / "README.md").write_text("later\n", encoding="utf-8")
    git(source, "commit", "-am", "later")
    later_sha = git(source, "rev-parse", "HEAD")
    mirror = root / "mirrors" / "owner" / "repo.git"
    mirror.parent.mkdir(parents=True)
    git(root, "clone", "--mirror", str(source), str(mirror))
    return mirror, base_sha, later_sha


class GitSourceBundleBuilderTests(unittest.TestCase):
    def test_builds_self_contained_bundle_for_exact_old_base(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, base_sha, later_sha = create_mirror(root)
            source_bundle = GitSourceBundleBuilder(
                git_path=GIT,
                mirror_root=root / "mirrors",
                temporary_root=root / "temporary",
            ).build(repository="owner/repo", base_sha=base_sha)

            self.assertEqual(base_sha, source_bundle.base_sha)
            self.assertNotEqual(later_sha, source_bundle.base_sha)
            self.assertEqual(len(source_bundle.artifact), source_bundle.size_bytes)
            bundle_path = root / "received.bundle"
            bundle_path.write_bytes(source_bundle.artifact)
            heads = git(root, "bundle", "list-heads", str(bundle_path))
            self.assertEqual(
                f"{base_sha} refs/heads/codex-runner-base",
                heads,
            )
            imported = root / "imported.git"
            git(root, "init", "--bare", str(imported))
            git(
                imported,
                "fetch",
                str(bundle_path),
                f"{base_sha}:refs/heads/imported",
            )
            self.assertEqual(base_sha, git(imported, "rev-parse", "refs/heads/imported"))
            self.assertEqual([], list((root / "temporary").iterdir()))

    def test_rejects_missing_mirror_and_unknown_base_without_leaving_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            builder = GitSourceBundleBuilder(
                git_path=GIT,
                mirror_root=root / "mirrors",
                temporary_root=root / "temporary",
            )
            with self.assertRaisesRegex(SourceBundleError, "mirror"):
                builder.build(repository="owner/repo", base_sha="a" * 40)

            create_mirror(root)
            with self.assertRaisesRegex(SourceBundleError, "base_lookup"):
                builder.build(repository="owner/repo", base_sha="a" * 40)
            self.assertEqual([], list((root / "temporary").iterdir()))


if __name__ == "__main__":
    unittest.main()
