"""Process entry point for the fixed ``codex-runner-v1`` forced SSH command."""

from __future__ import annotations

import json
import math
import os
import signal
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from codex_dispatcher.executors.codex_cli import validate_egress_proxy_url
from codex_dispatcher.executors.codex_docker import (
    CONTAINER_CODE_MODE_HOST_PATH,
    ROOTLESS_HOST_PROXY_URL,
    DockerCodexRuntime,
)
from codex_dispatcher.runner_disk import (
    FusedWorkItemDisk,
    WorkItemDiskRuntime,
)
from codex_dispatcher.runner_protocol import RunnerProtocolError
from codex_dispatcher.runner_service import LinuxRunnerService, serve_one
from codex_dispatcher.runner_transport import RunnerTransportRejected
from codex_dispatcher.runner_turns import RunnerTurnExecutor
from codex_dispatcher.runner_workspace import RunnerWorkspace


DEFAULT_RUNNER_CONFIG = Path("/srv/codex-runner/etc/config.json")
_CONFIG_VERSION = 1
_MAX_CONFIG_BYTES = 32 * 1024


class RunnerConfigurationError(ValueError):
    """Raised when the protected Runner configuration is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class RunnerConfiguration:
    git_path: Path
    codex_path: Path
    codex_home: Path
    output_schema: Path
    work_items_root: Path
    active_lock_path: Path
    git_timeout_seconds: float
    codex_timeout_seconds: float
    egress_proxy_url: str | None
    execution_mode: str = "direct"
    docker_runtime: DockerCodexRuntime | None = None
    work_item_disk: WorkItemDiskRuntime | None = None

    def __post_init__(self) -> None:
        if (
            self.execution_mode == "direct"
            and self.docker_runtime is None
            and self.work_item_disk is None
        ):
            return
        if (
            self.execution_mode == "rootless_docker"
            and isinstance(self.docker_runtime, DockerCodexRuntime)
            and isinstance(self.work_item_disk, WorkItemDiskRuntime)
        ):
            return
        raise ValueError("execution mode, Docker, and WorkItem disk are inconsistent")


def load_runner_configuration(path: Path) -> RunnerConfiguration:
    path = _normalized_absolute(path, "config path")
    try:
        config_stat = path.lstat()
    except OSError as exc:
        raise RunnerConfigurationError("Runner config is unavailable") from exc
    if (
        not stat.S_ISREG(config_stat.st_mode)
        or path.is_symlink()
        or config_stat.st_uid not in {0, os.geteuid()}
        or config_stat.st_mode & 0o022
        or not 0 < config_stat.st_size <= _MAX_CONFIG_BYTES
    ):
        raise RunnerConfigurationError(
            "Runner config must be a trusted protected regular file"
        )
    _validate_trusted_parents(path, "Runner config")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RunnerConfigurationError("Runner config is malformed") from exc
    required = {
        "version",
        "git_path",
        "codex_path",
        "codex_home",
        "output_schema",
        "work_items_root",
        "active_lock_path",
        "git_timeout_seconds",
        "codex_timeout_seconds",
    }
    optional = {"egress_proxy_url", "execution_mode", "docker_runtime"}
    if (
        not isinstance(payload, dict)
        or not required.issubset(payload)
        or set(payload) - required - optional
    ):
        raise RunnerConfigurationError("Runner config fields are invalid")
    if payload["version"] != _CONFIG_VERSION:
        raise RunnerConfigurationError("Runner config version is unsupported")
    git_path = _protected_executable(payload["git_path"], "git_path")
    codex_path = _protected_executable(payload["codex_path"], "codex_path")
    codex_home = _protected_directory(payload["codex_home"], "codex_home")
    output_schema = _protected_regular_file(payload["output_schema"], "output_schema")
    work_items_root = _protected_directory(
        payload["work_items_root"], "work_items_root"
    )
    active_lock_path = _configured_path(payload["active_lock_path"], "active_lock_path")
    _protected_directory(str(active_lock_path.parent), "active_lock_path parent")
    if active_lock_path.exists():
        try:
            lock_stat = active_lock_path.lstat()
        except OSError as exc:
            raise RunnerConfigurationError("active_lock_path is unavailable") from exc
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or active_lock_path.is_symlink()
            or lock_stat.st_uid != os.geteuid()
            or lock_stat.st_mode & 0o077
        ):
            raise RunnerConfigurationError(
                "active_lock_path must be an owned protected regular file"
            )
    git_timeout = _positive_number(payload["git_timeout_seconds"], "git_timeout_seconds")
    codex_timeout = _positive_number(
        payload["codex_timeout_seconds"], "codex_timeout_seconds"
    )
    try:
        egress_proxy_url = (
            validate_egress_proxy_url(payload["egress_proxy_url"])
            if "egress_proxy_url" in payload
            else None
        )
    except ValueError as exc:
        raise RunnerConfigurationError("egress_proxy_url is invalid") from exc
    execution_mode = payload.get("execution_mode", "direct")
    if execution_mode not in {"direct", "rootless_docker"}:
        raise RunnerConfigurationError("execution_mode is invalid")
    docker_payload = payload.get("docker_runtime")
    if execution_mode == "direct":
        if "docker_runtime" in payload:
            raise RunnerConfigurationError(
                "docker_runtime requires rootless_docker execution_mode"
            )
        docker_runtime = None
        work_item_disk = None
    else:
        docker_runtime, work_item_disk = _load_docker_runtime(
            docker_payload,
            codex_path=codex_path,
            work_items_root=work_items_root,
        )
    return RunnerConfiguration(
        git_path=git_path,
        codex_path=codex_path,
        codex_home=codex_home,
        output_schema=output_schema,
        work_items_root=work_items_root,
        active_lock_path=active_lock_path,
        git_timeout_seconds=git_timeout,
        codex_timeout_seconds=codex_timeout,
        egress_proxy_url=egress_proxy_url,
        execution_mode=execution_mode,
        docker_runtime=docker_runtime,
        work_item_disk=work_item_disk,
    )


def build_runner_service(configuration: RunnerConfiguration) -> LinuxRunnerService:
    if not isinstance(configuration, RunnerConfiguration):
        raise TypeError("configuration must be a RunnerConfiguration")
    workspace = RunnerWorkspace(
        git_path=configuration.git_path,
        work_items_root=configuration.work_items_root,
        timeout_seconds=configuration.git_timeout_seconds,
        work_item_disk=(
            FusedWorkItemDisk(
                configuration.work_item_disk,
                work_items_root=configuration.work_items_root,
            )
            if configuration.work_item_disk is not None
            else None
        ),
    )
    turns = RunnerTurnExecutor(
        workspace=workspace,
        codex_path=configuration.codex_path,
        codex_home=configuration.codex_home,
        output_schema=configuration.output_schema,
        timeout_seconds=configuration.codex_timeout_seconds,
        egress_proxy_url=configuration.egress_proxy_url,
        docker_runtime=configuration.docker_runtime,
    )
    return LinuxRunnerService(
        workspace=workspace,
        turns=turns,
        active_lock_path=configuration.active_lock_path,
    )


def run(
    *,
    config_path: Path = DEFAULT_RUNNER_CONFIG,
    input_stream: BinaryIO | None = None,
    output_stream: BinaryIO | None = None,
    error_stream: BinaryIO | None = None,
) -> int:
    """Serve exactly one request and reveal no exception or untrusted text on failure."""
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer
    error_stream = error_stream or sys.stderr.buffer
    try:
        if os.name == "posix" and hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        configuration = load_runner_configuration(config_path)
        serve_one(build_runner_service(configuration), input_stream, output_stream)
    except (RunnerConfigurationError, RunnerProtocolError, RunnerTransportRejected):
        error_stream.write(b"codex-runner-v1: request rejected\n")
        error_stream.flush()
        return 2
    except OSError:
        error_stream.write(b"codex-runner-v1: internal failure\n")
        error_stream.flush()
        return 3
    except Exception:  # pragma: no cover - last-resort forced-command boundary
        error_stream.write(b"codex-runner-v1: internal failure\n")
        error_stream.flush()
        return 3
    return 0


def main() -> int:
    if len(sys.argv) != 1:
        sys.stderr.write("codex-runner-v1: arguments are not accepted\n")
        return 2
    return run()


def _configured_path(value: Any, field: str) -> Path:
    if not isinstance(value, str):
        raise RunnerConfigurationError(f"{field} must be an absolute path")
    return _normalized_absolute(Path(value), field)


def _normalized_absolute(value: Path, field: str) -> Path:
    if (
        not isinstance(value, Path)
        or not value.is_absolute()
        or ".." in value.parts
        or "\x00" in str(value)
    ):
        raise RunnerConfigurationError(f"{field} must be a normalized absolute path")
    return value


def _protected_executable(value: Any, field: str) -> Path:
    path = _configured_path(value, field)
    try:
        resolved = path.resolve(strict=True)
        file_stat = resolved.stat()
    except OSError as exc:
        raise RunnerConfigurationError(f"{field} is unavailable") from exc
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid not in {0, os.geteuid()}
        or file_stat.st_mode & 0o022
        or not file_stat.st_mode & 0o111
    ):
        raise RunnerConfigurationError(
            f"{field} must be a trusted protected executable"
        )
    _validate_trusted_parents(resolved, field)
    return resolved


def _protected_regular_file(value: Any, field: str) -> Path:
    path = _configured_path(value, field)
    try:
        resolved = path.resolve(strict=True)
        file_stat = resolved.stat()
    except OSError as exc:
        raise RunnerConfigurationError(f"{field} is unavailable") from exc
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid not in {0, os.geteuid()}
        or file_stat.st_mode & 0o022
    ):
        raise RunnerConfigurationError(
            f"{field} must be a trusted protected regular file"
        )
    _validate_trusted_parents(resolved, field)
    return resolved


def _protected_directory(value: Any, field: str) -> Path:
    path = _configured_path(value, field)
    try:
        resolved = path.resolve(strict=True)
        directory_stat = resolved.stat()
    except OSError as exc:
        raise RunnerConfigurationError(f"{field} is unavailable") from exc
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or path.is_symlink()
        or directory_stat.st_mode & 0o077
        or directory_stat.st_uid != os.geteuid()
    ):
        raise RunnerConfigurationError(f"{field} must be an owned protected directory")
    _validate_trusted_parents(resolved, field)
    return resolved


def _validate_trusted_parents(path: Path, field: str) -> None:
    """Reject replacement through an ancestor controlled by another account."""
    for parent in path.parents:
        try:
            parent_stat = parent.stat()
        except OSError as exc:
            raise RunnerConfigurationError(f"{field} parent is unavailable") from exc
        root_sticky_directory = (
            parent_stat.st_uid == 0 and bool(parent_stat.st_mode & stat.S_ISVTX)
        )
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid not in {0, os.geteuid()}
            or (parent_stat.st_mode & 0o022 and not root_sticky_directory)
        ):
            raise RunnerConfigurationError(
                f"{field} parent directories must be trusted and protected"
            )


def _positive_number(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise RunnerConfigurationError(f"{field} must be positive")
    return float(value)


def _load_docker_runtime(
    payload: Any,
    *,
    codex_path: Path,
    work_items_root: Path,
) -> tuple[DockerCodexRuntime, WorkItemDiskRuntime]:
    expected = {
        "docker_path",
        "docker_host",
        "cli_config_directory",
        "image",
        "codex_sha256",
        "code_mode_host_sha256",
        "egress_proxy_url",
        "work_item_disk",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise RunnerConfigurationError("docker_runtime fields are invalid")
    docker_path = _protected_executable(payload["docker_path"], "docker_path")
    code_mode_host_path = _protected_executable(
        str(codex_path.with_name(CONTAINER_CODE_MODE_HOST_PATH.name)),
        "code_mode_host_path",
    )
    cli_config_directory = _protected_directory(
        payload["cli_config_directory"], "cli_config_directory"
    )
    expected_host = f"unix:///run/user/{os.geteuid()}/docker.sock"
    if payload["docker_host"] != expected_host:
        raise RunnerConfigurationError("docker_host is not the rootless user socket")
    if payload["egress_proxy_url"] != ROOTLESS_HOST_PROXY_URL:
        raise RunnerConfigurationError("Docker egress_proxy_url is invalid")
    try:
        docker_runtime = DockerCodexRuntime(
            docker_path=docker_path,
            docker_host=payload["docker_host"],
            cli_config_directory=cli_config_directory,
            image=payload["image"],
            codex_sha256=payload["codex_sha256"],
            code_mode_host_path=code_mode_host_path,
            code_mode_host_sha256=payload["code_mode_host_sha256"],
            egress_proxy_url=payload["egress_proxy_url"],
        )
    except (TypeError, ValueError) as exc:
        raise RunnerConfigurationError("docker_runtime values are invalid") from exc
    disk = _load_work_item_disk(
        payload["work_item_disk"],
        work_items_root=work_items_root,
    )
    return docker_runtime, disk


def _load_work_item_disk(
    payload: Any,
    *,
    work_items_root: Path,
) -> WorkItemDiskRuntime:
    expected = {
        "image_directory",
        "image_size_bytes",
        "host_reserve_bytes",
        "mkfs_ext4_path",
        "fuse2fs_path",
        "fusermount_path",
        "e2fsck_path",
        "findmnt_path",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise RunnerConfigurationError("work_item_disk fields are invalid")
    image_directory = _protected_directory(
        payload["image_directory"], "work_item_disk image_directory"
    )
    try:
        image_directory.relative_to(work_items_root)
    except ValueError:
        pass
    else:
        raise RunnerConfigurationError(
            "work_item_disk images must remain outside work_items_root"
        )
    executables = {
        field: _protected_executable(payload[field], f"work_item_disk {field}")
        for field in (
            "mkfs_ext4_path",
            "fuse2fs_path",
            "fusermount_path",
            "e2fsck_path",
            "findmnt_path",
        )
    }
    try:
        return WorkItemDiskRuntime(
            image_directory=image_directory,
            image_size_bytes=payload["image_size_bytes"],
            host_reserve_bytes=payload["host_reserve_bytes"],
            **executables,
        )
    except (TypeError, ValueError) as exc:
        raise RunnerConfigurationError("work_item_disk values are invalid") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate Runner config field")
        result[key] = value
    return result


if __name__ == "__main__":
    raise SystemExit(main())
