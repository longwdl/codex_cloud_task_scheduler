"""Read-only validation of an applied task worktree before commit or delivery."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from codex_dispatcher.command_runner import CommandResult, run_command
from codex_dispatcher.task_spec import is_path_allowed, normalize_repo_path


class ChangeValidationError(RuntimeError):
    """Raised when a worktree change cannot be proven safe."""


@dataclass(frozen=True, slots=True)
class ChangeValidationResult:
    changed_paths: tuple[str, ...]
    binary_paths: tuple[str, ...]


_SHA_RE = re.compile(r"[0-9a-f]{40,64}")
_MAX_NEW_FILE_BYTES = 10_000_000


class WorktreeChangeValidator:
    """Validate path policy and Git metadata without executing repository code."""

    def __init__(self, *, git_path: str | Path, timeout_seconds: float = 30.0) -> None:
        executable = str(git_path)
        if not executable or not Path(executable).is_absolute():
            raise ValueError("git_path must be a non-empty absolute path")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._git_path = executable
        self._timeout_seconds = timeout_seconds

    def validate(
        self,
        *,
        worktree: Path,
        base_sha: str,
        issue_allowed_paths: tuple[str, ...],
        repository_allowed_paths: tuple[str, ...],
        repository_denied_paths: tuple[str, ...] = (),
    ) -> ChangeValidationResult:
        if not worktree.is_absolute() or not worktree.is_dir():
            raise ValueError("worktree must be an existing absolute directory")
        if not isinstance(base_sha, str) or _SHA_RE.fullmatch(base_sha) is None:
            raise ValueError("base_sha must be a lowercase Git object ID")
        policy_sets = (
            issue_allowed_paths,
            repository_allowed_paths,
            repository_denied_paths,
        )
        if any(
            not isinstance(paths, tuple) or any(not isinstance(path, str) for path in paths)
            for paths in policy_sets
        ):
            raise TypeError("path policies must be tuples of strings")

        self._run(worktree, "rev-parse", "--verify", f"{base_sha}^{{commit}}")
        tracked = self._nul_paths(
            self._run(
                worktree,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--name-only",
                "-z",
                base_sha,
                "--",
            ).stdout
        )
        untracked = self._nul_paths(
            self._run(
                worktree,
                "ls-files",
                "--others",
                "-z",
                "--",
            ).stdout
        )
        changed = tuple(sorted(set(tracked) | set(untracked)))
        if not changed:
            raise ChangeValidationError("worktree contains no changes")
        if any(path.rsplit("/", 1)[-1] in {".gitmodules", ".gitattributes"} for path in changed):
            raise ChangeValidationError("Git behavior files cannot be modified automatically")
        for path in changed:
            if not is_path_allowed(path, issue_allowed_paths, repository_denied_paths):
                raise ChangeValidationError(f"changed path is outside Issue policy: {path}")
            if not is_path_allowed(path, repository_allowed_paths, repository_denied_paths):
                raise ChangeValidationError(f"changed path is outside repository policy: {path}")
            candidate = worktree / path
            self._reject_symlink_components(worktree, path)
            if candidate.exists() and not candidate.is_file():
                raise ChangeValidationError(f"changed path is not a regular file: {path}")

        binary = set(
            self._nul_paths(
                self._run(
                    worktree,
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-renames",
                    "--numstat",
                    "-z",
                    base_sha,
                    "--",
                ).stdout,
                numstat=True,
            )
        )
        for path in changed:
            candidate = worktree / path
            if not candidate.exists():
                continue
            content = self._read_regular_file(worktree, path)
            if b"\x00" in content:
                binary.add(path)
        for path in untracked:
            self._check_untracked_whitespace(worktree, path)
        if binary:
            raise ChangeValidationError(
                "binary changes are not supported: " + ", ".join(sorted(binary))
            )
        self._run(
            worktree,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--check",
            base_sha,
            "--",
        )
        return ChangeValidationResult(changed, tuple(sorted(binary)))

    @staticmethod
    def _read_regular_file(worktree: Path, path: str) -> bytes:
        descriptors: list[int] = []
        try:
            try:
                directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                nofollow = getattr(os, "O_NOFOLLOW", 0)
                descriptors.append(os.open(worktree, directory_flags))
                components = path.split("/")
                for component in components[:-1]:
                    descriptors.append(
                        os.open(
                            component,
                            directory_flags | nofollow,
                            dir_fd=descriptors[-1],
                        )
                    )
                descriptor = os.open(
                    components[-1], os.O_RDONLY | nofollow, dir_fd=descriptors[-1]
                )
                descriptors.append(descriptor)
            except OSError as exc:
                raise ChangeValidationError(f"changed file cannot be opened safely: {path}") from exc
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ChangeValidationError(f"changed path is not a regular file: {path}")
            if metadata.st_size > _MAX_NEW_FILE_BYTES:
                raise ChangeValidationError(f"changed file is too large: {path}")
            content = os.read(descriptor, _MAX_NEW_FILE_BYTES + 1)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        if len(content) > _MAX_NEW_FILE_BYTES:
            raise ChangeValidationError(f"changed file is too large: {path}")
        return content

    @staticmethod
    def _reject_symlink_components(worktree: Path, path: str) -> None:
        candidate = worktree
        for component in path.split("/"):
            candidate /= component
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                break
            except OSError as exc:
                raise ChangeValidationError(f"changed path cannot be inspected safely: {path}") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise ChangeValidationError(f"symbolic-link changes are not supported: {path}")

    def _check_untracked_whitespace(self, worktree: Path, path: str) -> None:
        result = self._run_raw(
            worktree,
            "diff",
            "--no-index",
            "--no-ext-diff",
            "--no-textconv",
            "--check",
            "--",
            os.devnull,
            path,
        )
        if result.returncode not in {0, 1}:
            raise ChangeValidationError(f"untracked file failed whitespace validation: {path}")

    @staticmethod
    def _nul_paths(value: str, *, numstat: bool = False) -> tuple[str, ...]:
        fields = tuple(field for field in value.split("\x00") if field)
        if numstat:
            paths: list[str] = []
            for field in fields:
                columns = field.split("\t", 2)
                if len(columns) != 3:
                    raise ChangeValidationError("git numstat returned malformed data")
                added, deleted, path = columns
                if added == "-" or deleted == "-":
                    paths.append(normalize_repo_path(path))
            return tuple(paths)
        try:
            return tuple(normalize_repo_path(field) for field in fields)
        except ValueError as exc:
            raise ChangeValidationError("git returned an unsafe changed path") from exc

    def _run(self, worktree: Path, *arguments: str) -> CommandResult:
        result = self._run_raw(worktree, *arguments)
        if result.returncode != 0:
            raise ChangeValidationError("git validation command failed")
        return result

    def _run_raw(self, worktree: Path, *arguments: str) -> CommandResult:
        result = run_command(
            (
                self._git_path,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "protocol.allow=never",
                "-c",
                "submodule.recurse=false",
                "-c",
                "diff.external=",
                "-c",
                "diff.trustExitCode=false",
                "-C",
                str(worktree),
                *arguments,
            ),
            timeout_seconds=self._timeout_seconds,
            env={"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
        )
        if (
            result.timed_out
            or result.error is not None
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise ChangeValidationError("git validation command did not complete safely")
        return result
