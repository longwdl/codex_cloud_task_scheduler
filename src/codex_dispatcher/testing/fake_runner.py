"""Stateful offline fake for the fixed SSH Runner transport contract."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from hashlib import sha256

from codex_dispatcher.publisher import VerifiedBundle
from codex_dispatcher.codex_jsonl import CodexTurnUsage
from codex_dispatcher.runner_protocol import (
    AgentResult,
    NEXT_PROTOCOL_VERSION,
    RunnerOperation,
    RunnerRequest,
    agent_result_to_json,
)
from codex_dispatcher.runner_transport import (
    RunnerAck,
    RunnerExportReply,
    RunnerTransportInterrupted,
    RunnerTransportRejected,
    RunnerTurnRemoteState,
    RunnerTurnReply,
    RunnerWireOutput,
)
from codex_dispatcher.runner_wire import (
    decode_runner_input,
    decode_runner_output,
    encode_runner_input,
    encode_runner_output,
)
from codex_dispatcher.work_items import WorkItem


@dataclass(frozen=True, slots=True)
class FakeRunnerCall:
    operation: RunnerOperation
    work_item_id: str
    turn_id: str | None
    stdin_sha256: str | None
    stdin_size: int
    version: int = 1
    session_generation_id: str | None = None
    session_generation: int | None = None
    agent_policy_digest: str | None = None


@dataclass(frozen=True, slots=True)
class FakeTurnFixture:
    session_id: str
    head_sha: str
    result: AgentResult
    artifact: bytes | None = None
    usage: CodexTurnUsage = CodexTurnUsage(0, 0, 0, 0, 0)


@dataclass(slots=True)
class _RunnerGeneration:
    session_generation_id: str
    generation_number: int
    policy_digest: str
    session_id: str


@dataclass(slots=True)
class _RunnerWorkItem:
    repository: str
    issue_number: int
    task_branch: str
    base_sha: str
    head_sha: str
    session_id: str | None = None
    generations: dict[int, _RunnerGeneration] = field(default_factory=dict)


class FakeSshRunnerTransport:
    """Execute Runner requests in memory without retaining prompt contents."""

    def __init__(self) -> None:
        self.calls: list[FakeRunnerCall] = []
        self._work_items: dict[str, _RunnerWorkItem] = {}
        self._fixtures: dict[str, deque[FakeTurnFixture]] = defaultdict(deque)
        self._turn_replies: dict[tuple[str, str], RunnerTurnReply] = {}
        self._artifacts: dict[tuple[str, str], bytes] = {}
        self._interrupt_after_effect: set[RunnerOperation] = set()
        self._reject_before_effect: set[RunnerOperation] = set()

    def queue_turn(self, work_item_id: str, fixture: FakeTurnFixture) -> None:
        self._fixtures[work_item_id].append(fixture)

    def interrupt_next(self, operation: RunnerOperation) -> None:
        self._interrupt_after_effect.add(operation)

    def reject_next(self, operation: RunnerOperation) -> None:
        self._reject_before_effect.add(operation)

    def invoke(
        self,
        request: RunnerRequest,
        *,
        stdin: bytes = b"",
        source_artifact: bytes | None = None,
    ) -> RunnerWireOutput:
        if not isinstance(request, RunnerRequest):
            raise TypeError("request must be a RunnerRequest")
        if not isinstance(stdin, bytes):
            raise TypeError("stdin must be bytes")
        # Exercise the exact byte frame and JSON parser even though this fake is in-process.
        request, stdin, source_artifact = decode_runner_input(
            encode_runner_input(
                request,
                prompt=stdin,
                source_artifact=source_artifact,
            )
        )
        needs_prompt = request.operation in {RunnerOperation.START, RunnerOperation.RESUME}
        if needs_prompt != bool(stdin):
            raise RunnerTransportRejected("Runner prompt framing is invalid")
        prompt_digest = sha256(stdin).hexdigest() if stdin else None
        self.calls.append(
            FakeRunnerCall(
                request.operation,
                request.work_item_id,
                request.turn_id,
                prompt_digest,
                len(stdin),
                request.version,
                request.session_generation_id,
                request.session_generation,
                request.agent_policy_digest,
            )
        )

        if request.operation in self._reject_before_effect:
            self._reject_before_effect.remove(request.operation)
            raise RunnerTransportRejected("fake Runner definitively rejected the request")

        if request.operation is RunnerOperation.PREPARE:
            assert source_artifact is not None
            output = self._prepare(request)
        elif request.operation in {RunnerOperation.START, RunnerOperation.RESUME}:
            if prompt_digest != request.prompt_sha256:
                raise RunnerTransportRejected("Runner prompt hash does not match the request")
            output = self._execute_turn(request)
        elif request.operation is RunnerOperation.STATUS:
            output = self._status(request)
        elif request.operation is RunnerOperation.EXPORT:
            output = self._export(request)
        else:
            raise RunnerTransportRejected("fake Runner operation is not enabled")

        if request.operation in self._interrupt_after_effect:
            self._interrupt_after_effect.remove(request.operation)
            raise RunnerTransportInterrupted("fake SSH connection interrupted after request effect")
        return decode_runner_output(encode_runner_output(output))

    def _prepare(self, request: RunnerRequest) -> RunnerWireOutput:
        assert request.repository is not None
        assert request.issue_number is not None
        assert request.task_branch is not None
        assert request.base_sha is not None
        current = self._work_items.get(request.work_item_id)
        identity = (
            request.repository,
            request.issue_number,
            request.task_branch,
            request.base_sha,
        )
        if current is None:
            self._work_items[request.work_item_id] = _RunnerWorkItem(
                *identity, head_sha=request.base_sha
            )
        elif (
            current.repository,
            current.issue_number,
            current.task_branch,
            current.base_sha,
        ) != identity:
            raise RunnerTransportRejected("fake Runner WorkItem identity conflict")
        reply = RunnerAck(RunnerOperation.PREPARE, request.work_item_id)
        return RunnerWireOutput(reply.to_json().encode("utf-8"))

    def _execute_turn(self, request: RunnerRequest) -> RunnerWireOutput:
        assert request.turn_id is not None
        assert request.input_head_sha is not None
        current = self._work_items.get(request.work_item_id)
        if current is None:
            raise RunnerTransportRejected("fake Runner WorkItem is not prepared")
        if request.input_head_sha != current.head_sha:
            raise RunnerTransportRejected("fake Runner input HEAD conflict")
        fixtures = self._fixtures[request.work_item_id]
        if not fixtures:
            raise RunnerTransportRejected("fake Runner has no configured Turn result")
        fixture = fixtures.popleft()
        if request.version == NEXT_PROTOCOL_VERSION:
            assert request.session_generation_id is not None
            assert request.session_generation is not None
            assert request.agent_policy_digest is not None
            highest = max(current.generations, default=0)
            if request.operation is RunnerOperation.START:
                if request.session_id is not None or request.session_generation <= highest:
                    raise RunnerTransportRejected(
                        "fake Runner cannot start an old or existing generation"
                    )
                generation = _RunnerGeneration(
                    request.session_generation_id,
                    request.session_generation,
                    request.agent_policy_digest,
                    fixture.session_id,
                )
                current.generations[request.session_generation] = generation
            else:
                generation = current.generations.get(request.session_generation)
                if (
                    generation is None
                    or request.session_generation != highest
                    or generation.session_generation_id
                    != request.session_generation_id
                    or generation.policy_digest != request.agent_policy_digest
                    or request.session_id != generation.session_id
                    or fixture.session_id != generation.session_id
                ):
                    raise RunnerTransportRejected(
                        "fake Runner resume generation conflict"
                    )
            session_id = generation.session_id
        else:
            if request.operation is RunnerOperation.START:
                if current.session_id is not None or request.session_id is not None:
                    raise RunnerTransportRejected(
                        "fake Runner cannot start a replacement session"
                    )
                current.session_id = fixture.session_id
            elif (
                current.session_id is None
                or request.session_id != current.session_id
                or fixture.session_id != current.session_id
            ):
                raise RunnerTransportRejected("fake Runner resume session conflict")
            session_id = current.session_id
        result_json = agent_result_to_json(fixture.result)
        reply = RunnerTurnReply(
            operation=request.operation,
            work_item_id=request.work_item_id,
            turn_id=request.turn_id,
            state=RunnerTurnRemoteState.FINISHED,
            session_id=session_id,
            head_sha=fixture.head_sha,
            output_sha256=sha256(result_json.encode("utf-8")).hexdigest(),
            result=fixture.result,
            version=request.version,
            session_generation_id=request.session_generation_id,
            session_generation=request.session_generation,
            agent_policy_digest=request.agent_policy_digest,
            usage=(
                fixture.usage
                if request.version == NEXT_PROTOCOL_VERSION
                else None
            ),
        )
        current.head_sha = fixture.head_sha
        self._turn_replies[(request.work_item_id, request.turn_id)] = reply
        if fixture.artifact is not None:
            self._artifacts[(request.work_item_id, fixture.head_sha)] = fixture.artifact
        return RunnerWireOutput(reply.to_json().encode("utf-8"))

    def _status(self, request: RunnerRequest) -> RunnerWireOutput:
        assert request.turn_id is not None
        reply = self._turn_replies.get((request.work_item_id, request.turn_id))
        if reply is None:
            observed = RunnerTurnReply(
                operation=RunnerOperation.STATUS,
                work_item_id=request.work_item_id,
                turn_id=request.turn_id,
                state=RunnerTurnRemoteState.UNKNOWN,
                error_code="turn_not_found",
            )
        else:
            observed = RunnerTurnReply(
                operation=RunnerOperation.STATUS,
                work_item_id=reply.work_item_id,
                turn_id=reply.turn_id,
                state=reply.state,
                session_id=reply.session_id,
                head_sha=reply.head_sha,
                output_sha256=reply.output_sha256,
                result=reply.result,
                error_code=reply.error_code,
                version=reply.version,
                session_generation_id=reply.session_generation_id,
                session_generation=reply.session_generation,
                agent_policy_digest=reply.agent_policy_digest,
                usage=reply.usage,
            )
        if reply is None and request.version == NEXT_PROTOCOL_VERSION:
            observed = RunnerTurnReply(
                operation=RunnerOperation.STATUS,
                work_item_id=request.work_item_id,
                turn_id=request.turn_id,
                state=RunnerTurnRemoteState.UNKNOWN,
                error_code="turn_not_found",
                version=request.version,
                session_generation_id=request.session_generation_id,
                session_generation=request.session_generation,
                agent_policy_digest=request.agent_policy_digest,
            )
        return RunnerWireOutput(observed.to_json().encode("utf-8"))

    def _export(self, request: RunnerRequest) -> RunnerWireOutput:
        assert request.expected_head_sha is not None
        current = self._work_items.get(request.work_item_id)
        if current is None or current.head_sha != request.expected_head_sha:
            raise RunnerTransportRejected("fake Runner export HEAD conflict")
        artifact = self._artifacts.get((request.work_item_id, request.expected_head_sha))
        if artifact is None:
            raise RunnerTransportRejected("fake Runner has no checkpoint artifact")
        reply = RunnerExportReply(
            work_item_id=request.work_item_id,
            head_sha=request.expected_head_sha,
            bundle_sha256=sha256(artifact).hexdigest(),
            size_bytes=len(artifact),
        )
        return RunnerWireOutput(reply.to_json().encode("utf-8"), artifact)


@dataclass(frozen=True, slots=True)
class FakeVerificationCall:
    bundle_sha256: str
    work_item_id: str


class FakeBundleVerifier:
    def __init__(self) -> None:
        self.calls: list[FakeVerificationCall] = []
        self._results: dict[str, VerifiedBundle] = {}

    def register(self, bundle: VerifiedBundle) -> None:
        self._results[bundle.bundle_sha256] = bundle

    def verify(
        self,
        artifact: bytes,
        *,
        manifest: RunnerExportReply,
        work_item: WorkItem,
    ) -> VerifiedBundle:
        manifest.validate_artifact(artifact)
        self.calls.append(FakeVerificationCall(manifest.bundle_sha256, work_item.work_item_id))
        result = self._results.get(manifest.bundle_sha256)
        if result is None:
            raise ValueError("fake quarantine verifier has no configured result")
        if (
            result.bundle_sha256 != manifest.bundle_sha256
            or result.head_sha != manifest.head_sha
            or result.size_bytes != manifest.size_bytes
        ):
            raise ValueError("fake quarantine verification evidence conflicts with manifest")
        return result
