"""Persistent Linux Runner work-item directories backed only by Git bundles."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from codex_dispatcher.command_runner import CommandResult, run_command
from codex_dispatcher.runner_disk import FusedWorkItemDisk, RunnerDiskError
from codex_dispatcher.runner_protocol import RunnerOperation, RunnerRequest
from codex_dispatcher.runner_transport import (
    MAX_ARTIFACT_BYTES,
    RunnerAck,
    RunnerExportReply,
    RunnerWireOutput,
)
from codex_dispatcher.source_bundle import SOURCE_BUNDLE_REF
from codex_dispatcher.work_items import validate_git_sha, validate_work_item_id


_METADATA_VERSION = 1
_MAX_METADATA_BYTES = 16 * 1024


class RunnerWorkspaceError(RuntimeError):
    """Raised when persistent Runner state cannot be proven consistent."""


@dataclass(frozen=True, slots=True)
class RunnerWorkspacePaths:
    root: Path
    repository: Path
    state: Path


@dataclass(frozen=True, slots=True)
class RunnerWorkspaceMetadata:
    work_item_id: str
    repository: str
    issue_number: int
    task_branch: str
    base_sha: str
    source_bundle_sha256: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": _METADATA_VERSION,
            "work_item_id": self.work_item_id,
            "repository": self.repository,
            "issue_number": self.issue_number,
            "task_branch": self.task_branch,
            "base_sha": self.base_sha,
            "source_bundle_sha256": self.source_bundle_sha256,
        }


class RunnerWorkspace:
    """Create, validate, and export one directory per WorkItem without a remote."""

    def __init__(
        self,
        *,
        git_path: str | Path,
        work_items_root: Path,
        timeout_seconds: float = 120.0,
        work_item_disk: FusedWorkItemDisk | None = None,
    ) -> None:
        executable = str(git_path)
        if not executable or not Path(executable).is_absolute():
            raise ValueError("git_path must be a non-empty absolute path")
        if (
            not isinstance(work_items_root, Path)
            or not work_items_root.is_absolute()
            or ".." in work_items_root.parts
        ):
            raise ValueError("work_items_root must be a normalized absolute path")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._git_path = executable
        self._root = work_items_root
        self._timeout_seconds = timeout_seconds
        self._work_item_disk = work_item_disk

    def prepare(self, request: RunnerRequest, source_bundle: bytes) -> RunnerAck:
        if request.operation is not RunnerOperation.PREPARE:
            raise ValueError("request must be a PREPARE operation")
        if not isinstance(source_bundle, bytes) or not source_bundle:
            raise ValueError("source_bundle must be non-empty bytes")
        if (
            len(source_bundle) != request.source_bundle_size
            or sha256(source_bundle).hexdigest() != request.source_bundle_sha256
        ):
            raise RunnerWorkspaceError("source bundle does not match the PREPARE request")
        assert request.repository is not None
        assert request.issue_number is not None
        assert request.task_branch is not None
        assert request.base_sha is not None
        metadata = RunnerWorkspaceMetadata(
            request.work_item_id,
            request.repository,
            request.issue_number,
            request.task_branch,
            request.base_sha,
            request.source_bundle_sha256,
        )
        paths = self._paths_for_metadata(metadata)
        self._prepare_root()
        if paths.root.is_symlink():
            raise RunnerWorkspaceError("WorkItem directory must not be a symbolic link")
        if self._work_item_disk is not None:
            return self._prepare_bounded(request, source_bundle, metadata, paths)
        existing = self._read_registry(request.work_item_id, required=False)
        if existing is not None:
            if existing != metadata:
                raise RunnerWorkspaceError("WorkItem registry identity conflicts with PREPARE")
            self._validate_prepared(paths, metadata, expected_head=metadata.base_sha)
            return RunnerAck(RunnerOperation.PREPARE, request.work_item_id)
        if paths.root.exists():
            recovered = self._read_metadata(paths.state / "workspace.json")
            if recovered != metadata:
                raise RunnerWorkspaceError("existing WorkItem directory identity conflicts")
            self._validate_prepared(paths, metadata, expected_head=metadata.base_sha)
            self._write_registry(metadata)
            return RunnerAck(RunnerOperation.PREPARE, request.work_item_id)

        staging_parent = self._root / ".staging"
        staging_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if staging_parent.is_symlink() or not staging_parent.is_dir():
            raise RunnerWorkspaceError("Runner staging directory is invalid")
        with tempfile.TemporaryDirectory(
            prefix=f"{request.work_item_id}-", dir=staging_parent
        ) as temp_dir:
            staging = Path(temp_dir) / "work-item"
            staging.mkdir(mode=0o700)
            staging_paths = RunnerWorkspacePaths(
                staging,
                staging / "repo",
                staging / "runner-state",
            )
            staging_paths.repository.mkdir(mode=0o700)
            staging_paths.state.mkdir(mode=0o700)
            bundle_path = staging_paths.state / "source.bundle"
            self._write_bytes(bundle_path, source_bundle)
            try:
                self._initialize_repository(staging_paths.repository, bundle_path, metadata)
                self._write_json(
                    staging_paths.state / "workspace.json", metadata.to_mapping()
                )
                bundle_path.unlink()
                paths.root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    os.rename(staging, paths.root)
                except OSError as exc:
                    raise RunnerWorkspaceError(
                        "WorkItem directory could not be installed atomically"
                    ) from exc
            finally:
                try:
                    bundle_path.unlink(missing_ok=True)
                except OSError:
                    pass
        self._validate_prepared(paths, metadata, expected_head=metadata.base_sha)
        self._write_registry(metadata)
        return RunnerAck(RunnerOperation.PREPARE, request.work_item_id)

    def paths(self, work_item_id: str) -> RunnerWorkspacePaths:
        metadata = self._read_registry(validate_work_item_id(work_item_id), required=True)
        assert metadata is not None
        paths = self._paths_for_metadata(metadata)
        self._ensure_bounded_mount(metadata.work_item_id, paths.root)
        self._validate_directory_identity(paths, metadata)
        return paths

    def metadata(self, work_item_id: str) -> RunnerWorkspaceMetadata:
        metadata = self._read_registry(validate_work_item_id(work_item_id), required=True)
        assert metadata is not None
        return metadata

    def validate_turn_anchor(self, work_item_id: str, input_head_sha: str) -> RunnerWorkspacePaths:
        input_head_sha = validate_git_sha(input_head_sha, "input_head_sha")
        metadata = self.metadata(work_item_id)
        paths = self._paths_for_metadata(metadata)
        self._ensure_bounded_mount(metadata.work_item_id, paths.root)
        self._validate_prepared(paths, metadata, expected_head=input_head_sha)
        return paths

    def current_head(self, work_item_id: str, *, require_clean: bool = True) -> str:
        metadata = self.metadata(work_item_id)
        paths = self._paths_for_metadata(metadata)
        self._ensure_bounded_mount(metadata.work_item_id, paths.root)
        self._validate_directory_identity(paths, metadata)
        branch = self._run(
            paths.repository,
            "symbolic-ref",
            "--short",
            "HEAD",
            stage="branch",
        ).stdout.strip()
        head = self._run(
            paths.repository,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
            stage="head",
        ).stdout.strip()
        if branch != metadata.task_branch:
            raise RunnerWorkspaceError("WorkItem repository is on an unexpected branch")
        try:
            validate_git_sha(head, "head")
        except ValueError as exc:
            raise RunnerWorkspaceError("Git returned an invalid WorkItem HEAD") from exc
        if require_clean and self._status(paths.repository):
            raise RunnerWorkspaceError("WorkItem repository contains uncommitted changes")
        return head

    def export(self, request: RunnerRequest) -> RunnerWireOutput:
        if request.operation is not RunnerOperation.EXPORT:
            raise ValueError("request must be an EXPORT operation")
        assert request.expected_head_sha is not None
        metadata = self.metadata(request.work_item_id)
        head = self.current_head(request.work_item_id, require_clean=True)
        if head != request.expected_head_sha:
            raise RunnerWorkspaceError("WorkItem HEAD conflicts with the export checkpoint")
        temporary = self._root / ".exports"
        temporary.mkdir(mode=0o700, parents=True, exist_ok=True)
        if temporary.is_symlink() or not temporary.is_dir():
            raise RunnerWorkspaceError("Runner export directory is invalid")
        with tempfile.TemporaryDirectory(prefix="export-", dir=temporary) as temp_dir:
            bundle_path = Path(temp_dir) / "result.bundle"
            reference = f"refs/heads/{metadata.task_branch}"
            self._run(
                self.paths(request.work_item_id).repository,
                "bundle",
                "create",
                str(bundle_path),
                reference,
                stage="bundle_create",
            )
            advertised = self._run(
                self.paths(request.work_item_id).repository,
                "bundle",
                "list-heads",
                str(bundle_path),
                stage="list_heads",
            ).stdout.strip()
            if advertised != f"{head} {reference}":
                raise RunnerWorkspaceError("result bundle advertises an unexpected reference")
            try:
                size_bytes = bundle_path.stat().st_size
            except OSError as exc:
                raise RunnerWorkspaceError("result bundle artifact is unavailable") from exc
            if not 1 <= size_bytes <= MAX_ARTIFACT_BYTES:
                raise RunnerWorkspaceError("result bundle exceeds the transport boundary")
            artifact = bundle_path.read_bytes()
            if len(artifact) != size_bytes:
                raise RunnerWorkspaceError("result bundle changed while it was being read")
        manifest = RunnerExportReply(
            work_item_id=request.work_item_id,
            head_sha=head,
            bundle_sha256=sha256(artifact).hexdigest(),
            size_bytes=len(artifact),
        )
        return RunnerWireOutput(manifest.to_json().encode("utf-8"), artifact)

    def _prepare_bounded(
        self,
        request: RunnerRequest,
        source_bundle: bytes,
        metadata: RunnerWorkspaceMetadata,
        paths: RunnerWorkspacePaths,
    ) -> RunnerAck:
        assert self._work_item_disk is not None
        existing = self._read_registry(request.work_item_id, required=False)
        if existing is not None:
            if existing != metadata:
                raise RunnerWorkspaceError("WorkItem registry identity conflicts with PREPARE")
            self._ensure_bounded_mount(request.work_item_id, paths.root)
            self._validate_prepared(paths, metadata, expected_head=metadata.base_sha)
            return RunnerAck(RunnerOperation.PREPARE, request.work_item_id)
        try:
            final_image_exists = self._work_item_disk.final_image_exists(
                request.work_item_id
            )
        except RunnerDiskError as exc:
            raise RunnerWorkspaceError("WorkItem disk state is ambiguous") from exc
        if final_image_exists:
            self._ensure_bounded_mount(request.work_item_id, paths.root)
            recovered = self._read_metadata(paths.state / "workspace.json")
            if recovered != metadata:
                raise RunnerWorkspaceError("existing WorkItem disk identity conflicts")
            self._validate_prepared(paths, metadata, expected_head=metadata.base_sha)
            self._write_registry(metadata)
            return RunnerAck(RunnerOperation.PREPARE, request.work_item_id)
        try:
            with self._work_item_disk.provision(
                request.work_item_id, paths.root
            ) as staging:
                staging_paths = RunnerWorkspacePaths(
                    staging,
                    staging / "repo",
                    staging / "runner-state",
                )
                staging_paths.repository.mkdir(mode=0o700)
                staging_paths.state.mkdir(mode=0o700)
                bundle_path = staging_paths.state / "source.bundle"
                self._write_bytes(bundle_path, source_bundle)
                try:
                    self._initialize_repository(
                        staging_paths.repository, bundle_path, metadata
                    )
                    self._write_json(
                        staging_paths.state / "workspace.json",
                        metadata.to_mapping(),
                    )
                    bundle_path.unlink()
                finally:
                    try:
                        bundle_path.unlink(missing_ok=True)
                    except OSError:
                        pass
        except RunnerDiskError as exc:
            raise RunnerWorkspaceError("WorkItem disk provisioning failed") from exc
        self._validate_prepared(paths, metadata, expected_head=metadata.base_sha)
        self._write_registry(metadata)
        return RunnerAck(RunnerOperation.PREPARE, request.work_item_id)

    def _ensure_bounded_mount(self, work_item_id: str, mountpoint: Path) -> None:
        if self._work_item_disk is None:
            return
        try:
            self._work_item_disk.ensure_mounted(work_item_id, mountpoint)
        except RunnerDiskError as exc:
            raise RunnerWorkspaceError("WorkItem disk boundary is unavailable") from exc

    def _initialize_repository(
        self,
        repository: Path,
        bundle_path: Path,
        metadata: RunnerWorkspaceMetadata,
    ) -> None:
        self._run(repository, "init", stage="init")
        self._run(repository, "bundle", "verify", str(bundle_path), stage="bundle_verify")
        advertised = self._run(
            repository,
            "bundle",
            "list-heads",
            str(bundle_path),
            stage="list_heads",
        ).stdout.strip()
        if advertised != f"{metadata.base_sha} {SOURCE_BUNDLE_REF}":
            raise RunnerWorkspaceError("source bundle advertises an unexpected reference")
        self._run(
            repository,
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            str(bundle_path),
            f"{metadata.base_sha}:refs/heads/{metadata.task_branch}",
            stage="bundle_import",
        )
        self._run(
            repository,
            "fsck",
            "--strict",
            "--no-reflogs",
            "--no-progress",
            metadata.base_sha,
            stage="fsck",
        )
        self._reject_git_behavior_files(repository, metadata.base_sha)
        self._run(
            repository,
            "checkout",
            "--force",
            "-B",
            metadata.task_branch,
            metadata.base_sha,
            "--",
            stage="checkout",
        )
        for key, value in (
            ("user.name", "Codex Runner"),
            ("user.email", "codex-runner@localhost"),
            ("core.hooksPath", "/dev/null"),
            ("submodule.recurse", "false"),
        ):
            self._run(repository, "config", "--local", key, value, stage="config")

    def _reject_git_behavior_files(self, repository: Path, base_sha: str) -> None:
        output = self._run(
            repository,
            "ls-tree",
            "-r",
            "--name-only",
            "-z",
            base_sha,
            stage="tree",
            max_output_bytes=4 * 1024 * 1024,
        ).stdout
        paths = tuple(path for path in output.split("\x00") if path)
        if "\ufffd" in output:
            raise RunnerWorkspaceError("source tree contains non-UTF-8 paths")
        if any(PurePosixPath(path).name == ".gitmodules" for path in paths):
            raise RunnerWorkspaceError("source tree contains unsupported submodules")
        attributes = tuple(
            path for path in paths if PurePosixPath(path).name == ".gitattributes"
        )
        for path in attributes:
            content = self._run(
                repository,
                "show",
                f"{base_sha}:{path}",
                stage="attributes",
                max_output_bytes=1024 * 1024,
            ).stdout
            for line in content.splitlines():
                rule = line.split("#", 1)[0].strip()
                if not rule:
                    continue
                tokens = rule.split()[1:]
                if any(
                    token.startswith(("filter=", "diff="))
                    or token == "working-tree-encoding"
                    for token in tokens
                ):
                    raise RunnerWorkspaceError(
                        "source tree requires an unsupported Git attribute driver"
                    )

    def _validate_prepared(
        self,
        paths: RunnerWorkspacePaths,
        metadata: RunnerWorkspaceMetadata,
        *,
        expected_head: str,
    ) -> None:
        self._validate_directory_identity(paths, metadata)
        if self._registry_path(metadata.work_item_id).exists():
            head = self.current_head(metadata.work_item_id, require_clean=True)
        else:
            head = self._current_head_without_registry(paths, metadata)
        if head != expected_head:
            raise RunnerWorkspaceError("WorkItem repository HEAD differs from its expected anchor")

    def _current_head_without_registry(
        self, paths: RunnerWorkspacePaths, metadata: RunnerWorkspaceMetadata
    ) -> str:
        branch = self._run(
            paths.repository, "symbolic-ref", "--short", "HEAD", stage="branch"
        ).stdout.strip()
        head = self._run(
            paths.repository, "rev-parse", "--verify", "HEAD^{commit}", stage="head"
        ).stdout.strip()
        if branch != metadata.task_branch or self._status(paths.repository):
            raise RunnerWorkspaceError("prepared WorkItem repository identity is invalid")
        return head

    def _validate_directory_identity(
        self, paths: RunnerWorkspacePaths, metadata: RunnerWorkspaceMetadata
    ) -> None:
        if (
            paths.root.is_symlink()
            or not paths.root.is_dir()
            or paths.repository.is_symlink()
            or not paths.repository.is_dir()
            or paths.state.is_symlink()
            or not paths.state.is_dir()
        ):
            raise RunnerWorkspaceError("WorkItem directory layout is invalid")
        stored = self._read_metadata(paths.state / "workspace.json")
        if stored != metadata:
            raise RunnerWorkspaceError("WorkItem directory metadata conflicts with registry")

    def _paths_for_metadata(
        self, metadata: RunnerWorkspaceMetadata
    ) -> RunnerWorkspacePaths:
        repository_key = metadata.repository.replace("/", "__")
        root = self._root / repository_key / f"issue-{metadata.issue_number}"
        try:
            root.resolve(strict=False).relative_to(self._root.resolve(strict=False))
        except ValueError as exc:
            raise RunnerWorkspaceError("WorkItem directory escapes work_items_root") from exc
        return RunnerWorkspacePaths(root, root / "repo", root / "runner-state")

    def _prepare_root(self) -> None:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._root.is_symlink() or not self._root.is_dir():
            raise RunnerWorkspaceError("work_items_root must be a non-symlink directory")
        registry = self._root / ".registry"
        registry.mkdir(mode=0o700, exist_ok=True)
        if registry.is_symlink() or not registry.is_dir():
            raise RunnerWorkspaceError("Runner registry directory is invalid")

    def _registry_path(self, work_item_id: str) -> Path:
        return self._root / ".registry" / f"{validate_work_item_id(work_item_id)}.json"

    def _write_registry(self, metadata: RunnerWorkspaceMetadata) -> None:
        self._prepare_root()
        self._write_json(self._registry_path(metadata.work_item_id), metadata.to_mapping())

    def _read_registry(
        self, work_item_id: str, *, required: bool
    ) -> RunnerWorkspaceMetadata | None:
        path = self._registry_path(work_item_id)
        if not path.exists():
            if required:
                raise RunnerWorkspaceError("WorkItem is not registered on this Runner")
            return None
        return self._read_metadata(path)

    def _read_metadata(self, path: Path) -> RunnerWorkspaceMetadata:
        try:
            metadata_stat = path.lstat()
            raw = path.read_bytes()
        except OSError as exc:
            raise RunnerWorkspaceError("WorkItem metadata is unavailable") from exc
        if (
            not stat.S_ISREG(metadata_stat.st_mode)
            or metadata_stat.st_mode & 0o022
            or not raw
            or len(raw) > _MAX_METADATA_BYTES
            or b"\x00" in raw
        ):
            raise RunnerWorkspaceError("WorkItem metadata exceeds its safe boundary")
        try:
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise RunnerWorkspaceError("WorkItem metadata is malformed") from exc
        expected = {
            "version",
            "work_item_id",
            "repository",
            "issue_number",
            "task_branch",
            "base_sha",
            "source_bundle_sha256",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise RunnerWorkspaceError("WorkItem metadata fields are invalid")
        try:
            request = RunnerRequest(
                RunnerOperation.PREPARE,
                payload["work_item_id"],
                repository=payload["repository"],
                issue_number=payload["issue_number"],
                task_branch=payload["task_branch"],
                base_sha=payload["base_sha"],
                source_bundle_sha256=payload["source_bundle_sha256"],
                source_bundle_size=1,
            )
        except (TypeError, ValueError) as exc:
            raise RunnerWorkspaceError("WorkItem metadata values are invalid") from exc
        if payload["version"] != _METADATA_VERSION:
            raise RunnerWorkspaceError("WorkItem metadata version is unsupported")
        return RunnerWorkspaceMetadata(
            request.work_item_id,
            request.repository or "",
            request.issue_number or 0,
            request.task_branch or "",
            request.base_sha or "",
            request.source_bundle_sha256 or "",
        )

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        encoded = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise RunnerWorkspaceError("WorkItem metadata could not be persisted") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _write_bytes(path: Path, content: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    def _status(self, repository: Path) -> str:
        return self._run(
            repository,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            stage="status",
            max_output_bytes=4 * 1024 * 1024,
        ).stdout

    def _run(
        self,
        repository: Path,
        *arguments: str,
        stage: str,
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
            "protocol.http.allow=never",
            "-c",
            "protocol.https.allow=never",
            "-c",
            "protocol.ssh.allow=never",
            "-c",
            "submodule.recurse=false",
            "-c",
            "diff.external=",
            "-c",
            "filter.lfs.required=false",
            "-c",
            "filter.lfs.smudge=",
            "-c",
            "filter.lfs.clean=",
        )
        argv = prefix + ("-C", str(repository)) + arguments
        result = run_command(
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
        if (
            result.returncode != 0
            or result.timed_out
            or result.error is not None
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise RunnerWorkspaceError(f"Git Runner workspace stage failed: {stage}")
        return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate metadata field")
        result[key] = value
    return result
