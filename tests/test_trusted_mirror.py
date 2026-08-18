from __future__ import annotations

import base64
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.command_runner import CommandResult, run_command as real_run_command
from codex_dispatcher.source_bundle import SourceBundle
from codex_dispatcher.trusted_mirror import (
    MIRROR_BASE_REF,
    GitHubMirrorRefresher,
    TrustedMirrorError,
    TrustedMirrorSource,
)


BASE_SHA = "a" * 40
TOKEN = "github_pat_fixture_only"


def _bundle(base_sha: str = BASE_SHA) -> SourceBundle:
    artifact = f"bundle:{base_sha}".encode("ascii")
    return SourceBundle(
        artifact,
        base_sha,
        sha256(artifact).hexdigest(),
        len(artifact),
    )


class _FakeRefresher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def refresh(self, repository: str, base_branch: str) -> str:
        self.calls.append((repository, base_branch))
        return BASE_SHA


class _FakeBuilder:
    def __init__(self, *, returned_sha: str = BASE_SHA) -> None:
        self.calls: list[tuple[str, str]] = []
        self.returned_sha = returned_sha

    def build(self, *, repository: str, base_sha: str) -> SourceBundle:
        self.calls.append((repository, base_sha))
        return _bundle(self.returned_sha)


class TrustedMirrorSourceTests(unittest.TestCase):
    def test_current_refreshes_before_build_while_exact_never_fetches(self) -> None:
        refresher = _FakeRefresher()
        builder = _FakeBuilder()
        source = TrustedMirrorSource(refresher=refresher, builder=builder)

        current = source.current("owner/repo", "main")
        exact = source.exact("owner/repo", BASE_SHA)

        self.assertEqual(BASE_SHA, current.base_sha)
        self.assertEqual(BASE_SHA, exact.base_sha)
        self.assertEqual([("owner/repo", "main")], refresher.calls)
        self.assertEqual(
            [("owner/repo", BASE_SHA), ("owner/repo", BASE_SHA)],
            builder.calls,
        )

    def test_builder_base_conflict_fails_closed(self) -> None:
        source = TrustedMirrorSource(
            refresher=_FakeRefresher(),
            builder=_FakeBuilder(returned_sha="b" * 40),
        )

        with self.assertRaisesRegex(TrustedMirrorError, "different base SHA"):
            source.current("owner/repo", "main")
        with self.assertRaisesRegex(TrustedMirrorError, "different base SHA"):
            source.exact("owner/repo", BASE_SHA)


