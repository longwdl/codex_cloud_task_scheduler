"""Local quarantine verification for self-contained Git bundles from an untrusted Runner."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from codex_dispatcher.command_runner import CommandResult, run_command
from codex_dispatcher.publisher import (
    MAX_CHANGED_PATHS,
    MAX_COMMITS_PER_PUBLISH,
    VerifiedBundle,
)
from codex_dispatcher.runner_transport import RunnerExportReply
from codex_dispatcher.task_spec import normalize_repo_path
from codex_dispatcher.work_items import WorkItem


MAX_CHANGED_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_CHANGED_FILE_BYTES = 50 * 1024 * 1024
_SHA_RE = re.compile(r"[0-9a-f]{40,64}")
_LIKELY_SECRET_PATTERNS = (
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"gh[pousr]_[A-Za-z0-9]{36,}",
    r"AKIA[A-Z0-9]{16}",
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
)


class GitBundleVerificationError(RuntimeError):
    """Raised when a Runner bundle cannot be mechanically proven safe to publish."""


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    mode: str
    object_type: str
    path: str


class GitBundleQuarantineVerifier:
    """Import one bundle into a fresh bare repository and inspect only fixed Git queries."""

    def __init__(
        self,
        *,
        git_path: str | Path,
        quarantine_root: Path,
        timeout_seconds: float = 60.0,
    ) -> None:
        executable = str(git_path)
        if not executable or not Path(executable).is_absolute():
            raise ValueError("git_path must be a non-empty absolute path")
        if not quarantine_root.is_absolute():
            raise ValueError("quarantine_root must be an absolute path")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._git_path = executable
        self._quarantine_root = quarantine_root
        self._timeout_seconds = timeout_seconds

    def verify(
        self,
        artifact: bytes,
        *,
        manifest: RunnerExportReply,
        work_item: WorkItem,
    ) -> VerifiedBundle:
        if not isinstance(manifest, RunnerExportReply):
            raise TypeError("manifest must be a RunnerExportReply")
        if not isinstance(work_item, WorkItem):
            raise TypeError("work_item must be a WorkItem")
        artifact = manifest.validate_artifact(artifact)
        if manifest.work_item_id != work_item.work_item_id:
            raise GitBundleVerificationError("bundle belongs to a different WorkItem")
        anchor = work_item.last_published_sha or work_item.base_sha
        if manifest.head_sha == anchor:
            raise GitBundleVerificationError("bundle HEAD does not advance the publication anchor")

        self._quarantine_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="verify-", dir=self._quarantine_root
        ) as temp_dir:
            quarantine = Path(temp_dir)
            bundle_path = quarantine / "runner.bundle"
            repository = quarantine / "objects.git"
            bundle_path.write_bytes(artifact)
            self._run(None, "init", "--bare", str(repository), stage="init")
            self._run(repository, "bundle", "verify", str(bundle_path), stage="bundle_verify")
            heads = self._bundle_heads(repository, bundle_path)
            if manifest.head_sha not in {object_id for object_id, _ in heads}:
                raise GitBundleVerificationError("expected bundle HEAD is not advertised")
            quarantine_ref = "refs/quarantine/verified-head"
            self._run(
                repository,
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                str(bundle_path),
                f"{manifest.head_sha}:{quarantine_ref}",
                stage="bundle_import",
            )
            imported_head = self._run(
                repository,
                "rev-parse",
                "--verify",
                f"{quarantine_ref}^{{commit}}",
                stage="head_verify",
            ).stdout.strip()
            if imported_head != manifest.head_sha:
                raise GitBundleVerificationError("imported bundle HEAD does not match manifest")
            self._run(repository, "cat-file", "-e", f"{anchor}^{{commit}}", stage="anchor")
            self._run(
                repository,
                "fsck",
                "--strict",
                "--no-reflogs",
                "--no-progress",
                manifest.head_sha,
                anchor,
                stage="fsck",
            )
            ancestor = self._execute(
                repository,
                "merge-base",
                "--is-ancestor",
                anchor,
                manifest.head_sha,
            )
            if ancestor.returncode != 0 or ancestor.timed_out or ancestor.error is not None:
                raise GitBundleVerificationError("bundle HEAD is not a descendant of its anchor")
            commit_count_text = self._run(
                repository,
                "rev-list",
                "--count",
                f"{anchor}..{manifest.head_sha}",
                stage="commit_count",
            ).stdout.strip()
            try:
                commit_count = int(commit_count_text)
            except ValueError as exc:
                raise GitBundleVerificationError("Git returned an invalid commit count") from exc
            if not 1 <= commit_count <= MAX_COMMITS_PER_PUBLISH:
                raise GitBundleVerificationError("bundle commit count exceeds publication policy")
            merges = self._run(
                repository,
                "rev-list",
                "--min-parents=2",
                f"{anchor}..{manifest.head_sha}",
                stage="merge_check",
            ).stdout.strip()
            if merges:
                raise GitBundleVerificationError("merge commits are not accepted from the Runner")
            self._run(
                repository,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--check",
                anchor,
                manifest.head_sha,
                "--",
                stage="diff_check",
            )
            changed_paths = self._changed_paths(repository, anchor, manifest.head_sha)
            self._inspect_changed_files(repository, manifest.head_sha, changed_paths)

        return VerifiedBundle(
            bundle_sha256=sha256(artifact).hexdigest(),
            head_sha=manifest.head_sha,
            parent_anchor_sha=anchor,
            changed_paths=changed_paths,
            commit_count=commit_count,
            size_bytes=len(artifact),
        )

    def _bundle_heads(self, repository: Path, bundle_path: Path) -> tuple[tuple[str, str], ...]:
        output = self._run(
            repository,
            "bundle",
            "list-heads",
            str(bundle_path),
            stage="list_heads",
        ).stdout
        heads: list[tuple[str, str]] = []
        for line in output.splitlines():
            fields = line.split(" ", 1)
            if len(fields) != 2 or _SHA_RE.fullmatch(fields[0]) is None or not fields[1]:
                raise GitBundleVerificationError("bundle advertised an invalid reference")
            heads.append((fields[0], fields[1]))
        if not heads:
            raise GitBundleVerificationError("bundle does not advertise any references")
        return tuple(heads)

    def _changed_paths(
        self, repository: Path, anchor: str, head_sha: str
    ) -> tuple[str, ...]:
        output = self._run(
            repository,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            anchor,
            head_sha,
            "--",
            stage="changed_paths",
            max_output_bytes=4 * 1024 * 1024,
        ).stdout
        raw_paths = tuple(path for path in output.split("\x00") if path)
        if not raw_paths or len(raw_paths) > MAX_CHANGED_PATHS:
            raise GitBundleVerificationError("bundle changed path count is invalid")
        if any("\ufffd" in path for path in raw_paths):
            raise GitBundleVerificationError("bundle contains a non-UTF-8 path")
        try:
            paths = tuple(normalize_repo_path(path) for path in raw_paths)
        except (TypeError, ValueError) as exc:
            raise GitBundleVerificationError("bundle contains an unsafe changed path") from exc
        if len(set(paths)) != len(paths):
            raise GitBundleVerificationError("bundle changed paths are not unique")
        if any(
            path.rsplit("/", 1)[-1] in {".gitattributes", ".gitmodules"}
            for path in paths
        ):
            raise GitBundleVerificationError("bundle changes Git behavior files")
        return tuple(sorted(paths))

    def _inspect_changed_files(
        self, repository: Path, head_sha: str, changed_paths: tuple[str, ...]
    ) -> None:
        total_bytes = 0
        for path in changed_paths:
            entry = self._tree_entry(repository, head_sha, path)
            if entry is None:
                continue
            if entry.mode not in {"100644", "100755"} or entry.object_type != "blob":
                raise GitBundleVerificationError("bundle contains a non-regular changed path")
            size_text = self._run(
                repository,
                "cat-file",
                "-s",
                f"{head_sha}:{path}",
                stage="blob_size",
            ).stdout.strip()
            try:
                size = int(size_text)
            except ValueError as exc:
                raise GitBundleVerificationError("Git returned an invalid blob size") from exc
            if size < 0 or size > MAX_CHANGED_FILE_BYTES:
                raise GitBundleVerificationError("changed file exceeds publication size policy")
            total_bytes += size
            if total_bytes > MAX_TOTAL_CHANGED_FILE_BYTES:
                raise GitBundleVerificationError(
                    "changed files exceed total publication size policy"
                )
            content = self._run(
                repository,
                "cat-file",
                "blob",
                f"{head_sha}:{path}",
                stage="blob_read",
                max_output_bytes=MAX_CHANGED_FILE_BYTES + 1,
            ).stdout
            if "\x00" in content or "\ufffd" in content:
                raise GitBundleVerificationError("binary or non-UTF-8 changes are not supported")
            if self._contains_likely_secret(repository, head_sha, path):
                raise GitBundleVerificationError("changed file contains a likely credential")

    def _contains_likely_secret(self, repository: Path, head_sha: str, path: str) -> bool:
        arguments = ["grep", "-l", "-E"]
        for pattern in _LIKELY_SECRET_PATTERNS:
            arguments.extend(("-e", pattern))
        arguments.extend((head_sha, "--", path))
        result = self._execute(repository, *arguments, max_output_bytes=4096)
        if (
            result.timed_out
            or result.error is not None
            or result.stdout_truncated
            or result.stderr_truncated
            or result.returncode not in {0, 1}
        ):
            raise GitBundleVerificationError("Git quarantine credential scan failed")
        return result.returncode == 0

    def _tree_entry(self, repository: Path, head_sha: str, path: str) -> _TreeEntry | None:
        output = self._run(
            repository,
            "ls-tree",
            "-z",
            head_sha,
            "--",
            path,
            stage="tree_entry",
        ).stdout
        entries = tuple(entry for entry in output.split("\x00") if entry)
        if not entries:
            return None
        if len(entries) != 1 or "\t" not in entries[0]:
            raise GitBundleVerificationError("Git returned an ambiguous tree entry")
        metadata, returned_path = entries[0].split("\t", 1)
        fields = metadata.split(" ")
        if len(fields) != 3 or returned_path != path:
            raise GitBundleVerificationError("Git returned an invalid tree entry")
        return _TreeEntry(fields[0], fields[1], returned_path)

    def _run(
        self,
        repository: Path | None,
        *arguments: str,
        stage: str,
        max_output_bytes: int = 1024 * 1024,
    ) -> CommandResult:
        result = self._execute(
            repository,
            *arguments,
            max_output_bytes=max_output_bytes,
        )
        if (
            result.returncode != 0
            or result.timed_out
            or result.error is not None
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise GitBundleVerificationError(f"Git quarantine stage failed: {stage}")
        return result

    def _execute(
        self,
        repository: Path | None,
        *arguments: str,
        max_output_bytes: int = 1024 * 1024,
    ) -> CommandResult:
        prefix = (
            self._git_path,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.file.allow=always",
            "-c",
            "submodule.recurse=false",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "diff.external=",
            "-c",
            "filter.lfs.required=false",
            "-c",
            "filter.lfs.smudge=",
            "-c",
            "filter.lfs.clean=",
        )
        argv = prefix + ("-C", str(repository)) + arguments if repository else prefix + arguments
        return run_command(
            argv,
            timeout_seconds=self._timeout_seconds,
            max_output_bytes=max_output_bytes,
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
            },
        )
