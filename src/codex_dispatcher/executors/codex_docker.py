"""Pure fixed-argv planning for one rootless Docker Codex container."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from codex_dispatcher.executors.codex_cli import (
    build_codex_invocation,
    build_codex_login_status_invocation,
    validate_egress_proxy_url,
)
from codex_dispatcher.work_items import validate_turn_id, validate_work_item_id


CONTAINER_CODEX_HOME = Path("/codex-home")
CONTAINER_CODEX_PATH = Path("/usr/local/bin/codex")
CONTAINER_CODE_MODE_HOST_PATH = Path("/usr/local/bin/codex-code-mode-host")
CONTAINER_TIMEOUT_PATH = Path("/usr/bin/timeout")
CONTAINER_REPOSITORY = Path("/workspace")
CONTAINER_SCHEMA = Path("/runner-contract/agent-result.schema.json")
DOCKER_NETWORK = "codex-egress"
DOCKER_NETWORK_SUBNET = "172.30.0.0/24"
DOCKER_NETWORK_GATEWAY = "172.30.0.1"
ROOTLESS_HOST_PROXY_URL = "http://10.0.2.2:3128"
CPU_LIMIT = "2.0"
MEMORY_LIMIT_BYTES = 8 * 1024 * 1024 * 1024
PIDS_LIMIT = 512
TMPFS_LIMIT_BYTES = 1024 * 1024 * 1024
AUTH_TIMEOUT_SECONDS = 20
CONTAINER_KILL_GRACE_SECONDS = 5

_IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class DockerCodexRuntime:
    """Trusted, host-level Docker inputs that never come from an Issue."""

    docker_path: Path
    docker_host: str
    cli_config_directory: Path
    image: str
    codex_sha256: str
    code_mode_host_path: Path
    code_mode_host_sha256: str
    egress_proxy_url: str | None = None

    def __post_init__(self) -> None:
        _absolute_host_path(self.docker_path, "docker_path")
        _absolute_host_path(self.cli_config_directory, "cli_config_directory")
        _absolute_host_path(self.code_mode_host_path, "code_mode_host_path")
        if self.code_mode_host_path.name != CONTAINER_CODE_MODE_HOST_PATH.name:
            raise ValueError(
                "code_mode_host_path must name codex-code-mode-host"
            )
        if (
            not isinstance(self.docker_host, str)
            or not self.docker_host.startswith("unix:///")
            or "\x00" in self.docker_host
            or any(character.isspace() for character in self.docker_host)
        ):
            raise ValueError("docker_host must be an absolute Unix socket URL")
        socket_path = Path(self.docker_host.removeprefix("unix://"))
        _absolute_host_path(socket_path, "docker_host socket")
        if not isinstance(self.image, str) or not _IMAGE_RE.fullmatch(self.image):
            raise ValueError("image must be a lowercase digest-pinned reference")
        if (
            not isinstance(self.codex_sha256, str)
            or not _SHA256_RE.fullmatch(self.codex_sha256)
        ):
            raise ValueError("codex_sha256 must be a lowercase SHA-256 digest")
        if (
            not isinstance(self.code_mode_host_sha256, str)
            or not _SHA256_RE.fullmatch(self.code_mode_host_sha256)
        ):
            raise ValueError(
                "code_mode_host_sha256 must be a lowercase SHA-256 digest"
            )
        if self.egress_proxy_url is not None:
            validate_egress_proxy_url(self.egress_proxy_url)


@dataclass(frozen=True, slots=True)
class DockerCodexPlan:
    argv: tuple[str, ...]
    environment: Mapping[str, str]
    reads_prompt_from_stdin: bool


def build_docker_login_status_plan(
    *,
    runtime: DockerCodexRuntime,
    work_item_id: str,
    codex_path: Path,
    codex_home: Path,
    auth_file: Path,
) -> DockerCodexPlan:
    """Run only the fixed ChatGPT login-status check in one WorkItem boundary."""
    proxy_url = _required_proxy_url(runtime)
    work_item_id = validate_work_item_id(work_item_id)
    codex_path = _mount_source(codex_path, "codex_path")
    code_mode_host_path = _mount_source(
        runtime.code_mode_host_path, "code_mode_host_path"
    )
    _validate_codex_tool_bundle(codex_path, code_mode_host_path)
    codex_home = _mount_source(codex_home, "codex_home")
    auth_file = _mount_source(auth_file, "auth_file")
    _validate_codex_home(codex_home)
    if auth_file.name != "auth.json":
        raise ValueError("auth_file must name auth.json")
    inner = build_codex_login_status_invocation(
        codex_path=CONTAINER_CODEX_PATH,
        codex_home=CONTAINER_CODEX_HOME,
        egress_proxy_url=proxy_url,
    )
    argv = (
        *_docker_prefix(runtime, f"codex-auth-{work_item_id}"),
        _mount(codex_path, CONTAINER_CODEX_PATH, readonly=True),
        _mount(
            code_mode_host_path,
            CONTAINER_CODE_MODE_HOST_PATH,
            readonly=True,
        ),
        _mount(codex_home, CONTAINER_CODEX_HOME),
        _mount(auth_file, CONTAINER_CODEX_HOME / "auth.json", readonly=True),
        *_container_environment(inner.environment),
        f"--workdir={CONTAINER_CODEX_HOME}",
        runtime.image,
        *_container_timeout(AUTH_TIMEOUT_SECONDS),
        *inner.argv,
    )
    return DockerCodexPlan(
        argv,
        MappingProxyType({"DOCKER_CONFIG": str(runtime.cli_config_directory)}),
        False,
    )


def build_docker_codex_plan(
    *,
    runtime: DockerCodexRuntime,
    work_item_id: str,
    turn_id: str,
    codex_path: Path,
    repository: Path,
    codex_home: Path,
    auth_file: Path,
    output_schema: Path,
    session_id: str | None,
    timeout_seconds: float = 3600.0,
) -> DockerCodexPlan:
    """Build a fixed Docker invocation; the Prompt remains standard-input only."""
    proxy_url = _required_proxy_url(runtime)
    validate_work_item_id(work_item_id)
    turn_id = validate_turn_id(turn_id)
    codex_path = _mount_source(codex_path, "codex_path")
    code_mode_host_path = _mount_source(
        runtime.code_mode_host_path, "code_mode_host_path"
    )
    _validate_codex_tool_bundle(codex_path, code_mode_host_path)
    repository = _mount_source(repository, "repository")
    codex_home = _mount_source(codex_home, "codex_home")
    auth_file = _mount_source(auth_file, "auth_file")
    output_schema = _mount_source(output_schema, "output_schema")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be positive")
    _validate_work_item_mounts(repository, codex_home)
    if auth_file.name != "auth.json":
        raise ValueError("auth_file must name auth.json")
    if output_schema.name != "agent-result.schema.json":
        raise ValueError("output_schema must name agent-result.schema.json")
    inner = build_codex_invocation(
        codex_path=CONTAINER_CODEX_PATH,
        repository_directory=CONTAINER_REPOSITORY,
        codex_home=CONTAINER_CODEX_HOME,
        output_schema=CONTAINER_SCHEMA,
        session_id=session_id,
        egress_proxy_url=proxy_url,
    )
    argv = (
        *_docker_prefix(runtime, f"codex-{turn_id}"),
        _mount(codex_path, CONTAINER_CODEX_PATH, readonly=True),
        _mount(
            code_mode_host_path,
            CONTAINER_CODE_MODE_HOST_PATH,
            readonly=True,
        ),
        _mount(repository, CONTAINER_REPOSITORY),
        _mount(codex_home, CONTAINER_CODEX_HOME),
        _mount(auth_file, CONTAINER_CODEX_HOME / "auth.json", readonly=True),
        _mount(output_schema, CONTAINER_SCHEMA, readonly=True),
        *_container_environment(inner.environment),
        f"--workdir={CONTAINER_REPOSITORY}",
        runtime.image,
        *_container_timeout(timeout_seconds),
        *inner.argv,
    )
    return DockerCodexPlan(
        argv,
        MappingProxyType({"DOCKER_CONFIG": str(runtime.cli_config_directory)}),
        True,
    )


def _docker_prefix(runtime: DockerCodexRuntime, name: str) -> tuple[str, ...]:
    return (
        str(runtime.docker_path),
        f"--host={runtime.docker_host}",
        "run",
        "--rm",
        f"--stop-timeout={CONTAINER_KILL_GRACE_SECONDS}",
        "--pull=never",
        "--log-driver=none",
        f"--name={name}",
        "--interactive",
        "--init",
        "--entrypoint=",
        "--user=0:0",
        "--read-only",
        f"--network={DOCKER_NETWORK}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true",
        "--pids-limit=512",
        f"--memory={MEMORY_LIMIT_BYTES}",
        f"--memory-swap={MEMORY_LIMIT_BYTES}",
        f"--cpus={CPU_LIMIT}",
        "--ulimit=nofile=1024:1024",
        "--ulimit=nproc=512:512",
        "--ulimit=core=0:0",
        (
            "--tmpfs=/tmp:rw,nosuid,nodev,mode=1777,"
            f"size={TMPFS_LIMIT_BYTES}"
        ),
    )


def _container_timeout(timeout_seconds: float) -> tuple[str, ...]:
    return (
        str(CONTAINER_TIMEOUT_PATH),
        "--signal=KILL",
        f"--kill-after={CONTAINER_KILL_GRACE_SECONDS}s",
        f"{math.ceil(timeout_seconds)}s",
    )


def _required_proxy_url(runtime: DockerCodexRuntime) -> str:
    if runtime.egress_proxy_url is None:
        raise ValueError("Docker Codex plans require an audited egress proxy")
    return runtime.egress_proxy_url


def _container_environment(environment: Mapping[str, str]) -> tuple[str, ...]:
    return (
        f"--env=HOME={CONTAINER_CODEX_HOME}",
        *(f"--env={key}={value}" for key, value in environment.items()),
    )


def _mount(source: Path, target: Path, *, readonly: bool = False) -> str:
    suffix = ",readonly" if readonly else ""
    return f"--mount=type=bind,source={source},target={target}{suffix}"


def _mount_source(value: Path, field: str) -> Path:
    value = _absolute_host_path(value, field)
    if "," in str(value):
        raise ValueError(f"{field} must not contain a comma")
    return value


def _validate_work_item_mounts(repository: Path, codex_home: Path) -> None:
    work_item_root = repository.parent
    _validate_codex_home(codex_home)
    if (
        repository.name != "repo"
        or codex_home != work_item_root / "runner-state" / "codex-home"
    ):
        raise ValueError("container mounts must belong to one WorkItem directory")


def _validate_codex_home(codex_home: Path) -> None:
    if codex_home.name != "codex-home" or codex_home.parent.name != "runner-state":
        raise ValueError("codex_home must belong to one WorkItem runner-state")


def _validate_codex_tool_bundle(
    codex_path: Path, code_mode_host_path: Path
) -> None:
    if code_mode_host_path != codex_path.with_name(
        CONTAINER_CODE_MODE_HOST_PATH.name
    ):
        raise ValueError(
            "code_mode_host_path must be the fixed sibling of codex_path"
        )


def _absolute_host_path(value: Path, field: str) -> Path:
    if (
        not isinstance(value, Path)
        or not value.is_absolute()
        or ".." in value.parts
        or "\x00" in str(value)
        or any(ord(character) < 32 for character in str(value))
    ):
        raise ValueError(f"{field} must be a normalized absolute path")
    return value
