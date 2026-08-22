"""Durable, idempotent execution of one Codex CLI Turn on the Linux Runner."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from codex_dispatcher.codex_jsonl import (
    MAX_EVENT_BYTES,
    CodexJsonlError,
    CodexTerminalStatus,
    parse_codex_jsonl,
)
from codex_dispatcher.command_runner import BinaryCommandResult, run_binary_command
from codex_dispatcher.executors.codex_cli import (
    build_codex_invocation,
    build_codex_login_status_invocation,
    validate_egress_proxy_url,
)
from codex_dispatcher.executors.codex_docker import (
    AUTH_TIMEOUT_SECONDS,
    DockerCodexRuntime,
    build_docker_codex_plan,
    build_docker_login_status_plan,
)
from codex_dispatcher.runner_docker import (
    DockerWorkItemContext,
    RunnerDockerError,
    bind_docker_session,
    docker_generation_container_is_running,
    load_docker_generation_receipt,
    prepare_docker_work_item,
    validate_docker_auth_state,
    validate_docker_command_boundary,
)
from codex_dispatcher.runner_protocol import (
    NEXT_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    RunnerOperation,
    RunnerProtocolError,
    RunnerRequest,
    agent_result_to_json,
    parse_agent_result,
    parse_runner_request,
)
from codex_dispatcher.runner_policy import PolicyBundle, PolicyBundleError
from codex_dispatcher.runner_transport import (
    RunnerTurnRemoteState,
    RunnerTurnReply,
    parse_runner_turn_reply,
)
from codex_dispatcher.runner_workspace import (
    RunnerWorkspace,
    RunnerWorkspaceError,
    RunnerWorkspacePaths,
)
from codex_dispatcher.work_items import validate_session_id


_TURN_RECORD_VERSION = 1
_MAX_TURN_RECORD_BYTES = 512 * 1024
_CHATGPT_LOGIN_STATUS = b"Logged in using ChatGPT\n"


class RunnerTurnError(RuntimeError):
    """Raised when a Turn request conflicts with durable Runner state."""


@dataclass(frozen=True, slots=True)
class _TurnRecord:
    request: RunnerRequest
    state: str
    reply: RunnerTurnReply | None


class RunnerTurnExecutor:
    """Execute fixed Codex argv synchronously while persisting replay-safe results."""

    def __init__(
        self,
        *,
        workspace: RunnerWorkspace,
        codex_path: Path,
        codex_home: Path,
        output_schema: Path,
        timeout_seconds: float = 3600.0,
        egress_proxy_url: str | None = None,
        docker_runtime: DockerCodexRuntime | None = None,
        policy_bundle: PolicyBundle | None = None,
    ) -> None:
        for path, field in (
            (codex_path, "codex_path"),
            (codex_home, "codex_home"),
            (output_schema, "output_schema"),
        ):
            if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{field} must be a normalized absolute path")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._workspace = workspace
        self._codex_path = codex_path
        self._codex_home = codex_home
        self._output_schema = output_schema
        self._timeout_seconds = timeout_seconds
        self._egress_proxy_url = (
            validate_egress_proxy_url(egress_proxy_url)
            if egress_proxy_url is not None
            else None
        )
        if docker_runtime is not None and not isinstance(
            docker_runtime, DockerCodexRuntime
        ):
            raise TypeError("docker_runtime must be a DockerCodexRuntime or None")
        if policy_bundle is not None and not isinstance(policy_bundle, PolicyBundle):
            raise TypeError("policy_bundle must be a PolicyBundle or None")
        if docker_runtime is None and policy_bundle is not None:
            raise ValueError("policy_bundle requires Docker Codex execution")
        self._docker_runtime = docker_runtime
        self._policy_bundle = policy_bundle

    def execute(self, request: RunnerRequest, prompt: bytes) -> RunnerTurnReply:
        if request.operation not in {RunnerOperation.START, RunnerOperation.RESUME}:
            raise ValueError("request must start or resume a Turn")
        self._require_activated_protocol(request)
        if not isinstance(prompt, bytes) or not prompt:
            raise ValueError("prompt must be non-empty bytes")
        if sha256(prompt).hexdigest() != request.prompt_sha256:
            raise RunnerTurnError("Prompt does not match the Turn request")
        assert request.turn_id is not None
        assert request.input_head_sha is not None
        paths = self._workspace.paths(request.work_item_id)
        record_path = self._record_path(paths.state, request.turn_id)
        existing = self._read_record(record_path, required=False)
        if existing is not None:
            if existing.request != request:
                raise RunnerTurnError("Turn identity conflicts with its durable request")
            if existing.state == "finished":
                assert existing.reply is not None
                return existing.reply
            if request.version == NEXT_PROTOCOL_VERSION:
                return self._v2_executing_reply(request, paths)
            return self._unknown_reply(request, "turn_outcome_unresolved")

        paths = self._workspace.validate_turn_anchor(
            request.work_item_id, request.input_head_sha
        )
        self._write_record(record_path, _TurnRecord(request, "executing", None))
        reply = self._run_codex(request, prompt, paths)
        self._write_record(record_path, _TurnRecord(request, "finished", reply))
        return reply

    def status(self, request: RunnerRequest) -> RunnerTurnReply:
        if request.operation is not RunnerOperation.STATUS:
            raise ValueError("request must be a STATUS operation")
        self._require_activated_protocol(request)
        assert request.turn_id is not None
        paths = self._workspace.paths(request.work_item_id)
        record = self._read_record(
            self._record_path(paths.state, request.turn_id), required=False
        )
        if record is None:
            return self._unknown_reply(request, "turn_not_found")
        if (
            record.request.work_item_id != request.work_item_id
            or record.request.turn_id != request.turn_id
            or record.request.version != request.version
        ):
            raise RunnerTurnError("Turn record identity is inconsistent")
        if request.version == NEXT_PROTOCOL_VERSION and (
            record.request.version != request.version
            or record.request.session_generation_id
            != request.session_generation_id
            or record.request.session_generation != request.session_generation
            or record.request.agent_policy_digest != request.agent_policy_digest
        ):
            raise RunnerTurnError("Turn generation identity is inconsistent")
        if record.state != "finished":
            if request.version == NEXT_PROTOCOL_VERSION:
                return self._v2_executing_reply(request, paths)
            return RunnerTurnReply(
                RunnerOperation.STATUS,
                request.work_item_id,
                request.turn_id,
                RunnerTurnRemoteState.UNKNOWN,
                session_id=record.request.session_id,
                error_code="turn_outcome_unresolved",
            )
        assert record.reply is not None
        return replace(record.reply, operation=RunnerOperation.STATUS)

    def _require_activated_protocol(self, request: RunnerRequest) -> None:
        if request.version == PROTOCOL_VERSION:
            return
        if request.version != NEXT_PROTOCOL_VERSION:
            raise RunnerTurnError("Runner protocol version is not activated")
        if request.operation is RunnerOperation.STOP:
            raise RunnerTurnError("Runner STOP is not activated")
        if self._docker_runtime is None or self._policy_bundle is None:
            raise RunnerTurnError("v2 requires policy-bound Docker execution")
        if request.agent_policy_digest != self._policy_bundle.policy_digest:
            raise RunnerTurnError("v2 agent policy digest does not match Runner policy")
        try:
            self._policy_bundle.validate()
        except PolicyBundleError as exc:
            raise RunnerTurnError("v2 Runner policy is invalid") from exc

    def _v2_executing_reply(
        self,
        request: RunnerRequest,
        paths: RunnerWorkspacePaths,
    ) -> RunnerTurnReply:
        assert self._docker_runtime is not None
        status_request = RunnerRequest(
            RunnerOperation.STATUS,
            request.work_item_id,
            turn_id=request.turn_id,
            session_generation_id=request.session_generation_id,
            session_generation=request.session_generation,
            agent_policy_digest=request.agent_policy_digest,
            version=NEXT_PROTOCOL_VERSION,
        )
        try:
            receipt = load_docker_generation_receipt(
                runtime=self._docker_runtime,
                paths=paths,
                request=status_request,
            )
        except (RunnerDockerError, OSError, ValueError):
            receipt = None
        if receipt is None:
            return self._unknown_reply(request, "turn_outcome_unresolved")
        _, session_id = receipt
        try:
            running = docker_generation_container_is_running(
                runtime=self._docker_runtime,
                request=request,
            )
        except (RunnerDockerError, OSError, ValueError):
            running = False
        if not running:
            return self._unknown_reply(
                request,
                "turn_outcome_unresolved",
                session_id=session_id,
            )
        assert request.turn_id is not None
        return RunnerTurnReply(
            request.operation,
            request.work_item_id,
            request.turn_id,
            RunnerTurnRemoteState.RUNNING,
            session_id=session_id,
            **self._v2_reply_fields(request),
        )

    def _run_codex(
        self,
        request: RunnerRequest,
        prompt: bytes,
        paths: RunnerWorkspacePaths,
    ) -> RunnerTurnReply:
        assert request.turn_id is not None
        if self._docker_runtime is not None:
            return self._run_codex_in_docker(request, prompt, paths)
        if not self._chatgpt_authentication_is_ready():
            return self._failed_reply(request, "codex_auth_invalid")
        plan = build_codex_invocation(
            codex_path=self._codex_path,
            repository_directory=paths.repository,
            codex_home=self._codex_home,
            output_schema=self._output_schema,
            session_id=request.session_id,
            egress_proxy_url=self._egress_proxy_url,
        )
        command = run_binary_command(
            plan.argv,
            timeout_seconds=self._timeout_seconds,
            max_output_bytes=4 * 1024 * 1024,
            max_stderr_bytes=256 * 1024,
            env=plan.environment,
            input_bytes=prompt,
            cwd=str(plan.cwd),
        )
        if (
            command.timed_out
            or command.error is not None
            or command.stdout_truncated
            or command.stderr_truncated
            or command.returncode is None
        ):
            return self._failed_reply(request, "codex_process_failed")
        try:
            summary = parse_codex_jsonl(
                command.stdout,
                expected_session_id=request.session_id,
            )
        except CodexJsonlError as exc:
            return self._failed_reply(request, f"codex_output_{exc.code}")
        if command.returncode != 0 or summary.status is not CodexTerminalStatus.COMPLETED:
            return self._failed_reply(
                request,
                "codex_turn_failed",
                session_id=summary.session_id,
            )
        assert summary.final_message is not None
        try:
            result = parse_agent_result(summary.final_message)
        except RunnerProtocolError:
            return self._failed_reply(
                request,
                "agent_result_invalid",
                session_id=summary.session_id,
            )
        try:
            head_sha = self._workspace.current_head(
                request.work_item_id, require_clean=True
            )
        except RunnerWorkspaceError:
            return self._failed_reply(
                request,
                "checkpoint_invalid",
                session_id=summary.session_id,
            )
        canonical = agent_result_to_json(result)
        return RunnerTurnReply(
            request.operation,
            request.work_item_id,
            request.turn_id,
            RunnerTurnRemoteState.FINISHED,
            session_id=summary.session_id,
            head_sha=head_sha,
            output_sha256=sha256(canonical.encode("utf-8")).hexdigest(),
            result=result,
        )

    def _run_codex_in_docker(
        self,
        request: RunnerRequest,
        prompt: bytes,
        paths: RunnerWorkspacePaths,
    ) -> RunnerTurnReply:
        assert self._docker_runtime is not None
        assert request.turn_id is not None
        is_v2 = request.version == NEXT_PROTOCOL_VERSION
        active_policy = self._policy_bundle if is_v2 else None
        auth_file = self._codex_home / "auth.json"
        receipt_hook: _ThreadStartedReceiptHook | None = None
        try:
            if active_policy is not None:
                active_policy.validate()
            context = prepare_docker_work_item(
                runtime=self._docker_runtime,
                paths=paths,
                request=request,
                auth_file=auth_file,
                output_schema=self._output_schema,
            )
            if not self._docker_authentication_is_ready(
                request, context, auth_file=auth_file
            ):
                return self._failed_reply(request, "codex_auth_invalid")
            validate_docker_command_boundary(
                runtime=self._docker_runtime,
                context=context,
                codex_path=self._codex_path,
                output_schema=self._output_schema,
            )
            if request.operation is RunnerOperation.RESUME:
                assert request.session_id is not None
                bind_docker_session(
                    context,
                    work_item_id=request.work_item_id,
                    session_id=request.session_id,
                )
            plan = build_docker_codex_plan(
                runtime=self._docker_runtime,
                work_item_id=request.work_item_id,
                turn_id=request.turn_id,
                codex_path=self._codex_path,
                repository=context.repository,
                codex_home=context.codex_home,
                auth_file=context.auth_file,
                output_schema=self._output_schema,
                session_id=request.session_id,
                timeout_seconds=self._timeout_seconds,
                policy_bundle=active_policy,
                session_generation_id=(
                    request.session_generation_id if is_v2 else None
                ),
                session_generation=request.session_generation if is_v2 else None,
                agent_policy_digest=request.agent_policy_digest if is_v2 else None,
            )
            if is_v2:
                receipt_hook = _ThreadStartedReceiptHook(request, context)
            command = run_binary_command(
                plan.argv,
                timeout_seconds=self._timeout_seconds + 15.0,
                max_output_bytes=4 * 1024 * 1024,
                max_stderr_bytes=256 * 1024,
                env=plan.environment,
                input_bytes=prompt,
                cwd=str(context.state),
                stdout_line_hook=receipt_hook,
            )
        except (RunnerDockerError, PolicyBundleError, OSError, ValueError):
            return self._failed_reply(request, "docker_boundary_invalid")
        if command.timed_out:
            raise RunnerTurnError("Docker Turn outcome is unresolved")
        if is_v2:
            assert receipt_hook is not None
            return self._finish_v2_docker_command(
                request=request,
                command=command,
                context=context,
                receipt_hook=receipt_hook,
            )
        try:
            validate_docker_auth_state(context)
        except RunnerDockerError:
            return self._failed_reply(request, "docker_boundary_invalid")
        if (
            command.error is not None
            or command.stdout_truncated
            or command.stderr_truncated
            or command.returncode is None
        ):
            return self._failed_reply(request, "codex_process_failed")
        try:
            summary = parse_codex_jsonl(
                command.stdout,
                expected_session_id=request.session_id,
            )
        except CodexJsonlError as exc:
            return self._failed_reply(request, f"codex_output_{exc.code}")
        try:
            bind_docker_session(
                context,
                work_item_id=request.work_item_id,
                session_id=summary.session_id,
            )
        except RunnerDockerError:
            return self._failed_reply(
                request,
                "codex_session_binding_failed",
                session_id=summary.session_id,
            )
        if command.returncode != 0 or summary.status is not CodexTerminalStatus.COMPLETED:
            return self._failed_reply(
                request,
                "codex_turn_failed",
                session_id=summary.session_id,
            )
        assert summary.final_message is not None
        try:
            result = parse_agent_result(summary.final_message)
        except RunnerProtocolError:
            return self._failed_reply(
                request,
                "agent_result_invalid",
                session_id=summary.session_id,
            )
        try:
            head_sha = self._workspace.current_head(
                request.work_item_id, require_clean=True
            )
        except RunnerWorkspaceError:
            return self._failed_reply(
                request,
                "checkpoint_invalid",
                session_id=summary.session_id,
            )
        canonical = agent_result_to_json(result)
        return RunnerTurnReply(
            request.operation,
            request.work_item_id,
            request.turn_id,
            RunnerTurnRemoteState.FINISHED,
            session_id=summary.session_id,
            head_sha=head_sha,
            output_sha256=sha256(canonical.encode("utf-8")).hexdigest(),
            result=result,
        )

    def _finish_v2_docker_command(
        self,
        *,
        request: RunnerRequest,
        command: BinaryCommandResult,
        context: DockerWorkItemContext,
        receipt_hook: _ThreadStartedReceiptHook,
    ) -> RunnerTurnReply:
        assert self._policy_bundle is not None
        try:
            validate_docker_auth_state(context)
            self._policy_bundle.validate()
        except (RunnerDockerError, PolicyBundleError):
            return self._failed_reply(
                request,
                "docker_boundary_invalid",
                session_id=receipt_hook.session_id,
            )
        if (
            command.stdout_truncated
            or command.stderr_truncated
            or command.returncode is None
        ):
            return self._failed_reply(
                request,
                "codex_process_failed",
                session_id=receipt_hook.session_id,
            )
        try:
            summary = parse_codex_jsonl(
                command.stdout,
                expected_session_id=request.session_id,
            )
        except CodexJsonlError as exc:
            return self._failed_reply(
                request,
                f"codex_output_{exc.code}",
                session_id=receipt_hook.session_id,
            )
        try:
            bind_docker_session(
                context,
                work_item_id=request.work_item_id,
                session_id=summary.session_id,
            )
        except RunnerDockerError:
            return self._failed_reply(
                request,
                "codex_session_binding_failed",
                session_id=summary.session_id,
            )
        if (
            receipt_hook.session_id is not None
            and receipt_hook.session_id != summary.session_id
        ):
            return self._failed_reply(
                request,
                "codex_session_binding_failed",
                session_id=summary.session_id,
            )
        if command.error not in {None, "command stdout hook failed"}:
            return self._failed_reply(
                request,
                "codex_process_failed",
                session_id=summary.session_id,
            )
        if command.returncode != 0 or summary.status is not (
            CodexTerminalStatus.COMPLETED
        ):
            return self._failed_reply(
                request,
                "codex_turn_failed",
                session_id=summary.session_id,
            )
        if summary.usage is None:
            return self._failed_reply(
                request,
                "codex_output_usage_missing",
                session_id=summary.session_id,
            )
        assert summary.final_message is not None
        try:
            result = parse_agent_result(summary.final_message)
        except RunnerProtocolError:
            return self._failed_reply(
                request,
                "agent_result_invalid",
                session_id=summary.session_id,
            )
        try:
            head_sha = self._workspace.current_head(
                request.work_item_id,
                require_clean=True,
            )
        except RunnerWorkspaceError:
            return self._failed_reply(
                request,
                "checkpoint_invalid",
                session_id=summary.session_id,
            )
        canonical = agent_result_to_json(result)
        assert request.turn_id is not None
        return RunnerTurnReply(
            request.operation,
            request.work_item_id,
            request.turn_id,
            RunnerTurnRemoteState.FINISHED,
            session_id=summary.session_id,
            head_sha=head_sha,
            output_sha256=sha256(canonical.encode("utf-8")).hexdigest(),
            result=result,
            usage=summary.usage,
            **self._v2_reply_fields(request),
        )

    def _docker_authentication_is_ready(
        self,
        request: RunnerRequest,
        context: DockerWorkItemContext,
        *,
        auth_file: Path,
    ) -> bool:
        assert self._docker_runtime is not None
        validate_docker_command_boundary(
            runtime=self._docker_runtime,
            context=context,
            codex_path=self._codex_path,
            output_schema=self._output_schema,
        )
        plan = build_docker_login_status_plan(
            runtime=self._docker_runtime,
            work_item_id=request.work_item_id,
            codex_path=self._codex_path,
            codex_home=context.codex_home,
            auth_file=context.auth_file,
        )
        command = run_binary_command(
            plan.argv,
            timeout_seconds=AUTH_TIMEOUT_SECONDS + 10.0,
            max_output_bytes=4096,
            max_stderr_bytes=4096,
            env=plan.environment,
            cwd=str(context.state),
        )
        if command.timed_out:
            raise RunnerTurnError("Docker authentication outcome is unresolved")
        return (
            not command.timed_out
            and command.error is None
            and not command.stdout_truncated
            and not command.stderr_truncated
            and command.returncode == 0
            and command.stdout == b""
            and command.stderr == _CHATGPT_LOGIN_STATUS
        )

    def _chatgpt_authentication_is_ready(self) -> bool:
        plan = build_codex_login_status_invocation(
            codex_path=self._codex_path,
            codex_home=self._codex_home,
            egress_proxy_url=self._egress_proxy_url,
        )
        command = run_binary_command(
            plan.argv,
            timeout_seconds=min(30.0, self._timeout_seconds),
            max_output_bytes=4096,
            max_stderr_bytes=4096,
            env=plan.environment,
        )
        return (
            not command.timed_out
            and command.error is None
            and not command.stdout_truncated
            and not command.stderr_truncated
            and command.returncode == 0
            and command.stdout == b""
            and command.stderr == _CHATGPT_LOGIN_STATUS
        )

    @classmethod
    def _failed_reply(
        cls,
        request: RunnerRequest,
        error_code: str,
        *,
        session_id: str | None = None,
    ) -> RunnerTurnReply:
        assert request.turn_id is not None
        return RunnerTurnReply(
            request.operation,
            request.work_item_id,
            request.turn_id,
            RunnerTurnRemoteState.FAILED,
            session_id=session_id,
            error_code=error_code,
            **cls._v2_reply_fields(request),
        )

    @classmethod
    def _unknown_reply(
        cls,
        request: RunnerRequest,
        error_code: str,
        *,
        session_id: str | None = None,
    ) -> RunnerTurnReply:
        assert request.turn_id is not None
        return RunnerTurnReply(
            request.operation,
            request.work_item_id,
            request.turn_id,
            RunnerTurnRemoteState.UNKNOWN,
            session_id=session_id if session_id is not None else request.session_id,
            error_code=error_code,
            **cls._v2_reply_fields(request),
        )

    @staticmethod
    def _v2_reply_fields(request: RunnerRequest) -> dict[str, object]:
        if request.version != NEXT_PROTOCOL_VERSION:
            return {}
        return {
            "session_generation_id": request.session_generation_id,
            "session_generation": request.session_generation,
            "agent_policy_digest": request.agent_policy_digest,
            "version": request.version,
        }

    @staticmethod
    def _record_path(state_directory: Path, turn_id: str) -> Path:
        directory = state_directory / "turns"
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise RunnerTurnError("Turn record directory is invalid")
        return directory / f"{turn_id}.json"

    def _read_record(self, path: Path, *, required: bool) -> _TurnRecord | None:
        if not path.exists():
            if required:
                raise RunnerTurnError("Turn record is unavailable")
            return None
        try:
            record_stat = path.lstat()
            raw = path.read_bytes()
        except OSError as exc:
            raise RunnerTurnError("Turn record is unavailable") from exc
        if (
            not stat.S_ISREG(record_stat.st_mode)
            or record_stat.st_mode & 0o022
            or not raw
            or len(raw) > _MAX_TURN_RECORD_BYTES
            or b"\x00" in raw
        ):
            raise RunnerTurnError("Turn record exceeds its safe boundary")
        try:
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise RunnerTurnError("Turn record is malformed") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "request",
            "state",
            "reply",
        }:
            raise RunnerTurnError("Turn record fields are invalid")
        if payload["version"] != _TURN_RECORD_VERSION or payload["state"] not in {
            "executing",
            "finished",
        }:
            raise RunnerTurnError("Turn record version or state is invalid")
        if not isinstance(payload["request"], dict):
            raise RunnerTurnError("Turn record request is invalid")
        request = parse_runner_request(
            json.dumps(
                payload["request"],
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        if request.operation not in {RunnerOperation.START, RunnerOperation.RESUME}:
            raise RunnerTurnError("Turn record operation is invalid")
        reply: RunnerTurnReply | None = None
        if payload["state"] == "executing":
            if payload["reply"] is not None:
                raise RunnerTurnError("executing Turn record contains a reply")
        else:
            if not isinstance(payload["reply"], str):
                raise RunnerTurnError("finished Turn record is missing its reply")
            reply = parse_runner_turn_reply(payload["reply"])
            if (
                reply.operation is not request.operation
                or reply.work_item_id != request.work_item_id
                or reply.turn_id != request.turn_id
                or reply.version != request.version
                or reply.session_generation_id != request.session_generation_id
                or reply.session_generation != request.session_generation
                or reply.agent_policy_digest != request.agent_policy_digest
            ):
                raise RunnerTurnError("Turn record reply identity is invalid")
        return _TurnRecord(request, payload["state"], reply)

    def _write_record(self, path: Path, record: _TurnRecord) -> None:
        payload = {
            "version": _TURN_RECORD_VERSION,
            "request": record.request.to_mapping(),
            "state": record.state,
            "reply": record.reply.to_json() if record.reply is not None else None,
        }
        encoded = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise RunnerTurnError("Turn record could not be persisted") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class _ThreadStartedReceiptHook:
    """Persist the first valid streamed v2 session identity before Turn exit."""

    __slots__ = ("_context", "_request", "session_id")

    def __init__(
        self,
        request: RunnerRequest,
        context: DockerWorkItemContext,
    ) -> None:
        self._request = request
        self._context = context
        self.session_id: str | None = None

    def __call__(self, raw_line: bytes) -> None:
        if (
            not isinstance(raw_line, bytes)
            or not raw_line
            or len(raw_line) > MAX_EVENT_BYTES
            or b"\x00" in raw_line
        ):
            raise RunnerTurnError("streamed Codex event exceeds its safe boundary")
        try:
            event = json.loads(
                raw_line.decode("utf-8"),
                object_pairs_hook=_unique_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise RunnerTurnError("streamed Codex event is malformed") from exc
        if not isinstance(event, dict):
            raise RunnerTurnError("streamed Codex event is not an object")
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type or len(event_type) > 128:
            raise RunnerTurnError("streamed Codex event type is invalid")
        if event_type != "thread.started":
            return
        if self.session_id is not None:
            raise RunnerTurnError("streamed Codex session identity is duplicated")
        try:
            session_id = validate_session_id(event.get("thread_id"))
        except ValueError as exc:
            raise RunnerTurnError(
                "streamed Codex session identity is invalid"
            ) from exc
        if (
            self._request.session_id is not None
            and session_id != self._request.session_id
        ):
            raise RunnerTurnError("streamed Codex session identity conflicts")
        bind_docker_session(
            self._context,
            work_item_id=self._request.work_item_id,
            session_id=session_id,
        )
        self.session_id = session_id


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate Turn record field")
        result[key] = value
    return result
