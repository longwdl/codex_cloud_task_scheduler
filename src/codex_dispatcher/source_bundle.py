"""Build a bounded, self-contained base bundle from a trusted local mirror."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from codex_dispatcher.command_runner import CommandResult, run_command
from codex_dispatcher.runner_protocol import MAX_INPUT_ARTIFACT_BYTES
from codex_dispatcher.work_items import validate_git_sha, validate_repository


SOURCE_BUNDLE_REF = "refs/heads/codex-runner-base"
_ADVERTISED_HEAD_RE = re.compile(r"([0-9a-f]{40,64}) ([^\n]+)")


class SourceBundleError(RuntimeError):
    """Raised when the exact base commit cannot be exported without ambiguity."""


@dataclass(frozen=True, slots=True)
class SourceBundle:
    artifact: bytes
    base_sha: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        validate_git_sha(self.base_sha, "base_sha")
        if not isinstance(self.artifact, bytes) or not self.artifact:
            raise ValueError("artifact must be non-empty bytes")
        if not 1 <= self.size_bytes <= MAX_INPUT_ARTIFACT_BYTES:
            raise ValueError("source bundle size is outside the protocol boundary")
        if self.size_bytes != len(self.artifact):
            raise ValueError("source bundle size does not match its artifact")
        if self.sha256 != sha256(self.artifact).hexdigest():
            raise ValueError("source bundle SHA-256 does not match its artifact")


class GitSourceBundleBuilder:
    """Copy one exact commit from a trusted mirror into an isolated bundle repository."""

    def __init__(
        self,
        *,
        git_path: str | Path,
        mirror_root: Path,
        temporary_root: Path,
        timeout_seconds: float = 120.0,
    ) -> None:
        executable = str(git_path)
        if not executable or not Path(executable).is_absolute():
            raise ValueError("git_path must be a non-empty absolute path")
        for path, field in (
            (mirror_root, "mirror_root"),
            (temporary_root, "temporary_root"),
        ):
            if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{field} must be a normalized absolute path")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._git_path = executable
        self._mirror_root = mirror_root
        self._temporary_root = temporary_root
        self._timeout_seconds = timeout_seconds

    def build(self, *, repository: str, base_sha: str) -> SourceBundle:
        repository = validate_repository(repository)
        base_sha = validate_git_sha(base_sha, "base_sha")
        owner, name = repository.split("/", 1)
        mirror = self._mirror_root / owner / f"{name}.git"
        self._require_trusted_mirror(mirror)
        self._prepare_temporary_root()

        resolved = self._run(
            mirror,
            "rev-parse",
            "--verify",
            f"{base_sha}^{{commit}}",
            stage="base_lookup",
        ).stdout.strip()
        if resolved != base_sha:
            raise SourceBundleError("local mirror did not resolve the exact persisted base SHA")

        with tempfile.TemporaryDirectory(
            prefix="source-", dir=self._temporary_root
        ) as temp_dir:
            staging = Path(temp_dir) / "objects.git"
            bundle_path = Path(temp_dir) / "source.bundle"
            self._run(None, "init", "--bare", str(staging), stage="init")
            self._run(
                staging,
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                str(mirror),
                f"{base_sha}:{SOURCE_BUNDLE_REF}",
                stage="local_import",
            )
            imported = self._run(
                staging,
                "rev-parse",
                "--verify",
                f"{SOURCE_BUNDLE_REF}^{{commit}}",
                stage="import_verify",
            ).stdout.strip()
            if imported != base_sha:
                raise SourceBundleError("isolated source repository changed the base SHA")
            self._run(
                staging,
                "fsck",
                "--strict",
                "--no-reflogs",
                "--no-progress",
                base_sha,
                stage="fsck",
            )
            self._run(
                staging,
                "bundle",
                "create",
                str(bundle_path),
                SOURCE_BUNDLE_REF,
                stage="bundle_create",
            )
            self._run(
                staging,
                "bundle",
                "verify",
                str(bundle_path),
                stage="bundle_verify",
            )
            advertised = self._run(
                staging,
                "bundle",
                "list-heads",
                str(bundle_path),
                stage="list_heads",
            ).stdout.strip()
            match = _ADVERTISED_HEAD_RE.fullmatch(advertised)
            if match is None or match.groups() != (base_sha, SOURCE_BUNDLE_REF):
                raise SourceBundleError("source bundle does not advertise only the exact base SHA")
            try:
                size_bytes = bundle_path.stat().st_size
            except OSError as exc:
                raise SourceBundleError("source bundle artifact is unavailable") from exc
            if not 1 <= size_bytes <= MAX_INPUT_ARTIFACT_BYTES:
                raise SourceBundleError("source bundle exceeds the transport size boundary")
            artifact = bundle_path.read_bytes()
            if len(artifact) != size_bytes:
                raise SourceBundleError("source bundle changed while it was being read")

        return SourceBundle(
            artifact=artifact,
            base_sha=base_sha,
            sha256=sha256(artifact).hexdigest(),
            size_bytes=size_bytes,
        )

    def _require_trusted_mirror(self, mirror: Path) -> None:
        try:
            resolved_root = self._mirror_root.resolve(strict=True)
            resolved_mirror = mirror.resolve(strict=True)
            resolved_mirror.relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise SourceBundleError(
                "trusted local mirror is missing or outside mirror_root"
            ) from exc
        if mirror.is_symlink() or not mirror.is_dir():
            raise SourceBundleError("trusted local mirror must be a non-symlink directory")

    def _prepare_temporary_root(self) -> None:
        self._temporary_root.mkdir(parents=True, exist_ok=True)
        if self._temporary_root.is_symlink() or not self._temporary_root.is_dir():
            raise SourceBundleError("temporary_root must be a non-symlink directory")

    def _run(
        self,
        repository: Path | None,
        *arguments: str,
        stage: str,
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
        argv = prefix + (("-C", str(repository)) if repository is not None else ()) + arguments
        result = run_command(
            argv,
            timeout_seconds=self._timeout_seconds,
            max_output_bytes=1024 * 1024,
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
            },
        )
        if (
            result.returncode != 0
            or result.timed_out
            or result.error is not None
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise SourceBundleError(f"Git source bundle stage failed: {stage}")
        return result
