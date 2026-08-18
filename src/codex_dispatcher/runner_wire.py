"""Length-prefixed byte framing for one fixed SSH Runner request and response."""

from __future__ import annotations

from hashlib import sha256

from codex_dispatcher.runner_protocol import (
    MAX_REQUEST_BYTES,
    MAX_INPUT_ARTIFACT_BYTES,
    RunnerOperation,
    RunnerProtocolError,
    RunnerRequest,
    parse_runner_request,
)
from codex_dispatcher.runner_transport import (
    MAX_ARTIFACT_BYTES,
    MAX_RESPONSE_BYTES,
    RunnerWireOutput,
)


MAX_PROMPT_BYTES = 1024 * 1024
_REQUEST_MAGIC = b"CODEX-RUNNER-REQUEST/1\n"
_RESPONSE_MAGIC = b"CODEX-RUNNER-RESPONSE/1\n"
_MAX_LENGTH_LINE_BYTES = 20


def encode_runner_input(
    request: RunnerRequest,
    *,
    prompt: bytes = b"",
    source_artifact: bytes | None = None,
) -> bytes:
    if not isinstance(request, RunnerRequest):
        raise TypeError("request must be a RunnerRequest")
    prompt, artifact = _validate_input_for_operation(request, prompt, source_artifact)
    request_bytes = request.to_json().encode("utf-8")
    return b"".join(
        (
            _REQUEST_MAGIC,
            str(len(request_bytes)).encode("ascii"),
            b"\n",
            str(len(prompt)).encode("ascii"),
            b"\n",
            str(len(artifact)).encode("ascii"),
            b"\n",
            request_bytes,
            prompt,
            artifact,
        )
    )


def decode_runner_input(value: bytes) -> tuple[RunnerRequest, bytes, bytes | None]:
    if not isinstance(value, bytes):
        raise TypeError("Runner input frame must be bytes")
    if not value.startswith(_REQUEST_MAGIC):
        raise RunnerProtocolError("Runner frame magic or version is invalid")
    offset = len(_REQUEST_MAGIC)
    request_size, offset = _read_length(value, offset)
    prompt_size, offset = _read_length(value, offset)
    artifact_size, offset = _read_length(value, offset)
    body = value[offset:]
    if request_size <= 0 or request_size > MAX_REQUEST_BYTES:
        raise RunnerProtocolError("Runner framed request size is invalid")
    if prompt_size < 0 or prompt_size > MAX_PROMPT_BYTES:
        raise RunnerProtocolError("Runner framed prompt size is invalid")
    if artifact_size < 0 or artifact_size > MAX_INPUT_ARTIFACT_BYTES:
        raise RunnerProtocolError("Runner framed source artifact size is invalid")
    if len(body) != request_size + prompt_size + artifact_size:
        raise RunnerProtocolError("Runner input frame length does not match its header")
    request = parse_runner_request(body[:request_size])
    prompt_end = request_size + prompt_size
    prompt = body[request_size:prompt_end]
    artifact = body[prompt_end:] or None
    prompt, source_artifact = _validate_input_for_operation(request, prompt, artifact)
    if prompt and sha256(prompt).hexdigest() != request.prompt_sha256:
        raise RunnerProtocolError("Runner framed prompt hash does not match the request")
    return request, prompt, source_artifact or None


def encode_runner_output(output: RunnerWireOutput) -> bytes:
    if not isinstance(output, RunnerWireOutput):
        raise TypeError("output must be a RunnerWireOutput")
    artifact = output.artifact or b""
    return b"".join(
        (
            _RESPONSE_MAGIC,
            str(len(output.payload)).encode("ascii"),
            b"\n",
            str(len(artifact)).encode("ascii"),
            b"\n",
            output.payload,
            artifact,
        )
    )


def decode_runner_output(value: bytes) -> RunnerWireOutput:
    if not isinstance(value, bytes):
        raise TypeError("Runner output frame must be bytes")
    payload_size, artifact_size, body = _decode_header(value, _RESPONSE_MAGIC)
    if payload_size <= 0 or payload_size > MAX_RESPONSE_BYTES:
        raise RunnerProtocolError("Runner framed response size is invalid")
    if artifact_size < 0 or artifact_size > MAX_ARTIFACT_BYTES:
        raise RunnerProtocolError("Runner framed artifact size is invalid")
    if len(body) != payload_size + artifact_size:
        raise RunnerProtocolError("Runner output frame length does not match its header")
    artifact = body[payload_size:] or None
    return RunnerWireOutput(body[:payload_size], artifact)


def _validate_input_for_operation(
    request: RunnerRequest,
    prompt: bytes,
    source_artifact: bytes | None,
) -> tuple[bytes, bytes]:
    if not isinstance(prompt, bytes):
        raise TypeError("prompt must be bytes")
    if source_artifact is not None and not isinstance(source_artifact, bytes):
        raise TypeError("source_artifact must be bytes or None")
    artifact = source_artifact or b""
    needs_prompt = request.operation in {RunnerOperation.START, RunnerOperation.RESUME}
    needs_artifact = request.operation is RunnerOperation.PREPARE
    if needs_prompt != bool(prompt):
        raise RunnerProtocolError("Runner operation has an invalid Prompt frame")
    if needs_artifact != bool(artifact):
        raise RunnerProtocolError("Runner operation has an invalid source artifact frame")
    if len(prompt) > MAX_PROMPT_BYTES or b"\x00" in prompt:
        raise RunnerProtocolError("Runner Prompt exceeds its safe byte boundary")
    if len(artifact) > MAX_INPUT_ARTIFACT_BYTES:
        raise RunnerProtocolError("Runner source artifact exceeds its safe byte boundary")
    if artifact and (
        len(artifact) != request.source_bundle_size
        or sha256(artifact).hexdigest() != request.source_bundle_sha256
    ):
        raise RunnerProtocolError("Runner source artifact does not match the request")
    return prompt, artifact


def _decode_header(value: bytes, magic: bytes) -> tuple[int, int, bytes]:
    if not value.startswith(magic):
        raise RunnerProtocolError("Runner frame magic or version is invalid")
    offset = len(magic)
    first, offset = _read_length(value, offset)
    second, offset = _read_length(value, offset)
    return first, second, value[offset:]


def _read_length(value: bytes, offset: int) -> tuple[int, int]:
    end = value.find(b"\n", offset, offset + _MAX_LENGTH_LINE_BYTES + 1)
    if end < 0:
        raise RunnerProtocolError("Runner frame length header is invalid")
    raw = value[offset:end]
    if not raw or len(raw) > _MAX_LENGTH_LINE_BYTES or not raw.isdigit():
        raise RunnerProtocolError("Runner frame length header is invalid")
    return int(raw), end + 1
