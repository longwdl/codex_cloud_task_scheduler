"""Fail-closed host boundary for per-WorkItem rootless Docker execution."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from codex_dispatcher.command_runner import run_command
from codex_dispatcher.executors.codex_docker import (
    CONTAINER_CODE_MODE_HOST_PATH,
    DOCKER_NETWORK,
    DOCKER_NETWORK_GATEWAY,
    DOCKER_NETWORK_SUBNET,
    ROOTLESS_HOST_PROXY_URL,
    DockerCodexRuntime,
)
from codex_dispatcher.runner_protocol import RunnerOperation, RunnerRequest
from codex_dispatcher.runner_workspace import RunnerWorkspacePaths
from codex_dispatcher.work_items import validate_session_id, validate_work_item_id


_SESSION_BINDING_VERSION = 1
_TOOL_BINDING_VERSION = 1
_MAX_SESSION_BINDING_BYTES = 4096


class RunnerDockerError(RuntimeError):
    """Raised when the rootless container boundary cannot be proven safe."""


@dataclass(frozen=True, slots=True)
class DockerWorkItemContext:
    repository: Path
    state: Path
    codex_home: Path
    session_binding: Path
    tool_binding: Path
    image: str
    codex_sha256: str
    code_mode_host_sha256: str


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
    tool_binding = paths.state / "codex-session-tools.json"
    if request.operation is RunnerOperation.START:
        if (
            session_binding.exists()
            or session_binding.is_symlink()
            or tool_binding.exists()
            or tool_binding.is_symlink()
        ):
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
        if binding != (
            request.work_item_id,
            request.session_id,
            runtime.image,
            runtime.codex_sha256,
        ):
            raise RunnerDockerError("Docker RESUME session binding is unavailable")
        tool_binding_value = _read_tool_binding(tool_binding, required=False)
        expected_tool_binding = (
            request.work_item_id,
            request.session_id,
            runtime.image,
            runtime.codex_sha256,
            runtime.code_mode_host_sha256,
        )
        if (
            tool_binding_value is not None
            and tool_binding_value != expected_tool_binding
        ):
            raise RunnerDockerError("Docker RESUME session binding is unavailable")
    _owned_protected_directory(codex_home, "WorkItem Codex home")
    return DockerWorkItemContext(
        paths.repository,
        paths.state,
        codex_home,
        session_binding,
        tool_binding,
        runtime.image,
        runtime.codex_sha256,
        runtime.code_mode_host_sha256,
    )


def validate_docker_command_boundary(
    *,
    runtime: DockerCodexRuntime,
    context: DockerWorkItemContext,
    codex_path: Path,
    auth_file: Path,
    output_schema: Path,
) -> None:
    """Freshly validate every mutable input immediately before Docker starts."""
    _validate_runtime(runtime)
    _validate_docker_assets(runtime)
    _owned_protected_directory(context.repository.parent, "WorkItem root")
    _owned_protected_directory(context.repository, "WorkItem repository")
    _owned_protected_directory(context.state, "WorkItem state")
    _owned_protected_directory(context.codex_home, "WorkItem Codex home")
    if (
        context.state != context.repository.parent / "runner-state"
        or context.codex_home != context.state / "codex-home"
        or context.session_binding != context.state / "codex-session.json"
        or context.tool_binding != context.state / "codex-session-tools.json"
        or context.image != runtime.image
        or context.codex_sha256 != runtime.codex_sha256
        or context.code_mode_host_sha256 != runtime.code_mode_host_sha256
        or runtime.code_mode_host_path
        != codex_path.with_name(CONTAINER_CODE_MODE_HOST_PATH.name)
    ):
        raise RunnerDockerError("Docker WorkItem context is inconsistent")
    _validate_codex_binary(codex_path, runtime.codex_sha256, "Codex executable")
    _validate_codex_binary(
        runtime.code_mode_host_path,
        runtime.code_mode_host_sha256,
        "Codex code-mode host",
    )
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
    expected_session_binding = (
        work_item_id,
        session_id,
        context.image,
        context.codex_sha256,
    )
    existing_session_binding = _read_session_binding(
        context.session_binding, required=False
    )
    if existing_session_binding is None:
        _persist_binding(
            context.session_binding,
            {
                "version": _SESSION_BINDING_VERSION,
                "work_item_id": work_item_id,
                "session_id": session_id,
                "image": context.image,
                "codex_sha256": context.codex_sha256,
            },
            "WorkItem Codex session binding",
        )
        existing_session_binding = _read_session_binding(context.session_binding)
    if existing_session_binding != expected_session_binding:
        raise RunnerDockerError("WorkItem Codex session binding conflicts")

    expected_tool_binding = (
        *expected_session_binding,
        context.code_mode_host_sha256,
    )
    existing_tool_binding = _read_tool_binding(
        context.tool_binding, required=False
    )
    if existing_tool_binding is None:
        _persist_binding(
            context.tool_binding,
            {
                "version": _TOOL_BINDING_VERSION,
                "work_item_id": work_item_id,
                "session_id": session_id,
                "image": context.image,
                "codex_sha256": context.codex_sha256,
                "code_mode_host_sha256": context.code_mode_host_sha256,
            },
            "WorkItem Codex tool binding",
        )
        existing_tool_binding = _read_tool_binding(context.tool_binding)
    if existing_tool_binding != expected_tool_binding:
        raise RunnerDockerError("WorkItem Codex tool binding conflicts")


def _persist_binding(path: Path, payload: dict[str, object], field: str) -> None:
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise RunnerDockerError(f"{field} could not be persisted") from exc
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
) -> tuple[str, str, str, str] | None:
    payload = _read_binding_payload(
        path, required=required, field="WorkItem Codex session binding"
    )
    if payload is None:
        return None
    try:
        if set(payload) != {
            "version",
            "work_item_id",
            "session_id",
            "image",
            "codex_sha256",
        } or payload["version"] != _SESSION_BINDING_VERSION:
            raise ValueError("unexpected fields")
        common = _parse_binding_common(payload)
    except (TypeError, ValueError) as exc:
        raise RunnerDockerError(
            "WorkItem Codex session binding is malformed"
        ) from exc
    return common


def _read_tool_binding(
    path: Path, *, required: bool = True
) -> tuple[str, str, str, str, str] | None:
    payload = _read_binding_payload(
        path, required=required, field="WorkItem Codex tool binding"
    )
    if payload is None:
        return None
    try:
        if set(payload) != {
            "version",
            "work_item_id",
            "session_id",
            "image",
            "codex_sha256",
            "code_mode_host_sha256",
        } or payload["version"] != _TOOL_BINDING_VERSION:
            raise ValueError("unexpected fields")
        common = _parse_binding_common(payload)
        code_mode_host_sha256 = _validate_binding_sha256(
            payload["code_mode_host_sha256"], "code-mode host"
        )
    except (TypeError, ValueError) as exc:
        raise RunnerDockerError("WorkItem Codex tool binding is malformed") from exc
    return (*common, code_mode_host_sha256)


def _read_binding_payload(
    path: Path, *, required: bool, field: str
) -> dict[str, Any] | None:
    if not path.exists():
        if required:
            raise RunnerDockerError(f"{field} is unavailable")
        return None
    try:
        binding_stat = path.lstat()
        raw = path.read_bytes()
    except OSError as exc:
        raise RunnerDockerError(f"{field} is unavailable") from exc
    if (
        not stat.S_ISREG(binding_stat.st_mode)
        or path.is_symlink()
        or binding_stat.st_uid != os.geteuid()
        or binding_stat.st_mode & 0o077
        or not raw
        or len(raw) > _MAX_SESSION_BINDING_BYTES
        or b"\x00" in raw
    ):
        raise RunnerDockerError(f"{field} is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(payload, dict):
            raise ValueError("binding is not an object")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RunnerDockerError(f"{field} is malformed") from exc
    return payload


def _parse_binding_common(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    work_item_id = validate_work_item_id(payload["work_item_id"])
    session_id = validate_session_id(payload["session_id"])
    image = payload["image"]
    if not isinstance(image, str) or not image:
        raise ValueError("invalid image")
    codex_sha256 = _validate_binding_sha256(payload["codex_sha256"], "Codex")
    return work_item_id, session_id, image, codex_sha256


def _validate_binding_sha256(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"invalid {field} digest")
    return value


def _validate_codex_binary(
    path: Path, expected_sha256: str, field: str
) -> None:
    _trusted_executable(path, field)
    digest = sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise RunnerDockerError(f"{field} could not be hashed") from exc
    if digest.hexdigest() != expected_sha256:
        raise RunnerDockerError(f"{field} digest is invalid")


def _validate_docker_assets(runtime: DockerCodexRuntime) -> None:
    image = _docker_inspect_json(
        runtime,
        "image",
        "inspect",
        runtime.image,
        "--format={{json .}}",
    )
    repo_digests = image.get("RepoDigests")
    if (
        image.get("Os") != "linux"
        or image.get("Architecture") != "amd64"
        or not isinstance(repo_digests, list)
        or repo_digests != [runtime.image]
    ):
        raise RunnerDockerError("Docker image identity is invalid")

    network = _docker_inspect_json(
        runtime,
        "network",
        "inspect",
        DOCKER_NETWORK,
        "--format={{json .}}",
    )
    ipam = network.get("IPAM")
    configs = ipam.get("Config") if isinstance(ipam, dict) else None
    config = configs[0] if isinstance(configs, list) and len(configs) == 1 else None
    options = network.get("Options")
    if (
        network.get("Name") != DOCKER_NETWORK
        or network.get("Driver") != "bridge"
        or network.get("Scope") != "local"
        or network.get("Internal") is not False
        or network.get("Attachable") is not False
        or network.get("Ingress") is not False
        or network.get("EnableIPv6") is not False
        or network.get("Containers") != {}
        or not isinstance(ipam, dict)
        or ipam.get("Driver") != "default"
        or not isinstance(config, dict)
        or config.get("Subnet") != DOCKER_NETWORK_SUBNET
        or config.get("Gateway") != DOCKER_NETWORK_GATEWAY
        or options
        != {
            "com.docker.network.bridge.enable_icc": "false",
            "com.docker.network.bridge.enable_ip_masquerade": "true",
        }
    ):
        raise RunnerDockerError("Docker egress network identity is invalid")


def _docker_inspect_json(
    runtime: DockerCodexRuntime,
    *arguments: str,
) -> dict[str, Any]:
    result = run_command(
        (
            str(runtime.docker_path),
            f"--host={runtime.docker_host}",
            *arguments,
        ),
        timeout_seconds=10.0,
        max_output_bytes=64 * 1024,
        env={"DOCKER_CONFIG": str(runtime.cli_config_directory)},
    )
    if (
        result.returncode != 0
        or result.timed_out
        or result.error is not None
        or result.stdout_truncated
        or result.stderr_truncated
    ):
        raise RunnerDockerError("Docker asset inspection failed")
    try:
        payload = json.loads(result.stdout, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RunnerDockerError("Docker asset inspection output is malformed") from exc
    if not isinstance(payload, dict):
        raise RunnerDockerError("Docker asset inspection output is malformed")
    return payload


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
