"""Offline orchestration of persistent Runner Turns and publication checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from codex_dispatcher.bundle_verification import BundleVerifier
from codex_dispatcher.prompt_builder import PromptSnapshot
from codex_dispatcher.publisher import (
    PublicationPlan,
    PublishRequest,
    plan_publication,
)
from codex_dispatcher.runner_protocol import (
    AgentResultStatus,
    RunnerOperation,
    RunnerProtocolError,
    RunnerRequest,
)
from codex_dispatcher.runner_transport import (
    RunnerTransport,
    RunnerTransportInterrupted,
    RunnerTransportRejected,
    RunnerTurnRemoteState,
    RunnerTurnReply,
    parse_runner_ack,
    parse_runner_export_reply,
    parse_runner_turn_reply,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.work_items import (
    Turn,
    TurnState,
    WorkItem,
    WorkItemState,
    validate_git_sha,
)


MAX_TURN_PROMPT_BYTES = 1024 * 1024


class TurnOrchestrationError(RuntimeError):
    """Raised when trusted orchestration inputs are inconsistent."""


@dataclass(frozen=True, slots=True)
class TurnProgress:
    work_item: WorkItem
    turn: Turn

    @property
    def checkpoint_ready(self) -> bool:
        return self.turn.state is TurnState.CHECKPOINTING


class OfflineTurnOrchestrator:
    """Drive the fixed Runner port without network- or provider-specific behavior."""

    def __init__(
        self,
        *,
        store: StateStore,
        transport: RunnerTransport,
        bundle_verifier: BundleVerifier,
    ) -> None:
        self._store = store
        self._transport = transport
        self._bundle_verifier = bundle_verifier

    def prepare_work_item(self, work_item_id: str, *, source_bundle: bytes) -> WorkItem:
        if not isinstance(source_bundle, bytes) or not source_bundle:
            raise ValueError("source_bundle must be non-empty bytes")
        work_item = self._require_work_item(work_item_id)
        if work_item.state is WorkItemState.READY:
            return work_item
        if work_item.state is WorkItemState.DISCOVERED:
            work_item = self._store.update_work_item_state(
                work_item_id, WorkItemState.PREPARING
            )
        elif work_item.state is not WorkItemState.PREPARING:
            raise TurnOrchestrationError("WorkItem is not eligible for Runner preparation")
        request = RunnerRequest(
            RunnerOperation.PREPARE,
            work_item.work_item_id,
            repository=work_item.repository,
            issue_number=work_item.issue_number,
            task_branch=work_item.task_branch,
            base_sha=work_item.base_sha,
            source_bundle_sha256=sha256(source_bundle).hexdigest(),
            source_bundle_size=len(source_bundle),
        )
        try:
            output = self._transport.invoke(request, source_artifact=source_bundle)
            ack = parse_runner_ack(output.payload)
        except RunnerTransportInterrupted:
            # Prepare is idempotent. Leave PREPARING so the next sweep can safely retry it.
            return self._require_work_item(work_item_id)
        except RunnerTransportRejected:
            return self._store.update_work_item_state(work_item_id, WorkItemState.BLOCKED)
        if (
            ack.operation is not RunnerOperation.PREPARE
            or ack.work_item_id != work_item.work_item_id
            or output.artifact is not None
        ):
            raise RunnerProtocolError("Runner preparation acknowledgement identity is invalid")
        return self._store.update_work_item_state(work_item_id, WorkItemState.READY)

    def run_turn(
        self,
        work_item_id: str,
        *,
        issue_revision: str,
        prompt: PromptSnapshot,
        expected_turn_number: int | None = None,
        turn_id: str | None = None,
    ) -> TurnProgress:
        prompt_bytes = self._validate_prompt(prompt)
        work_item = self._require_work_item(work_item_id)
        input_head_sha = work_item.last_published_sha or work_item.base_sha
        work_item, turn = self._store.begin_turn(
            work_item_id,
            issue_revision=issue_revision,
            prompt_sha256=prompt.sha256,
            input_head_sha=input_head_sha,
            included_comment_ids=prompt.included_comment_ids,
            expected_turn_number=expected_turn_number,
            turn_id=turn_id,
        )
        turn = self._store.update_turn_state(turn.turn_id, TurnState.STARTING)
        if work_item.codex_session_id is None:
            operation = RunnerOperation.START
            request = RunnerRequest(
                operation,
                work_item_id,
                turn_id=turn.turn_id,
                prompt_sha256=prompt.sha256,
                input_head_sha=input_head_sha,
            )
        else:
            operation = RunnerOperation.RESUME
            request = RunnerRequest(
                operation,
                work_item_id,
                turn_id=turn.turn_id,
                session_id=work_item.codex_session_id,
                prompt_sha256=prompt.sha256,
                input_head_sha=input_head_sha,
            )
        try:
            output = self._transport.invoke(request, stdin=prompt_bytes)
        except RunnerTransportInterrupted:
            return self._mark_reconciling(turn.turn_id)
        except RunnerTransportRejected:
            return self._finalize_blocked(
                turn.turn_id, error_code="runner_request_rejected"
            )
        if output.artifact is not None:
            return self._finalize_blocked(
                turn.turn_id, error_code="runner_unexpected_artifact"
            )
        try:
            reply = parse_runner_turn_reply(output.payload)
        except RunnerProtocolError:
            return self._mark_reconciling(turn.turn_id)
        return self._apply_turn_reply(turn.turn_id, operation, reply)

    def reconcile_turn(self, turn_id: str) -> TurnProgress:
        turn = self._require_turn(turn_id)
        if turn.state not in {
            TurnState.STARTING,
            TurnState.RUNNING,
            TurnState.RECONCILING,
        }:
            raise TurnOrchestrationError("Turn is not eligible for Runner reconciliation")
        request = RunnerRequest(
            RunnerOperation.STATUS,
            turn.work_item_id,
            turn_id=turn.turn_id,
        )
        try:
            output = self._transport.invoke(request)
        except (RunnerTransportInterrupted, RunnerTransportRejected):
            return self._mark_reconciling(turn_id)
        if output.artifact is not None:
            return self._mark_reconciling(turn_id)
        try:
            reply = parse_runner_turn_reply(output.payload)
        except RunnerProtocolError:
            return self._mark_reconciling(turn_id)
        return self._apply_turn_reply(turn_id, RunnerOperation.STATUS, reply)

    def prepare_publication(
        self,
        turn_id: str,
        *,
        issue_allowed_paths: tuple[str, ...],
        repository_allowed_paths: tuple[str, ...],
        repository_denied_paths: tuple[str, ...] = (),
    ) -> PublicationPlan:
        turn = self._require_turn(turn_id)
        if turn.state is not TurnState.CHECKPOINTING or turn.output_head_sha is None:
            raise TurnOrchestrationError("Turn does not have a checkpoint ready for publication")
        work_item = self._require_work_item(turn.work_item_id)
        request = RunnerRequest(
            RunnerOperation.EXPORT,
            work_item.work_item_id,
            expected_head_sha=turn.output_head_sha,
        )
        output = self._transport.invoke(request)
        manifest = parse_runner_export_reply(output.payload)
        if (
            manifest.work_item_id != work_item.work_item_id
            or manifest.head_sha != turn.output_head_sha
        ):
            raise RunnerProtocolError("Runner export identity does not match the checkpoint")
        artifact = manifest.validate_artifact(output.artifact)
        bundle = self._bundle_verifier.verify(
            artifact,
            manifest=manifest,
            work_item=work_item,
        )
        if (
            bundle.bundle_sha256 != manifest.bundle_sha256
            or bundle.head_sha != manifest.head_sha
            or bundle.size_bytes != manifest.size_bytes
        ):
            raise RunnerProtocolError("verified bundle conflicts with the Runner export manifest")
        return plan_publication(
            request=PublishRequest(work_item.work_item_id, turn.output_head_sha),
            work_item=work_item,
            bundle=bundle,
            issue_allowed_paths=issue_allowed_paths,
            repository_allowed_paths=repository_allowed_paths,
            repository_denied_paths=repository_denied_paths,
        )

    def complete_publication(
        self,
        turn_id: str,
        *,
        plan: PublicationPlan,
        observed_remote_sha: str,
    ) -> TurnProgress:
        if not isinstance(plan, PublicationPlan):
            raise TypeError("plan must be a PublicationPlan")
        observed_remote_sha = validate_git_sha(observed_remote_sha, "observed_remote_sha")
        turn = self._require_turn(turn_id)
        work_item = self._require_work_item(turn.work_item_id)
        expected_ref = f"refs/heads/{work_item.task_branch}"
        if (
            plan.work_item_id != work_item.work_item_id
            or plan.repository != work_item.repository
            or plan.source_sha != turn.output_head_sha
            or plan.target_ref != expected_ref
            or plan.force
            or plan.delete
            or observed_remote_sha != plan.source_sha
        ):
            raise TurnOrchestrationError("publication evidence does not match the checkpoint")

        terminal_mapping = {
            TurnState.FINISHED: WorkItemState.REVIEW,
            TurnState.NEEDS_INPUT: WorkItemState.WAITING_INPUT,
            TurnState.BLOCKED: WorkItemState.BLOCKED,
        }
        if turn.state in terminal_mapping:
            if (
                work_item.state is terminal_mapping[turn.state]
                and work_item.last_published_sha == plan.source_sha
            ):
                return TurnProgress(work_item, turn)
            raise TurnOrchestrationError("terminal publication state is inconsistent")
        if turn.state not in {TurnState.CHECKPOINTING, TurnState.PUBLISHED}:
            raise TurnOrchestrationError("Turn is not awaiting publication completion")

        if turn.state is TurnState.CHECKPOINTING:
            if work_item.last_published_sha != plan.source_sha:
                if plan.expected_remote_sha != work_item.last_published_sha:
                    raise TurnOrchestrationError("publication anchor changed before read-back")
                previous_sha = work_item.last_published_sha or work_item.base_sha
                work_item = self._store.record_published_sha(
                    work_item.work_item_id,
                    previous_sha=previous_sha,
                    head_sha=plan.source_sha,
                )
            turn = self._store.update_turn_state(turn.turn_id, TurnState.PUBLISHED)
        return self._finalize_recorded_result(turn.turn_id)

    def _apply_turn_reply(
        self,
        turn_id: str,
        expected_operation: RunnerOperation,
        reply: RunnerTurnReply,
    ) -> TurnProgress:
        turn = self._require_turn(turn_id)
        work_item = self._require_work_item(turn.work_item_id)
        if (
            reply.operation is not expected_operation
            or reply.work_item_id != work_item.work_item_id
            or reply.turn_id != turn.turn_id
        ):
            return self._finalize_blocked(
                turn_id, error_code="runner_reply_identity_invalid"
            )
        if reply.state is RunnerTurnRemoteState.UNKNOWN:
            return self._mark_reconciling(turn_id)
        if reply.session_id is not None:
            try:
                work_item = self._store.bind_codex_session(
                    work_item.work_item_id, reply.session_id
                )
            except ValueError:
                return self._finalize_blocked(
                    turn_id, error_code="session_binding_conflict"
                )
        if reply.state is RunnerTurnRemoteState.FAILED:
            assert reply.error_code is not None
            return self._finalize_blocked(turn_id, error_code=reply.error_code)
        if reply.state is RunnerTurnRemoteState.RUNNING:
            turn = self._store.update_turn_state(turn_id, TurnState.RUNNING)
            return TurnProgress(work_item, turn)

        assert reply.result is not None
        assert reply.output_sha256 is not None
        assert reply.head_sha is not None
        turn = self._store.record_turn_result(
            turn_id,
            output_sha256=reply.output_sha256,
            output_head_sha=reply.head_sha,
            result_status=reply.result.status.value,
            result_summary=reply.result.summary,
        )
        if reply.head_sha != turn.input_head_sha:
            turn = self._store.update_turn_state(turn_id, TurnState.CHECKPOINTING)
            return TurnProgress(work_item, turn)
        return self._finalize_recorded_result(turn_id)

    def _finalize_recorded_result(self, turn_id: str) -> TurnProgress:
        turn = self._require_turn(turn_id)
        mapping = {
            AgentResultStatus.COMPLETED.value: (
                TurnState.FINISHED,
                WorkItemState.REVIEW,
            ),
            AgentResultStatus.NEEDS_INPUT.value: (
                TurnState.NEEDS_INPUT,
                WorkItemState.WAITING_INPUT,
            ),
            AgentResultStatus.BLOCKED.value: (
                TurnState.BLOCKED,
                WorkItemState.BLOCKED,
            ),
        }
        try:
            turn_state, work_item_state = mapping[turn.result_status]
        except KeyError as exc:
            raise TurnOrchestrationError("Turn does not have a recorded Agent result") from exc
        work_item, turn = self._store.finalize_turn(
            turn_id,
            turn_state=turn_state,
            work_item_state=work_item_state,
        )
        return TurnProgress(work_item, turn)

    def _mark_reconciling(self, turn_id: str) -> TurnProgress:
        turn = self._require_turn(turn_id)
        if turn.state is not TurnState.RECONCILING:
            turn = self._store.update_turn_state(turn_id, TurnState.RECONCILING)
        return TurnProgress(self._require_work_item(turn.work_item_id), turn)

    def _finalize_blocked(
        self, turn_id: str, *, error_code: str | None = None
    ) -> TurnProgress:
        if error_code is not None:
            self._store.record_turn_error(turn_id, error_code=error_code)
        work_item, turn = self._store.finalize_turn(
            turn_id,
            turn_state=TurnState.BLOCKED,
            work_item_state=WorkItemState.BLOCKED,
        )
        return TurnProgress(work_item, turn)

    def _require_work_item(self, work_item_id: str) -> WorkItem:
        work_item = self._store.get_work_item(work_item_id)
        if work_item is None:
            raise KeyError(f"work item not found: {work_item_id}")
        return work_item

    def _require_turn(self, turn_id: str) -> Turn:
        turn = self._store.get_turn(turn_id)
        if turn is None:
            raise KeyError(f"turn not found: {turn_id}")
        return turn

    @staticmethod
    def _validate_prompt(prompt: PromptSnapshot) -> bytes:
        if not isinstance(prompt, PromptSnapshot):
            raise TypeError("prompt must be a PromptSnapshot")
        encoded = prompt.content.encode("utf-8")
        if not encoded or len(encoded) > MAX_TURN_PROMPT_BYTES or b"\x00" in encoded:
            raise ValueError("prompt exceeds its safe input boundary")
        if sha256(encoded).hexdigest() != prompt.sha256:
            raise ValueError("prompt content does not match its persisted hash")
        return encoded
