"""Fixed, single-request service behind the ``codex-runner-v1`` SSH command."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from codex_dispatcher.runner_protocol import (
    MAX_INPUT_ARTIFACT_BYTES,
    MAX_REQUEST_BYTES,
    RunnerOperation,
)
from codex_dispatcher.runner_transport import RunnerTransportRejected, RunnerWireOutput
from codex_dispatcher.runner_turns import RunnerTurnError, RunnerTurnExecutor
from codex_dispatcher.runner_wire import (
    MAX_PROMPT_BYTES,
    decode_runner_input,
    encode_runner_output,
)
from codex_dispatcher.runner_workspace import RunnerWorkspace, RunnerWorkspaceError


MAX_RUNNER_INPUT_FRAME_BYTES = (
    MAX_REQUEST_BYTES + MAX_PROMPT_BYTES + MAX_INPUT_ARTIFACT_BYTES + 256
)


class LinuxRunnerService:
    """Dispatch the closed Runner operation set; no caller-controlled command exists."""

    def __init__(
        self,
        *,
        workspace: RunnerWorkspace,
        turns: RunnerTurnExecutor,
        active_lock_path: Path,
    ) -> None:
        if (
            not isinstance(active_lock_path, Path)
            or not active_lock_path.is_absolute()
            or ".." in active_lock_path.parts
        ):
            raise ValueError("active_lock_path must be a normalized absolute path")
        self._workspace = workspace
        self._turns = turns
        self._active_lock_path = active_lock_path

    def handle_frame(self, frame: bytes) -> bytes:
        request, prompt, source_artifact = decode_runner_input(frame)
        try:
            if request.operation is RunnerOperation.PREPARE:
                assert source_artifact is not None
                with self._active_turn_lock():
                    ack = self._workspace.prepare(request, source_artifact)
                output = RunnerWireOutput(ack.to_json().encode("utf-8"))
            elif request.operation in {RunnerOperation.START, RunnerOperation.RESUME}:
                with self._active_turn_lock():
                    reply = self._turns.execute(request, prompt)
                output = RunnerWireOutput(reply.to_json().encode("utf-8"))
            elif request.operation is RunnerOperation.STATUS:
                reply = self._turns.status(request)
                output = RunnerWireOutput(reply.to_json().encode("utf-8"))
            elif request.operation is RunnerOperation.EXPORT:
                output = self._workspace.export(request)
            elif request.operation is RunnerOperation.ARCHIVE:
                with self._active_turn_lock():
                    if self._workspace.archive_requires_safety_preflight(request):
                        self._turns.assert_archive_safe(request.work_item_id)
                    reply = self._workspace.archive(request)
                output = RunnerWireOutput(reply.to_json().encode("utf-8"))
            elif request.operation is RunnerOperation.ARCHIVE_STATUS:
                with self._active_turn_lock():
                    reply = self._workspace.archive_status(request)
                output = RunnerWireOutput(reply.to_json().encode("utf-8"))
            elif request.operation is RunnerOperation.PROVE_ABSENCE:
                with self._active_turn_lock():
                    reply = self._workspace.prove_absence(request)
                output = RunnerWireOutput(reply.to_json().encode("utf-8"))
            else:
                raise RunnerTransportRejected("Runner operation is disabled")
        except (RunnerWorkspaceError, RunnerTurnError) as exc:
            raise RunnerTransportRejected("Runner definitively rejected the request") from exc
        return encode_runner_output(output)

    @contextmanager
    def _active_turn_lock(self) -> Iterator[None]:
        self._active_lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._active_lock_path.open("a+b") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RunnerTransportRejected("another Codex Turn is active") from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def serve_one(
    service: LinuxRunnerService,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
) -> None:
    """Read one bounded frame and write one response; intended for a forced SSH command."""
    frame = input_stream.read(MAX_RUNNER_INPUT_FRAME_BYTES + 1)
    if not frame or len(frame) > MAX_RUNNER_INPUT_FRAME_BYTES:
        raise RunnerTransportRejected("Runner input frame exceeds its safe boundary")
    response = service.handle_frame(frame)
    output_stream.write(response)
    output_stream.flush()