class GitHubMirrorRefresherTests(unittest.TestCase):
    def test_real_git_initializes_only_minimal_local_config_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "mirrors"
            fetch_calls: list[tuple[str, ...]] = []

            def local_git(argv, **kwargs):
                normalized = tuple(argv)
                if "fetch" in normalized:
                    fetch_calls.append(normalized)
                    return CommandResult(0, "", "")
                if f"{MIRROR_BASE_REF}^{{commit}}" in normalized:
                    return CommandResult(0, BASE_SHA + "\n", "")
                return real_run_command(argv, **kwargs)

            refresher = GitHubMirrorRefresher(
                git_path="/usr/bin/git",
                mirror_root=root,
            )
            with patch(
                "codex_dispatcher.trusted_mirror.run_command",
                side_effect=local_git,
            ):
                observed = refresher.refresh("owner/repo", "main")

            self.assertEqual(BASE_SHA, observed)
            self.assertEqual(1, len(fetch_calls))
            self.assertFalse((root / "owner" / "repo.git" / "config").is_symlink())

    def test_fixed_fetch_uses_protected_mirror_and_environment_only_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "mirrors"
            observed: list[tuple[tuple[str, ...], dict[str, object]]] = []

            def fake_run(argv, **kwargs):
                normalized = tuple(argv)
                observed.append((normalized, kwargs))
                if "--is-bare-repository" in normalized:
                    return CommandResult(0, "true\n", "")
                if "--get-regexp" in normalized:
                    return CommandResult(
                        0,
                        "core.repositoryformatversion\ncore.filemode\ncore.bare\n",
                        "",
                    )
                if f"{MIRROR_BASE_REF}^{{commit}}" in normalized:
                    return CommandResult(0, BASE_SHA + "\n", "")
                return CommandResult(0, "", "")

            refresher = GitHubMirrorRefresher(
                git_path=Path("/usr/bin/git"),
                mirror_root=root,
                github_token=TOKEN,
            )
            with patch(
                "codex_dispatcher.trusted_mirror.run_command",
                side_effect=fake_run,
            ):
                first = refresher.refresh("owner/repo", "main")
                second = refresher.refresh("owner/repo", "main")

            self.assertEqual(BASE_SHA, first)
            self.assertEqual(BASE_SHA, second)
            self.assertEqual(9, len(observed))
            self.assertTrue((root / "owner" / "repo.git").is_dir())
            fetches = [item for item in observed if "fetch" in item[0]]
            self.assertEqual(2, len(fetches))
            expected_refspec = f"+refs/heads/main:{MIRROR_BASE_REF}"
            authorization = base64.b64encode(
                f"x-access-token:{TOKEN}".encode("utf-8")
            ).decode("ascii")
            for argv, kwargs in fetches:
                self.assertIn("https://github.com/owner/repo.git", argv)
                self.assertIn(expected_refspec, argv)
                self.assertNotIn("--prune", argv)
                self.assertFalse(any(TOKEN in value for value in argv))
                environment = kwargs["env"]
                self.assertEqual(
                    f"Authorization: Basic {authorization}",
                    environment["GIT_CONFIG_VALUE_0"],
                )
                self.assertEqual((TOKEN, authorization), kwargs["secrets"])
                self.assertEqual("1", environment["GIT_CONFIG_NOSYSTEM"])
                self.assertEqual("0", environment["GIT_TERMINAL_PROMPT"])

    def test_rejects_invalid_identity_and_unprotected_root_before_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "mirrors"
            root.mkdir()
            refresher = GitHubMirrorRefresher(
                git_path="/usr/bin/git",
                mirror_root=root,
            )
            with patch("codex_dispatcher.trusted_mirror.run_command") as command:
                with self.assertRaises(ValueError):
                    refresher.refresh("owner/repo", "--upload-pack=bad")
                root.chmod(0o777)
                with self.assertRaisesRegex(TrustedMirrorError, "mirror root"):
                    refresher.refresh("owner/repo", "main")
            command.assert_not_called()

    def test_git_failure_is_generic_and_does_not_return_provider_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "mirrors"
            mirror = root / "owner" / "repo.git"
            mirror.mkdir(parents=True, mode=0o700)
            root.chmod(0o700)
            mirror.parent.chmod(0o700)
            mirror.chmod(0o700)
            results = iter(
                (
                    CommandResult(0, "true\n", ""),
                    CommandResult(
                        0,
                        "core.repositoryformatversion\ncore.filemode\ncore.bare\n",
                        "",
                    ),
                    CommandResult(1, "", f"remote rejected {TOKEN}"),
                )
            )
            refresher = GitHubMirrorRefresher(
                git_path="/usr/bin/git",
                mirror_root=root,
                github_token=TOKEN,
            )
            with patch(
                "codex_dispatcher.trusted_mirror.run_command",
                side_effect=lambda *args, **kwargs: next(results),
            ):
                with self.assertRaises(TrustedMirrorError) as raised:
                    refresher.refresh("owner/repo", "main")

            self.assertEqual(
                "trusted mirror Git stage failed: base_fetch",
                str(raised.exception),
            )
            self.assertNotIn(TOKEN, str(raised.exception))

    def test_existing_remote_or_behavioral_local_config_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "mirrors"
            mirror = root / "owner" / "repo.git"
            mirror.mkdir(parents=True, mode=0o700)
            root.chmod(0o700)
            mirror.parent.chmod(0o700)
            mirror.chmod(0o700)
            results = iter(
                (
                    CommandResult(0, "true\n", ""),
                    CommandResult(
                        0,
                        "core.repositoryformatversion\ncore.bare\nremote.origin.url\n",
                        "",
                    ),
                )
            )
            refresher = GitHubMirrorRefresher(
                git_path="/usr/bin/git",
                mirror_root=root,
            )
            with patch(
                "codex_dispatcher.trusted_mirror.run_command",
                side_effect=lambda *args, **kwargs: next(results),
            ):
                with self.assertRaisesRegex(TrustedMirrorError, "config is not minimal"):
                    refresher.refresh("owner/repo", "main")


if __name__ == "__main__":
    unittest.main()
