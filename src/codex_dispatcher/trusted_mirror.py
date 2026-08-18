"""Refresh fixed GitHub base refs without persistent remotes or credentials."""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path
from typing import Protocol

from codex_dispatcher.command_runner import CommandResult, run_command
from codex_dispatcher.source_bundle import SourceBundle
from codex_dispatcher.work_items import (
    validate_branch,
    validate_git_sha,
    validate_repository,
)


MIRROR_BASE_REF = "refs/codex-dispatcher/base"
_ALLOWED_LOCAL_CONFIG_KEYS = frozenset(
    {
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.ignorecase",
        "core.precomposeunicode",
        "extensions.objectformat",
        "extensions.compatobjectformat",
    }
)


class TrustedMirrorError(RuntimeError):
    """Raised when a mirror cannot be refreshed or inspected unambiguously."""


class MirrorRefresher(Protocol):
    def refresh(self, repository: str, base_branch: str) -> str: ...


class ExactSourceBuilder(Protocol):
    def build(self, *, repository: str, base_sha: str) -> SourceBundle: ...


class TrustedMirrorSource:
    """Bridge a current-ref refresher to the exact source-bundle builder."""

    def __init__(
        self,
        *,
        refresher: MirrorRefresher,
        builder: ExactSourceBuilder,
    ) -> None:
        self._refresher = refresher
        self._builder = builder

    def current(self, repository: str, base_branch: str) -> SourceBundle:
        base_sha = self._refresher.refresh(repository, base_branch)
        bundle = self._builder.build(repository=repository, base_sha=base_sha)
        if bundle.base_sha != base_sha:
            raise TrustedMirrorError("source builder returned a different base SHA")
        return bundle

    def exact(self, repository: str, base_sha: str) -> SourceBundle:
        base_sha = validate_git_sha(base_sha, "base_sha")
        bundle = self._builder.build(repository=repository, base_sha=base_sha)
        if bundle.base_sha != base_sha:
            raise TrustedMirrorError("source builder returned a different base SHA")
        return bundle


class GitHubMirrorRefresher:
    """Fetch one configured GitHub branch into one protected local mirror ref."""

    def __init__(
        self,
        *,
        git_path: str | Path,
        mirror_root: Path,
        github_token: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        executable = str(git_path)
        if not executable or not Path(executable).is_absolute():
            raise ValueError("git_path must be a non-empty absolute path")
        if (
            not isinstance(mirror_root, Path)
            or not mirror_root.is_absolute()
            or ".." in mirror_root.parts
        ):
            raise ValueError("mirror_root must be a normalized absolute path")
        if github_token is not None and (
            not isinstance(github_token, str)
            or not github_token
            or "\x00" in github_token
        ):
            raise ValueError("github_token must be non-empty text without NUL or None")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._git_path = executable
        self._mirror_root = mirror_root
        self._github_token = github_token
        self._timeout_seconds = timeout_seconds

    def refresh(self, repository: str, base_branch: str) -> str:
        repository = validate_repository(repository)
        base_branch = validate_branch(base_branch)
        mirror = self._mirror_path(repository)
        created = self._prepare_mirror_directory(mirror)
        if created:
            self._run(mirror, "init", "--bare", ".", stage="mirror_init")
        bare = self._run(
            mirror,
            "rev-parse",
            "--is-bare-repository",
            stage="mirror_validate",
        ).stdout.strip()
        if bare != "true":
            raise TrustedMirrorError("trusted mirror is not a bare Git repository")
        self._validate_local_config(mirror)

        remote_url = f"https://github.com/{repository}.git"
        source_ref = f"refs/heads/{base_branch}"
        self._run(
            mirror,
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "--no-recurse-submodules",
            remote_url,
            f"+{source_ref}:{MIRROR_BASE_REF}",
            stage="base_fetch",
        )
        base_sha = self._run(
            mirror,
            "rev-parse",
            "--verify",
            f"{MIRROR_BASE_REF}^{{commit}}",
            stage="base_verify",
        ).stdout.strip()
        try:
            return validate_git_sha(base_sha, "base_sha")
        except ValueError as exc:
            raise TrustedMirrorError("Git returned an invalid base SHA") from exc

    def _mirror_path(self, repository: str) -> Path:
        owner, name = repository.split("/", 1)
        return self._mirror_root / owner / f"{name}.git"

    def _prepare_mirror_directory(self, mirror: Path) -> bool:
        self._mirror_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._require_protected_directory(self._mirror_root, "mirror root")
        owner = mirror.parent
        owner.mkdir(mode=0o700, exist_ok=True)
        self._require_protected_directory(owner, "mirror owner directory")
        try:
            mirror.mkdir(mode=0o700)
        except FileExistsError:
            created = False
        else:
            created = True
        self._require_protected_directory(mirror, "trusted mirror")
        return created

    def _validate_local_config(self, mirror: Path) -> None:
        keys = self._run(
            mirror,
            "config",
            "--local",
            "--name-only",
            "--get-regexp",
            ".*",
            stage="mirror_config",
        ).stdout.splitlines()
        if (
            not keys
            or len(keys) != len(set(keys))
            or any(key not in _ALLOWED_LOCAL_CONFIG_KEYS for key in keys)
        ):
            raise TrustedMirrorError("trusted mirror local Git config is not minimal")

    @staticmethod
    def _require_protected_directory(path: Path, field: str) -> None:
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise TrustedMirrorError(f"{field} is unavailable") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
        ):
            raise TrustedMirrorError(
                f"{field} must be an owned, non-symlink, protected directory"
            )

    def _run(
        self,
        repository: Path,
        *arguments: str,
        stage: str,
    ) -> CommandResult:
        environment = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }
        secrets: tuple[str, ...] = ()
        if self._github_token is not None:
            credential = f"x-access-token:{self._github_token}"
            authorization = base64.b64encode(credential.encode("utf-8")).decode("ascii")
            environment.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {authorization}",
                }
            )
            secrets = (self._github_token, authorization)
        argv = (
            self._git_path,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            "core.askPass=",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.https.allow=always",
            "-c",
            "submodule.recurse=false",
            "-c",
            "fetch.fsckObjects=true",
            "-c",
            "transfer.fsckObjects=true",
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "-c",
            "http.followRedirects=initial",
            "-C",
            str(repository),
            *arguments,
        )
        result = run_command(
            argv,
            timeout_seconds=self._timeout_seconds,
            max_output_bytes=1024 * 1024,
            env=environment,
            secrets=secrets,
        )
        if (
            result.returncode != 0
            or result.timed_out
            or result.error is not None
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise TrustedMirrorError(f"trusted mirror Git stage failed: {stage}")
        return result
