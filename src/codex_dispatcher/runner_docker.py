"""Fail-closed host boundary for per-WorkItem rootless Docker execution."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_dispatcher.executors.codex_docker import (
    ROOTLESS_HOST_PROXY_URL,
    DockerCodexRuntime,
)
from codex_dispatcher.runner_protocol import RunnerOperation, RunnerRequest
from codex_dispatcher.runner_workspace import RunnerWorkspacePaths
from codex_dispatcher.work_items import validate_session_id, validate_work_item_id


_SESSION_BINDING_VERSION = 1
_MAX_SESSION_BINDING_BYTES = 4096


class RunnerDockerError(RuntimeError):
    """Raised when the rootless container boundary cannot be proven safe."""


@dataclass(frozen=True, slots=True)
class DockerWorkItemContext:
    repository: Path
    state: Path
    codex_home: Path
    session_binding: Path
    image: str


def prepare_docker_work_item(
    *,
    runtime: DockerCodexRuntime,
    paths: RunnerWorkspacePaths,
    request: RunnerRequest,
    auth_file: Path,
    output_schema: Path,
) -> DockerWorkItemContext:
    """Validate exact host inputs and prepare one isolated session directory."""
    if request.operation not in {RunnerOperation.START, RunnerOperation.RESUME}:
        raise ValueError("request must start or resume a Docker Turn")
    validate_work_item_id(request.work_item_id)
    _validate_runtime(runtime)
    _owned_protected_directory(paths.root, "WorkItem root")
    _owned_protected_directory(paths.repository, "WorkItem repository")
    _owned_protected_directory(paths.state, "WorkItem state")
    if (
        paths.repository != paths.root / "repo"
        or paths.state != paths.root / "runner-state"
    ):
        raise RunnerDockerError("WorkItem directory layout is invalid")
    _trusted_regular_file(auth_file, "Codex auth file", secret=True)
    if auth_file.name != "auth.json":
        raise RunnerDockerError("Codex auth file name is invalid")
    _trusted_regular_file(output_schema, "output schema", secret=False)
    if output_schema.name != "agent-result.schema.json":
        raise RunnerDockerError("output schema name is invalid")

    codex_home = paths.state / "codex-home"
    session_binding = paths.state / "codex-session.json"
    if request.operation is RunnerOperation.START:
        if session_binding.exists() or session_binding.is_symlink():
            raise RunnerDockerError("Docker START cannot replace a bound Codex session")
        if codex_home.exists() or codex_home.is_symlink():
            _owned_protected_directory(codex_home, "WorkItem Codex home")
            try:
                if any(codex_home.iterdir()):
                    raise RunnerDockerError("unbound WorkItem Codex home is not empty")
            except OSError as exc:
                raise RunnerDockerError("WorkItem Codex home is unavailable") from exc
        else:
            try:
                codex_home.mkdir(mode=0o700)
            except OSError as exc:
                raise RunnerDockerError("WorkItem Codex home could not be created") from exc
    else:
        assert request.session_id is not None
        _owned_protected_directory(codex_home, "WorkItem Codex home")
        binding = _read_session_binding(session_binding)
        if binding != (request.work_item_id, request.session_id, runtime.image):
            raise RunnerDockerError("Docker RESUME session binding is unavailable")
    _owned_protected_directory(codex_home, "WorkItem Codex home")
    return DockerWorkItemContext(
        paths.repository,
        paths.state,
        codex_home,
        session_binding,
        runtime.image,
    )


def validate_docker_command_boundary(
    *,
    runtime: DockerCodexRuntime,
    context: DockerWorkItemContext,
    auth_file: Path,
    output_schema: Path,
) -> None:
    """Freshly validate every mutable input immediately before Docker starts."""
    _validate_runtime(runtime)
    _owned_protected_directory(context.repository.parent, "WorkItem root")
    _owned_protected_directory(context.repository, "WorkItem repository")
    _owned_protected_directory(context.state, "WorkItem state")
    _owned_protected_directory(context.codex_home, "WorkItem Codex home")
    if (
        context.state != context.repository.parent / "runner-state"
        or context.codex_home != context.state / "codex-home"
        or context.session_binding != context.state / "codex-session.json"
        or context.image != runtime.image
    ):
        raise RunnerDockerError("Docker WorkItem context is inconsistent")
    _trusted_regular_file(auth_file, "Codex auth file", secret=True)
    _trusted_regular_file(output_schema, "output schema", secret=False)


def bind_docker_session(
    context: DockerWorkItemContext,
    *,
    work_item_id: str,
    session_id: str,
) -> None:
    """Persist one immutable WorkItem-to-session identity outside the container mount."""
    work_item_id = validate_work_item_id(work_item_id)
    session_id = validate_session_id(session_id)
    _owned_protected_directory(context.state, "WorkItem state")
    _owned_protected_directory(context.codex_home, "WorkItem Codex home")
    existing = _read_session_binding(context.session_binding, required=False)
    if existing is not None:
        if existing != (work_item_id, session_id, context.image):
            raise RunnerDockerError("WorkItem Codex session binding conflicts")
        return
    payload = {
        "version": _SESSION_BINDING_VERSION,
        "work_item_id": work_item_id,
        "session_id": session_id,
        "image": context.image,
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    temporary = context.state / f".codex-session.{os.getpid()}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, context.session_binding)
        directory_descriptor = os.open(context.state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except FileExistsError:
        existing = _read_session_binding(context.session_binding, required=False)
        if existing != (work_item_id, session_id, context.image):
            raise RunnerDockerError("WorkItem Codex session binding conflicts")
    except OSError as exc:
        raise RunnerDockerError(
            "WorkItem Codex session binding could not be persisted"
        ) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _validate_runtime(runtime: DockerCodexRuntime) -> None:
    if not isinstance(runtime, DockerCodexRuntime):
        raise TypeError("runtime must be a DockerCodexRuntime")
    if runtime.egress_proxy_url != ROOTLESS_HOST_PROXY_URL:
        raise RunnerDockerError("Docker egress proxy is not the approved rootless endpoint")
    _trusted_executable(runtime.docker_path, "Docker executable")
    _owned_protected_directory(runtime.cli_config_directory, "Docker CLI config")
    try:
        if any(runtime.cli_config_directory.iterdir()):
            raise RunnerDockerError("Docker CLI config must remain empty")
    except OSError as exc:
        raise RunnerDockerError("Docker CLI config is unavailable") from exc
    socket_path = Path(runtime.docker_host.removeprefix("unix://"))
    expected_socket = _expected_rootless_socket()
    if socket_path != expected_socket:
        raise RunnerDockerError("Docker socket is not the exact rootless user socket")
    try:
        runtime_directory_stat = socket_path.parent.lstat()
        socket_stat = socket_path.lstat()
    except OSError as exc:
        raise RunnerDockerError("rootless Docker socket is unavailable") from exc
    if (
        not stat.S_ISDIR(runtime_directory_stat.st_mode)
        or socket_path.parent.is_symlink()
        or runtime_directory_stat.st_uid != os.geteuid()
        or runtime_directory_stat.st_mode & 0o077
    ):
        raise RunnerDockerError("rootless Docker runtime directory is not protected")
    if (
        not stat.S_ISSOCK(socket_stat.st_mode)
        or socket_path.is_symlink()
        or socket_stat.st_uid != os.geteuid()
        or socket_stat.st_gid != os.getegid()
        or socket_stat.st_mode & 0o007
    ):
        raise RunnerDockerError("rootless Docker socket identity is invalid")


def _expected_rootless_socket() -> Path:
    return Path("/run/user") / str(os.geteuid()) / "docker.sock"


def _read_session_binding(
    path: Path, *, required: bool = True
) -> tuple[str, str, str] | None:
    if not path.exists():
        if required:
            raise RunnerDockerError("WorkItem Codex session binding is unavailable")
        return None
    try:
        binding_stat = path.lstat()
        raw = path.read_bytes()
    except OSError as exc:
        raise RunnerDockerError("WorkItem Codex session binding is unavailable") from exc
    if (
        not stat.S_ISREG(binding_stat.st_mode)
        or path.is_symlink()
        or binding_stat.st_uid != os.geteuid()
        or binding_stat.st_mode & 0o077
        or not raw
        or len(raw) > _MAX_SESSION_BINDING_BYTES
        or b"\x00" in raw
    ):
        raise RunnerDockerError("WorkItem Codex session binding is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "work_item_id",
            "session_id",
            "image",
        }:
            raise ValueError("unexpected fields")
        if payload["version"] != _SESSION_BINDING_VERSION:
            raise ValueError("unsupported version")
        work_item_id = validate_work_item_id(payload["work_item_id"])
        session_id = validate_session_id(payload["session_id"])
        image = payload["image"]
        if not isinstance(image, str) or not image:
            raise ValueError("invalid image")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RunnerDockerError("WorkItem Codex session binding is malformed") from exc
    return work_item_id, session_id, image


def _owned_protected_directory(path: Path, field: str) -> None:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise RunnerDockerError(f"{field} is unavailable") from exc
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or path.is_symlink()
        or path_stat.st_uid != os.geteuid()
        or path_stat.st_mode & 0o077
    ):
        raise RunnerDockerError(f"{field} must be an owned protected directory")


def _trusted_regular_file(path: Path, field: str, *, secret: bool) -> None:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise RunnerDockerError(f"{field} is unavailable") from exc
    disallowed_mode = 0o077 if secret else 0o022
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path.is_symlink()
        or path_stat.st_uid not in {0, os.geteuid()}
        or path_stat.st_mode & disallowed_mode
    ):
        raise RunnerDockerError(f"{field} is not a trusted protected regular file")


def _trusted_executable(path: Path, field: str) -> None:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise RunnerDockerError(f"{field} is unavailable") from exc
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path.is_symlink()
        or path_stat.st_uid not in {0, os.geteuid()}
        or path_stat.st_mode & 0o022
        or not path_stat.st_mode & 0o111
    ):
        raise RunnerDockerError(f"{field} is not a trusted protected executable")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate session binding field")
        result[key] = value
    return result
