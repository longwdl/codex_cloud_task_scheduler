"""Fail-closed host boundary for per-WorkItem rootless Docker execution."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any

from codex_dispatcher.command_runner import run_command
from codex_dispatcher.executors.codex_docker import (
    CONTAINER_CODE_MODE_HOST_PATH,
    DOCKER_NETWORK,
    DOCKER_LABEL_POLICY_DIGEST,
    DOCKER_LABEL_SESSION_GENERATION,
    DOCKER_LABEL_SESSION_GENERATION_ID,
    DOCKER_LABEL_TURN,
    DOCKER_LABEL_WORK_ITEM,
    DOCKER_NETWORK_GATEWAY,
    DOCKER_NETWORK_SUBNET,
    ROOTLESS_HOST_PROXY_URL,
    DockerCodexRuntime,
)
from codex_dispatcher.runner_protocol import (
    NEXT_PROTOCOL_VERSION,
    RunnerOperation,
    RunnerRequest,
)
from codex_dispatcher.runner_workspace import RunnerWorkspacePaths
from codex_dispatcher.work_items import (
    validate_session_generation_id,
    validate_session_id,
    validate_work_item_id,
)


_SESSION_BINDING_VERSION = 1
_TOOL_BINDING_VERSION = 1
_AUTH_BINDING_VERSION = 1
_GENERATION_RECORD_VERSION = 1
_MAX_SESSION_BINDING_BYTES = 4096
_MAX_AUTH_BYTES = 1024 * 1024
_PINNED_IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")


class RunnerDockerError(RuntimeError):
    """Raised when the rootless container boundary cannot be proven safe."""


class DockerGenerationContainerState(StrEnum):
    """Exact state of one identity-validated v2 Turn container."""

    RUNNING = "running"
    STOPPED = "stopped"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class DockerWorkItemContext:
    work_item_id: str
    repository: Path
    state: Path
    codex_home: Path
    auth_file: Path
    auth_binding: Path
    session_binding: Path
    tool_binding: Path
    image: str
    codex_sha256: str
    code_mode_host_sha256: str
    session_generation_id: str | None = None
    session_generation: int | None = None
    agent_policy_digest: str | None = None
    generation_record: Path | None = None


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
    if auth_file.name != "auth.json":
        raise RunnerDockerError("Codex auth file name is invalid")
    _trusted_regular_file(output_schema, "output schema", secret=False)
    if output_schema.name != "agent-result.schema.json":
        raise RunnerDockerError("output schema name is invalid")

    if request.version == NEXT_PROTOCOL_VERSION:
        return _prepare_docker_generation(
            runtime=runtime,
            paths=paths,
            request=request,
            auth_file=auth_file,
        )

    codex_home = paths.state / "codex-home"
    work_item_auth = codex_home / "auth.json"
    auth_binding = paths.state / "codex-auth-binding.json"
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
        auth_exists = work_item_auth.exists() or work_item_auth.is_symlink()
        binding_exists = auth_binding.exists() or auth_binding.is_symlink()
        if auth_exists != binding_exists:
            raise RunnerDockerError("Docker START auth binding is incomplete")
        if codex_home.exists() or codex_home.is_symlink():
            _owned_protected_directory(codex_home, "WorkItem Codex home")
            try:
                entries = tuple(codex_home.iterdir())
            except OSError as exc:
                raise RunnerDockerError("WorkItem Codex home is unavailable") from exc
            if auth_exists:
                if entries != (work_item_auth,):
                    raise RunnerDockerError(
                        "unbound WorkItem Codex home has ambiguous state"
                    )
                _validate_bound_auth(
                    work_item_auth,
                    auth_binding,
                    request.work_item_id,
                )
            elif entries:
                raise RunnerDockerError("unbound WorkItem Codex home is not empty")
        else:
            if auth_exists or binding_exists:
                raise RunnerDockerError("Docker START auth binding is inconsistent")
            try:
                codex_home.mkdir(mode=0o700)
            except OSError as exc:
                raise RunnerDockerError("WorkItem Codex home could not be created") from exc
        if not auth_exists:
            _seed_work_item_auth(
                state=paths.state,
                source=auth_file,
                destination=work_item_auth,
                binding=auth_binding,
                work_item_id=request.work_item_id,
            )
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
        auth_exists = work_item_auth.exists() or work_item_auth.is_symlink()
        binding_exists = auth_binding.exists() or auth_binding.is_symlink()
        if auth_exists != binding_exists:
            raise RunnerDockerError("Docker RESUME auth binding is incomplete")
        if not auth_exists:
            _seed_work_item_auth(
                state=paths.state,
                source=auth_file,
                destination=work_item_auth,
                binding=auth_binding,
                work_item_id=request.work_item_id,
            )
        else:
            _validate_bound_auth(
                work_item_auth,
                auth_binding,
                request.work_item_id,
            )
    _owned_protected_directory(codex_home, "WorkItem Codex home")
    return DockerWorkItemContext(
        request.work_item_id,
        paths.repository,
        paths.state,
        codex_home,
        work_item_auth,
        auth_binding,
        session_binding,
        tool_binding,
        runtime.image,
        runtime.codex_sha256,
        runtime.code_mode_host_sha256,
    )


def _prepare_docker_generation(
    *,
    runtime: DockerCodexRuntime,
    paths: RunnerWorkspacePaths,
    request: RunnerRequest,
    auth_file: Path,
) -> DockerWorkItemContext:
    assert request.session_generation_id is not None
    assert request.session_generation is not None
    assert request.agent_policy_digest is not None
    create_root = request.operation is RunnerOperation.START
    generation_root, records = _generation_records(
        paths.state,
        create_root=create_root,
    )
    if any(record[0] != request.work_item_id for record in records.values()):
        raise RunnerDockerError("Docker generation WorkItem identity conflicts")
    highest = max((record[2] for record in records.values()), default=0)
    generation_directory = generation_root / request.session_generation_id
    expected_record = (
        request.work_item_id,
        request.session_generation_id,
        request.session_generation,
        request.agent_policy_digest,
        runtime.image,
        runtime.codex_sha256,
        runtime.code_mode_host_sha256,
    )
    if request.operation is RunnerOperation.START:
        if request.session_generation <= highest:
            raise RunnerDockerError(
                "Docker START generation must advance persisted state"
            )
        if generation_directory.exists() or generation_directory.is_symlink():
            raise RunnerDockerError("Docker START generation already exists")
        _create_protected_directory(
            generation_directory,
            parent=generation_root,
            field="Docker generation directory",
        )
        generation_record = generation_directory / "generation.json"
        _persist_binding(
            generation_record,
            {
                "version": _GENERATION_RECORD_VERSION,
                "work_item_id": request.work_item_id,
                "session_generation_id": request.session_generation_id,
                "session_generation": request.session_generation,
                "agent_policy_digest": request.agent_policy_digest,
                "image": runtime.image,
                "codex_sha256": runtime.codex_sha256,
                "code_mode_host_sha256": runtime.code_mode_host_sha256,
            },
            "Docker generation record",
        )
        if _read_generation_record(generation_record) != expected_record:
            raise RunnerDockerError("Docker generation record conflicts")
    else:
        if request.session_generation != highest:
            raise RunnerDockerError(
                "Docker RESUME requires the latest persisted generation"
            )
        observed = records.get(request.session_generation_id)
        if observed != expected_record:
            raise RunnerDockerError("Docker RESUME generation binding is unavailable")
        generation_record = generation_directory / "generation.json"

    codex_home = generation_directory / "codex-home"
    work_item_auth = codex_home / "auth.json"
    auth_binding = generation_directory / "codex-auth-binding.json"
    session_binding = generation_directory / "codex-session.json"
    tool_binding = generation_directory / "codex-session-tools.json"
    if request.operation is RunnerOperation.START:
        _create_protected_directory(
            codex_home,
            parent=generation_directory,
            field="generation Codex home",
        )
        _seed_work_item_auth(
            state=generation_directory,
            source=auth_file,
            destination=work_item_auth,
            binding=auth_binding,
            work_item_id=request.work_item_id,
        )
    else:
        assert request.session_id is not None
        _owned_protected_directory(codex_home, "generation Codex home")
        _validate_bound_auth(
            work_item_auth,
            auth_binding,
            request.work_item_id,
        )
        session = _read_session_binding(session_binding)
        tools = _read_tool_binding(tool_binding)
        expected_session = (
            request.work_item_id,
            request.session_id,
            runtime.image,
            runtime.codex_sha256,
        )
        if session != expected_session or tools != (
            *expected_session,
            runtime.code_mode_host_sha256,
        ):
            raise RunnerDockerError("Docker RESUME session binding is unavailable")

    return DockerWorkItemContext(
        request.work_item_id,
        paths.repository,
        generation_directory,
        codex_home,
        work_item_auth,
        auth_binding,
        session_binding,
        tool_binding,
        runtime.image,
        runtime.codex_sha256,
        runtime.code_mode_host_sha256,
        request.session_generation_id,
        request.session_generation,
        request.agent_policy_digest,
        generation_record,
    )


def load_docker_generation_receipt(
    *,
    runtime: DockerCodexRuntime,
    paths: RunnerWorkspacePaths,
    request: RunnerRequest,
) -> tuple[DockerWorkItemContext, str] | None:
    """Read one exact v2 session/tool receipt without mutating Runner state."""
    if (
        request.version != NEXT_PROTOCOL_VERSION
        or request.operation is not RunnerOperation.STATUS
    ):
        raise ValueError("request must be a v2 STATUS operation")
    assert request.session_generation_id is not None
    assert request.session_generation is not None
    assert request.agent_policy_digest is not None
    _validate_runtime(runtime)
    _owned_protected_directory(paths.root, "WorkItem root")
    _owned_protected_directory(paths.repository, "WorkItem repository")
    _owned_protected_directory(paths.state, "WorkItem state")
    generation_root, records = _generation_records(paths.state, create_root=False)
    if any(record[0] != request.work_item_id for record in records.values()):
        raise RunnerDockerError("Docker generation WorkItem identity conflicts")
    generation_directory = generation_root / request.session_generation_id
    observed = records.get(request.session_generation_id)
    expected = (
        request.work_item_id,
        request.session_generation_id,
        request.session_generation,
        request.agent_policy_digest,
        runtime.image,
        runtime.codex_sha256,
        runtime.code_mode_host_sha256,
    )
    if observed != expected:
        raise RunnerDockerError("Docker STATUS generation binding is unavailable")
    codex_home = generation_directory / "codex-home"
    context = DockerWorkItemContext(
        request.work_item_id,
        paths.repository,
        generation_directory,
        codex_home,
        codex_home / "auth.json",
        generation_directory / "codex-auth-binding.json",
        generation_directory / "codex-session.json",
        generation_directory / "codex-session-tools.json",
        runtime.image,
        runtime.codex_sha256,
        runtime.code_mode_host_sha256,
        request.session_generation_id,
        request.session_generation,
        request.agent_policy_digest,
        generation_directory / "generation.json",
    )
    _owned_protected_directory(codex_home, "generation Codex home")
    validate_docker_auth_state(context)
    session = _read_session_binding(context.session_binding, required=False)
    tools = _read_tool_binding(context.tool_binding, required=False)
    if session is None and tools is None:
        return None
    if session is None or tools is None:
        raise RunnerDockerError("Docker generation receipt is incomplete")
    expected_session_prefix = (
        request.work_item_id,
        session[1],
        runtime.image,
        runtime.codex_sha256,
    )
    if session != expected_session_prefix or tools != (
        *expected_session_prefix,
        runtime.code_mode_host_sha256,
    ):
        raise RunnerDockerError("Docker generation receipt conflicts")
    return context, session[1]


def _generation_records(
    state: Path,
    *,
    create_root: bool,
) -> tuple[Path, dict[str, tuple[str, str, int, str, str, str, str]]]:
    root = state / "generations"
    if not root.exists():
        if not create_root:
            raise RunnerDockerError("Docker generations directory is unavailable")
        _create_protected_directory(
            root,
            parent=state,
            field="Docker generations directory",
        )
    _owned_protected_directory(root, "Docker generations directory")
    records: dict[str, tuple[str, str, int, str, str, str, str]] = {}
    numbers: set[int] = set()
    try:
        entries = tuple(root.iterdir())
    except OSError as exc:
        raise RunnerDockerError("Docker generations directory is unavailable") from exc
    for entry in entries:
        try:
            generation_id = validate_session_generation_id(entry.name)
        except ValueError as exc:
            raise RunnerDockerError(
                "Docker generations directory contains an invalid entry"
            ) from exc
        _owned_protected_directory(entry, "Docker generation directory")
        record = _read_generation_record(entry / "generation.json")
        if record[1] != generation_id or record[2] in numbers:
            raise RunnerDockerError("Docker generation records conflict")
        records[generation_id] = record
        numbers.add(record[2])
    return root, records


def _read_generation_record(
    path: Path,
) -> tuple[str, str, int, str, str, str, str]:
    try:
        if path.lstat().st_nlink != 1:
            raise RunnerDockerError("Docker generation record is invalid")
    except OSError as exc:
        raise RunnerDockerError("Docker generation record is unavailable") from exc
    payload = _read_binding_payload(
        path,
        required=True,
        field="Docker generation record",
    )
    assert payload is not None
    expected_fields = {
        "version",
        "work_item_id",
        "session_generation_id",
        "session_generation",
        "agent_policy_digest",
        "image",
        "codex_sha256",
        "code_mode_host_sha256",
    }
    try:
        if set(payload) != expected_fields or payload["version"] != (
            _GENERATION_RECORD_VERSION
        ):
            raise ValueError("unexpected fields")
        work_item_id = validate_work_item_id(payload["work_item_id"])
        generation_id = validate_session_generation_id(
            payload["session_generation_id"]
        )
        generation = payload["session_generation"]
        if type(generation) is not int or generation <= 0:
            raise ValueError("invalid generation")
        policy_digest = _validate_binding_sha256(
            payload["agent_policy_digest"], "agent policy"
        )
        image = payload["image"]
        if not isinstance(image, str) or not _PINNED_IMAGE_RE.fullmatch(image):
            raise ValueError("invalid image")
        codex_sha256 = _validate_binding_sha256(payload["codex_sha256"], "Codex")
        code_mode_sha256 = _validate_binding_sha256(
            payload["code_mode_host_sha256"], "code-mode host"
        )
    except (TypeError, ValueError) as exc:
        raise RunnerDockerError("Docker generation record is malformed") from exc
    return (
        work_item_id,
        generation_id,
        generation,
        policy_digest,
        image,
        codex_sha256,
        code_mode_sha256,
    )


def _create_protected_directory(path: Path, *, parent: Path, field: str) -> None:
    _owned_protected_directory(parent, f"{field} parent")
    try:
        path.mkdir(mode=0o700)
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise RunnerDockerError(f"{field} could not be created") from exc
    _owned_protected_directory(path, field)


def validate_docker_command_boundary(
    *,
    runtime: DockerCodexRuntime,
    context: DockerWorkItemContext,
    codex_path: Path,
    output_schema: Path,
) -> None:
    """Freshly validate every mutable input immediately before Docker starts."""
    _validate_runtime(runtime)
    _validate_docker_assets(runtime)
    _owned_protected_directory(context.repository.parent, "WorkItem root")
    _owned_protected_directory(context.repository, "WorkItem repository")
    _owned_protected_directory(context.state, "WorkItem state")
    _owned_protected_directory(context.codex_home, "WorkItem Codex home")
    common_invalid = (
        context.repository != context.repository.parent / "repo"
        or context.image != runtime.image
        or context.codex_sha256 != runtime.codex_sha256
        or context.code_mode_host_sha256 != runtime.code_mode_host_sha256
        or runtime.code_mode_host_path
        != codex_path.with_name(CONTAINER_CODE_MODE_HOST_PATH.name)
    )
    if context.session_generation_id is None:
        layout_invalid = (
            context.state != context.repository.parent / "runner-state"
            or context.codex_home != context.state / "codex-home"
            or context.auth_file != context.codex_home / "auth.json"
            or context.auth_binding != context.state / "codex-auth-binding.json"
            or context.session_binding != context.state / "codex-session.json"
            or context.tool_binding != context.state / "codex-session-tools.json"
            or context.session_generation is not None
            or context.agent_policy_digest is not None
            or context.generation_record is not None
        )
    else:
        expected_generation_directory = (
            context.repository.parent
            / "runner-state"
            / "generations"
            / context.session_generation_id
        )
        layout_invalid = (
            context.state != expected_generation_directory
            or context.codex_home != context.state / "codex-home"
            or context.auth_file != context.codex_home / "auth.json"
            or context.auth_binding != context.state / "codex-auth-binding.json"
            or context.session_binding != context.state / "codex-session.json"
            or context.tool_binding != context.state / "codex-session-tools.json"
            or context.generation_record != context.state / "generation.json"
            or _read_generation_record(context.generation_record)
            != (
                context.work_item_id,
                context.session_generation_id,
                context.session_generation,
                context.agent_policy_digest,
                context.image,
                context.codex_sha256,
                context.code_mode_host_sha256,
            )
        )
    if common_invalid or layout_invalid:
        raise RunnerDockerError("Docker WorkItem context is inconsistent")
    _validate_codex_binary(codex_path, runtime.codex_sha256, "Codex executable")
    _validate_codex_binary(
        runtime.code_mode_host_path,
        runtime.code_mode_host_sha256,
        "Codex code-mode host",
    )
    validate_docker_auth_state(context)
    _trusted_regular_file(output_schema, "output schema", secret=False)


def validate_docker_auth_state(context: DockerWorkItemContext) -> None:
    """Validate the host-only WorkItem auth binding without exposing its content."""
    _owned_protected_directory(context.state, "WorkItem state")
    _validate_bound_auth(
        context.auth_file,
        context.auth_binding,
        context.work_item_id,
    )


def bind_docker_session(
    context: DockerWorkItemContext,
    *,
    work_item_id: str,
    session_id: str,
) -> None:
    """Persist one immutable WorkItem-to-session identity outside the container mount."""
    work_item_id = validate_work_item_id(work_item_id)
    session_id = validate_session_id(session_id)
    if work_item_id != context.work_item_id:
        raise RunnerDockerError("WorkItem Codex session binding conflicts")
    _owned_protected_directory(context.state, "WorkItem state")
    _owned_protected_directory(context.codex_home, "WorkItem Codex home")
    expected_session_binding = (
        work_item_id,
        session_id,
        context.image,
        context.codex_sha256,
    )
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

    # The session binding is the durable receipt commit marker. The complete
    # tool identity must reach disk first so a visible receipt can never be
    # paired with a missing tool binding after a host crash.
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


def inspect_docker_generation_container(
    *,
    runtime: DockerCodexRuntime,
    request: RunnerRequest,
) -> DockerGenerationContainerState:
    """Inspect one exact v2 Turn container without changing Docker state."""
    if request.version != NEXT_PROTOCOL_VERSION or request.turn_id is None:
        raise ValueError("request must identify one v2 Turn")
    assert request.session_generation_id is not None
    assert request.session_generation is not None
    assert request.agent_policy_digest is not None
    _validate_runtime(runtime)
    container_name = f"codex-{request.turn_id}"
    result = run_command(
        (
            str(runtime.docker_path),
            f"--host={runtime.docker_host}",
            "container",
            "inspect",
            container_name,
            "--format={{json .}}",
        ),
        timeout_seconds=10.0,
        max_output_bytes=64 * 1024,
        env={"DOCKER_CONFIG": str(runtime.cli_config_directory)},
    )
    if (
        result.timed_out
        or result.error is not None
        or result.stdout_truncated
        or result.stderr_truncated
    ):
        raise RunnerDockerError("Docker container inspection failed")
    if result.returncode != 0:
        missing_messages = {
            f"Error response from daemon: No such container: {container_name}",
            f"Error: No such object: {container_name}",
        }
        if (
            result.returncode == 1
            and not result.stdout.strip()
            and result.stderr.strip() in missing_messages
        ):
            return DockerGenerationContainerState.ABSENT
        raise RunnerDockerError("Docker container inspection failed")
    if result.stderr:
        raise RunnerDockerError("Docker container inspection failed")
    try:
        payload = json.loads(result.stdout, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RunnerDockerError(
            "Docker container inspection output is malformed"
        ) from exc
    if not isinstance(payload, dict):
        raise RunnerDockerError("Docker container inspection output is malformed")
    config = payload.get("Config")
    host_config = payload.get("HostConfig")
    network_settings = payload.get("NetworkSettings")
    state = payload.get("State")
    labels = config.get("Labels") if isinstance(config, dict) else None
    networks = (
        network_settings.get("Networks")
        if isinstance(network_settings, dict)
        else None
    )
    expected_labels = {
        DOCKER_LABEL_WORK_ITEM: request.work_item_id,
        DOCKER_LABEL_SESSION_GENERATION_ID: request.session_generation_id,
        DOCKER_LABEL_SESSION_GENERATION: str(request.session_generation),
        DOCKER_LABEL_TURN: request.turn_id,
        DOCKER_LABEL_POLICY_DIGEST: request.agent_policy_digest,
    }
    identity_matches = bool(
        payload.get("Name") == f"/{container_name}"
        and isinstance(config, dict)
        and config.get("Image") == runtime.image
        and isinstance(labels, dict)
        and all(labels.get(key) == value for key, value in expected_labels.items())
        and isinstance(host_config, dict)
        and host_config.get("NetworkMode") == DOCKER_NETWORK
        and isinstance(networks, dict)
        and set(networks) == {DOCKER_NETWORK}
    )
    if not identity_matches or not isinstance(state, dict):
        raise RunnerDockerError("Docker container identity is invalid")
    running = state.get("Running")
    paused = state.get("Paused")
    restarting = state.get("Restarting")
    dead = state.get("Dead")
    pid = state.get("Pid")
    if (
        type(running) is not bool
        or type(paused) is not bool
        or type(restarting) is not bool
        or type(dead) is not bool
        or type(pid) is not int
        or paused
        or restarting
        or dead
        or (running and pid <= 0)
        or (not running and pid != 0)
    ):
        raise RunnerDockerError("Docker container state is invalid")
    return (
        DockerGenerationContainerState.RUNNING
        if running
        else DockerGenerationContainerState.STOPPED
    )


def docker_generation_container_is_running(
    *,
    runtime: DockerCodexRuntime,
    request: RunnerRequest,
) -> bool:
    """Compatibility wrapper proving whether the exact container is running."""
    return (
        inspect_docker_generation_container(runtime=runtime, request=request)
        is DockerGenerationContainerState.RUNNING
    )


def _seed_work_item_auth(
    *,
    state: Path,
    source: Path,
    destination: Path,
    binding: Path,
    work_item_id: str,
) -> None:
    work_item_id = validate_work_item_id(work_item_id)
    _owned_protected_directory(state, "WorkItem state")
    if (
        destination != state / "codex-home" / "auth.json"
        or binding != state / "codex-auth-binding.json"
    ):
        raise RunnerDockerError("WorkItem auth paths are inconsistent")
    if (
        destination.exists()
        or destination.is_symlink()
        or binding.exists()
        or binding.is_symlink()
    ):
        raise RunnerDockerError("WorkItem auth binding already exists")
    raw = _read_auth_bytes(source, "Codex auth source", trusted_source=True)
    source_sha256 = sha256(raw).hexdigest()
    temporary = destination.parent / f".{destination.name}.{os.getpid()}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination, follow_symlinks=False)
        directory_descriptor = os.open(
            destination.parent, os.O_RDONLY | os.O_DIRECTORY
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise RunnerDockerError("WorkItem auth file could not be persisted") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    _persist_binding(
        binding,
        {
            "version": _AUTH_BINDING_VERSION,
            "work_item_id": work_item_id,
            "source_sha256": source_sha256,
        },
        "WorkItem Codex auth binding",
    )
    _validate_bound_auth(destination, binding, work_item_id)


def _validate_bound_auth(auth_file: Path, binding: Path, work_item_id: str) -> None:
    _read_auth_bytes(auth_file, "WorkItem Codex auth file", trusted_source=False)
    bound = _read_auth_binding(binding)
    if bound is None or bound[0] != validate_work_item_id(work_item_id):
        raise RunnerDockerError("WorkItem Codex auth binding conflicts")


def _read_auth_binding(
    path: Path, *, required: bool = True
) -> tuple[str, str] | None:
    payload = _read_binding_payload(
        path, required=required, field="WorkItem Codex auth binding"
    )
    if payload is None:
        return None
    try:
        if set(payload) != {"version", "work_item_id", "source_sha256"}:
            raise ValueError("unexpected fields")
        if payload["version"] != _AUTH_BINDING_VERSION:
            raise ValueError("unsupported version")
        work_item_id = validate_work_item_id(payload["work_item_id"])
        source_sha256 = _validate_binding_sha256(
            payload["source_sha256"], "auth source"
        )
    except (TypeError, ValueError) as exc:
        raise RunnerDockerError("WorkItem Codex auth binding is malformed") from exc
    return work_item_id, source_sha256


def _read_auth_bytes(path: Path, field: str, *, trusted_source: bool) -> bytes:
    try:
        metadata = path.lstat()
        raw = path.read_bytes()
    except OSError as exc:
        raise RunnerDockerError(f"{field} is unavailable") from exc
    valid_owner = (
        metadata.st_uid in {0, os.geteuid()}
        if trusted_source
        else metadata.st_uid == os.geteuid()
    )
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or not valid_owner
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or not raw
        or len(raw) > _MAX_AUTH_BYTES
        or b"\x00" in raw
    ):
        raise RunnerDockerError(f"{field} is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RunnerDockerError(f"{field} is malformed") from exc
    if not isinstance(payload, dict):
        raise RunnerDockerError(f"{field} is malformed")
    return raw


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
