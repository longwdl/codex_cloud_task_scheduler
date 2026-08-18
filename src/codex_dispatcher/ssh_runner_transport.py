"""Fixed OpenSSH byte-stream adapter for the versioned Runner protocol."""

from __future__ import annotations

import os
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from codex_dispatcher.command_runner import run_binary_command
from codex_dispatcher.runner_protocol import RunnerProtocolError, RunnerRequest
from codex_dispatcher.runner_transport import (
    MAX_ARTIFACT_BYTES,
    MAX_RESPONSE_BYTES,
    RunnerTransportInterrupted,
    RunnerTransportRejected,
    RunnerWireOutput,
)
from codex_dispatcher.runner_wire import decode_runner_output, encode_runner_input


_USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_HOST_RE = re.compile(
    r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
)
_REMOTE_COMMAND = "/srv/codex-runner/bin/codex-runner-v1"
_MAX_FRAMED_OUTPUT_BYTES = MAX_RESPONSE_BYTES + MAX_ARTIFACT_BYTES + 128


@dataclass(frozen=True, slots=True)
class SshInvocationPlan:
    argv: tuple[str, ...]
    environment: Mapping[str, str]


class SshRunnerTransport:
    """Invoke only the fixed forced-command Runner endpoint over pinned OpenSSH."""

    def __init__(
        self,
        *,
        ssh_path: Path,
        host: str,
        user: str,
        port: int,
        known_hosts_path: Path,
        identity_file: Path,
        assh_proxy_path: Path | None = None,
        assh_home: Path | None = None,
        connect_timeout_seconds: int = 10,
        operation_timeout_seconds: float = 3900.0,
    ) -> None:
        self._ssh_path = _protected_executable(ssh_path, "ssh_path")
        if not isinstance(host, str) or _HOST_RE.fullmatch(host) is None:
            raise ValueError("host must be a fixed DNS name or IPv4 address")
        if not isinstance(user, str) or _USER_RE.fullmatch(user) is None:
            raise ValueError("user must be a safe Linux account name")
        if type(port) is not int or not 1 <= port <= 65_535:
            raise ValueError("port must be an integer between 1 and 65535")
        self._known_hosts_path = _protected_regular_file(
            known_hosts_path,
            "known_hosts_path",
            forbidden_mode=0o022,
        )
        self._identity_file = _protected_regular_file(
            identity_file,
            "identity_file",
            forbidden_mode=0o077,
        )
        self._assh_proxy_path = (
            None
            if assh_proxy_path is None
            else _protected_executable(assh_proxy_path, "assh_proxy_path")
        )
        if (self._assh_proxy_path is None) != (assh_home is None):
            raise ValueError("assh_proxy_path and assh_home must be configured together")
        self._assh_home = (
            None if assh_home is None else _protected_directory(assh_home, "assh_home")
        )
        if (
            type(connect_timeout_seconds) is not int
            or not 1 <= connect_timeout_seconds <= 60
        ):
            raise ValueError("connect_timeout_seconds must be an integer from 1 to 60")
        if operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be positive")
        self._host = host
        self._user = user
        self._port = port
        self._connect_timeout_seconds = connect_timeout_seconds
        self._operation_timeout_seconds = operation_timeout_seconds

    def invocation_plan(self) -> SshInvocationPlan:
        proxy_command = "none"
        if self._assh_proxy_path is not None:
            proxy_command = (
                f"{shlex.quote(str(self._assh_proxy_path))} connect --port=%p %h"
            )
        options = (
            "BatchMode=yes",
            "ClearAllForwardings=yes",
            "ExitOnForwardFailure=yes",
            "ForwardAgent=no",
            "ForwardX11=no",
            "AddKeysToAgent=no",
            "IdentityAgent=none",
            "IdentitiesOnly=yes",
            "PasswordAuthentication=no",
            "KbdInteractiveAuthentication=no",
            "PreferredAuthentications=publickey",
            "StrictHostKeyChecking=yes",
            f"UserKnownHostsFile={self._known_hosts_path}",
            "GlobalKnownHostsFile=/dev/null",
            f"ProxyCommand={proxy_command}",
            "ProxyJump=none",
            "PermitLocalCommand=no",
            "RequestTTY=no",
            "ControlMaster=no",
            "ControlPath=none",
            "ControlPersist=no",
            "UpdateHostKeys=no",
            "NumberOfPasswordPrompts=0",
            "ConnectionAttempts=1",
            f"ConnectTimeout={self._connect_timeout_seconds}",
            "ServerAliveInterval=15",
            "ServerAliveCountMax=2",
            "LogLevel=ERROR",
        )
        argv: list[str] = [str(self._ssh_path), "-T", "-F", "/dev/null"]
        for option in options:
            argv.extend(("-o", option))
        argv.extend(
            (
                "-i",
                str(self._identity_file),
                "-p",
                str(self._port),
                f"{self._user}@{self._host}",
                _REMOTE_COMMAND,
            )
        )
        environment = {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
        if self._assh_home is not None:
            environment["HOME"] = str(self._assh_home)
        return SshInvocationPlan(tuple(argv), MappingProxyType(environment))

    def invoke(
        self,
        request: RunnerRequest,
        *,
        stdin: bytes = b"",
        source_artifact: bytes | None = None,
    ) -> RunnerWireOutput:
        frame = encode_runner_input(
            request,
            prompt=stdin,
            source_artifact=source_artifact,
        )
        plan = self.invocation_plan()
        result = run_binary_command(
            plan.argv,
            timeout_seconds=self._operation_timeout_seconds,
            max_output_bytes=_MAX_FRAMED_OUTPUT_BYTES,
            max_stderr_bytes=65_536,
            env=plan.environment,
            input_bytes=frame,
        )
        if (
            result.timed_out
            or result.error is not None
            or result.stdout_truncated
            or result.stderr_truncated
            or result.returncode is None
            or result.returncode == 255
        ):
            raise RunnerTransportInterrupted("SSH Runner outcome is ambiguous")
        if result.returncode == 2:
            raise RunnerTransportRejected("SSH Runner definitively rejected the request")
        if result.returncode != 0:
            raise RunnerTransportInterrupted("SSH Runner failed without a definitive rejection")
        try:
            return decode_runner_output(result.stdout)
        except RunnerProtocolError as exc:
            raise RunnerTransportInterrupted("SSH Runner returned an ambiguous frame") from exc


def _protected_regular_file(value: Path, field: str, *, forbidden_mode: int) -> Path:
    if not isinstance(value, Path) or not value.is_absolute() or ".." in value.parts:
        raise ValueError(f"{field} must be a normalized absolute path")
    try:
        file_stat = value.stat()
    except OSError as exc:
        raise ValueError(f"{field} must reference an existing protected regular file") from exc
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_mode & forbidden_mode:
        raise ValueError(f"{field} must reference an existing protected regular file")
    return value


def _protected_executable(value: Path, field: str) -> Path:
    if (
        not isinstance(value, Path)
        or not value.is_absolute()
        or ".." in value.parts
        or "\x00" in str(value)
    ):
        raise ValueError(f"{field} must be a normalized absolute path")
    try:
        resolved = value.resolve(strict=True)
        file_stat = resolved.stat()
    except OSError as exc:
        raise ValueError(f"{field} must reference a protected executable") from exc
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_mode & 0o022
        or not file_stat.st_mode & 0o111
    ):
        raise ValueError(f"{field} must reference a protected executable")
    return value


def _protected_directory(value: Path, field: str) -> Path:
    if (
        not isinstance(value, Path)
        or not value.is_absolute()
        or ".." in value.parts
        or "\x00" in str(value)
    ):
        raise ValueError(f"{field} must be a normalized absolute path")
    try:
        directory_stat = value.lstat()
    except OSError as exc:
        raise ValueError(f"{field} must reference an owned protected directory") from exc
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_mode & 0o022
        or directory_stat.st_uid != os.geteuid()
    ):
        raise ValueError(f"{field} must reference an owned protected directory")
    return value
