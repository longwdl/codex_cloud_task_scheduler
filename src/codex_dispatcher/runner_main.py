"""Process entry point for the fixed ``codex-runner-v1`` forced SSH command."""

from __future__ import annotations

import json
import os
import signal
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

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
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RunnerConfigurationError("Runner config is malformed") from exc
    expected = {
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
    if not isinstance(payload, dict) or set(payload) != expected:
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
    return RunnerConfiguration(
        git_path,
        codex_path,
        codex_home,
        output_schema,
        work_items_root,
        active_lock_path,
        git_timeout,
        codex_timeout,
    )


def build_runner_service(configuration: RunnerConfiguration) -> LinuxRunnerService:
    if not isinstance(configuration, RunnerConfiguration):
        raise TypeError("configuration must be a RunnerConfiguration")
    workspace = RunnerWorkspace(
        git_path=configuration.git_path,
        work_items_root=configuration.work_items_root,
        timeout_seconds=configuration.git_timeout_seconds,
    )
    turns = RunnerTurnExecutor(
        workspace=workspace,
        codex_path=configuration.codex_path,
        codex_home=configuration.codex_home,
        output_schema=configuration.output_schema,
        timeout_seconds=configuration.codex_timeout_seconds,
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
    return resolved


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise RunnerConfigurationError(f"{field} must be positive")
    return float(value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate Runner config field")
        result[key] = value
    return result


if __name__ == "__main__":
    raise SystemExit(main())
