"""Safe subprocess wrapper for dispatcher-owned, fixed argv commands only."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
import signal
import subprocess
import threading
import time
from typing import BinaryIO

from codex_dispatcher.redaction import redact_text


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BinaryCommandResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    error: str | None = None


class RunningBinaryCommand:
    """Capability limited to one exact subprocess started by this module."""

    __slots__ = ("_argv", "_process", "_termination_requested")

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        argv: Sequence[str],
    ) -> None:
        self._process = process
        self._argv = tuple(argv)
        self._termination_requested = False

    @property
    def argv(self) -> tuple[str, ...]:
        return self._argv

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def process_group_id(self) -> int:
        if os.name != "posix":
            raise RuntimeError("process groups require POSIX")
        return os.getpgid(self.pid)

    @property
    def session_id(self) -> int:
        if os.name != "posix":
            raise RuntimeError("process sessions require POSIX")
        return os.getsid(self.pid)

    @property
    def termination_requested(self) -> bool:
        return self._termination_requested

    def kill_exact_process_group(
        self,
        *,
        expected_argv: Sequence[str],
        expected_pid: int,
    ) -> None:
        """SIGKILL only this still-live, exact argv POSIX session leader."""
        normalized_expected = tuple(_validate_argv(expected_argv))
        if os.name != "posix":
            raise RuntimeError("exact process-group termination requires POSIX")
        if type(expected_pid) is not int or expected_pid <= 0:
            raise ValueError("expected_pid must be a positive integer")
        process_argv = self._process.args
        if isinstance(process_argv, str):
            raise RuntimeError("refusing to terminate a non-argv subprocess")
        if (
            expected_pid != self.pid
            or normalized_expected != self._argv
            or tuple(process_argv) != self._argv
            or self._process.poll() is not None
        ):
            raise RuntimeError("refusing to terminate a mismatched or exited subprocess")
        try:
            process_group_id = os.getpgid(self.pid)
            session_id = os.getsid(self.pid)
        except ProcessLookupError as exc:
            raise RuntimeError("refusing to terminate an exited subprocess") from exc
        if process_group_id != self.pid or session_id != self.pid:
            raise RuntimeError("refusing to terminate a non-leader process group")
        os.killpg(self.pid, signal.SIGKILL)
        self._termination_requested = True


BinaryCommandStartedHook = Callable[[RunningBinaryCommand], None]


def run_command(
    argv: Sequence[str], *, timeout_seconds: float = 30.0, max_output_bytes: int = 65_536,
    env: Mapping[str, str] | None = None, input_text: str | None = None,
    secrets: Sequence[str] = (), cwd: str | None = None,
) -> CommandResult:
    """Run a dispatcher-owned argv without a shell and return redacted bounded output."""
    if input_text is not None and not isinstance(input_text, str):
        raise TypeError("input_text must be a string or None")
    binary = run_binary_command(
        argv,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        env=env,
        input_bytes=input_text.encode("utf-8") if input_text is not None else b"",
        secrets=secrets,
        cwd=cwd,
    )
    stdout = _decode_output(bytearray(binary.stdout), binary.stdout_truncated)
    stderr = _decode_output(bytearray(binary.stderr), binary.stderr_truncated)
    return CommandResult(
        binary.returncode,
        redact_text(stdout, secrets),
        redact_text(stderr, secrets),
        binary.timed_out,
        binary.stdout_truncated,
        binary.stderr_truncated,
        binary.error,
    )


def run_binary_command(
    argv: Sequence[str],
    *,
    timeout_seconds: float = 30.0,
    max_output_bytes: int = 65_536,
    max_stderr_bytes: int | None = None,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes = b"",
    secrets: Sequence[str] = (),
    cwd: str | None = None,
    started_hook: BinaryCommandStartedHook | None = None,
) -> BinaryCommandResult:
    """Run fixed argv and return bounded bytes; callers must never log raw output."""
    normalized_argv = _validate_argv(argv)
    if max_stderr_bytes is None:
        max_stderr_bytes = max_output_bytes
    if timeout_seconds <= 0 or max_output_bytes < 0 or max_stderr_bytes < 0:
        raise ValueError("timeout and output byte limits must be positive or non-negative")
    if not isinstance(input_bytes, bytes):
        raise TypeError("input_bytes must be bytes")
    if started_hook is not None and not callable(started_hook):
        raise TypeError("started_hook must be callable or None")
    command_env = _command_environment(env)
    try:
        process = subprocess.Popen(
            normalized_argv,
            shell=False,
            cwd=cwd,
            env=command_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return BinaryCommandResult(
            None, b"", b"", error=redact_text(str(exc), secrets)
        )

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    stdout_truncated = [False]
    stderr_truncated = [False]
    readers = [
        threading.Thread(
            target=_drain_bounded,
            args=(process.stdout, stdout_buffer, stdout_truncated, max_output_bytes),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_bounded,
            args=(process.stderr, stderr_buffer, stderr_truncated, max_stderr_bytes),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    writer = threading.Thread(
        target=_write_input,
        args=(process.stdin, input_bytes),
        daemon=True,
    )
    writer.start()

    hook_failed = False
    if started_hook is not None:
        try:
            started_hook(RunningBinaryCommand(process, normalized_argv))
        except Exception:
            # A trusted observation hook must not leak its exact child or turn an
            # unproven observation failure into an immediate process kill.  Let
            # the bounded command finish normally, then report ambiguity.
            hook_failed = True
    # The hook has its own bounded proof budget.  Do not let that read-only
    # observation time consume the primary command deadline and accidentally
    # turn a failed proof into an unauthorized timeout kill.
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    try:
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True

    for thread in (*readers, writer):
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    if any(thread.is_alive() for thread in (*readers, writer)):
        timed_out = True
    if timed_out:
        _kill_process_group(process)
        process.wait()
        for thread in (*readers, writer):
            thread.join()

    error = None
    if timed_out:
        error = "command timed out"
    elif hook_failed:
        error = "command start hook failed"
    return BinaryCommandResult(
        None if timed_out else process.returncode,
        bytes(stdout_buffer),
        bytes(stderr_buffer),
        timed_out,
        stdout_truncated[0],
        stderr_truncated[0],
        error,
    )


def _command_environment(env: Mapping[str, str] | None) -> dict[str, str]:
    command_env = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    if env is not None:
        if not isinstance(env, Mapping) or any(
            not isinstance(key, str)
            or not key
            or "=" in key
            or not isinstance(value, str)
            or "\x00" in key
            or "\x00" in value
            for key, value in env.items()
        ):
            raise TypeError("env must be a string-to-string mapping without NUL")
        command_env.update(env)
    return command_env


def _validate_argv(argv: Sequence[str]) -> list[str]:
    if not isinstance(argv, (list, tuple)) or not argv:
        raise TypeError("argv must be a non-empty list or tuple of strings")
    if any(
        not isinstance(argument, str) or not argument or "\x00" in argument
        for argument in argv
    ):
        raise ValueError("argv entries must be non-empty strings without NUL")
    return list(argv)


def _drain_bounded(
    stream: BinaryIO, buffer: bytearray, truncated: list[bool], maximum: int
) -> None:
    try:
        while chunk := stream.read(8192):
            remaining = maximum - len(buffer)
            if remaining > 0:
                buffer.extend(chunk[:remaining])
            if len(chunk) > max(remaining, 0):
                truncated[0] = True
    finally:
        stream.close()


def _write_input(stream: BinaryIO, content: bytes) -> None:
    try:
        if content:
            stream.write(content)
            stream.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        stream.close()


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def _decode_output(value: bytearray, truncated: bool) -> str:
    output = bytes(value).decode("utf-8", errors="replace")
    if not truncated:
        return output
    marker = "\n[output truncated]\n"
    return output + marker
