"""Offline orchestration of persistent Runner Turns and publication checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from codex_dispatcher.bundle_verification import BundleVerifier
from codex_dispatcher.prompt_builder import CanonicalInputSnapshot, PromptSnapshot
from codex_dispatcher.publisher import (
    PublicationPlan,
    PublishRequest,
    plan_publication,
)
from codex_dispatcher.runner_protocol import (
    AgentResultStatus,
    NEXT_PROTOCOL_VERSION,
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
    PromptKind,
    SessionGeneration,
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


@dataclass(frozen=True, slots=True)
class PreparedPublication:
    artifact: bytes
    plan: PublicationPlan


class PublicationReceiptLike(Protocol):
    observed_remote_sha: str


class TaskBranchPublisher(Protocol):
    def publish(
        self,
        artifact: bytes,
        *,
        plan: PublicationPlan,
        work_item: WorkItem,
    ) -> PublicationReceiptLike: ...


class PublicationRecordedHook(Protocol):
    """Fixture-only boundary after the publication anchor is durably recorded."""

    def __call__(self, work_item: WorkItem, turn: Turn) -> None: ...


class OfflineTurnOrchestrator:
    """Drive the fixed Runner port without network- or provider-specific behavior."""

    def __init__(
        self,
        *,
        store: StateStore,
        transport: RunnerTransport,
        bundle_verifier: BundleVerifier,
        publication_recorded_hook: PublicationRecordedHook | None = None,
    ) -> None:
        self._store = store
        self._transport = transport
        self._bundle_verifier = bundle_verifier
        self._publication_recorded_hook = publication_recorded_hook

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
        issue_allowed_paths: tuple[str, ...],
        expected_turn_number: int | None = None,
        turn_id: str | None = None,
    ) -> TurnProgress:
        prompt_bytes = self._validate_prompt(prompt)
        if not issue_allowed_paths:
            raise ValueError("issue_allowed_paths must be frozen before starting a Turn")
        work_item = self._require_work_item(work_item_id)
        input_head_sha = work_item.last_published_sha or work_item.base_sha
        work_item, turn = self._store.begin_turn(
            work_item_id,
            issue_revision=issue_revision,
            prompt_sha256=prompt.sha256,
            input_head_sha=input_head_sha,
            included_comment_ids=prompt.included_comment_ids,
            issue_allowed_paths=issue_allowed_paths,
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

    def run_generation_turn(
        self,
        work_item_id: str,
        *,
        session_generation: SessionGeneration,
        issue_revision: str,
        prompt: PromptSnapshot,
        prompt_kind: PromptKind,
        inputs: CanonicalInputSnapshot,
        issue_allowed_paths: tuple[str, ...],
        handoff_id: str | None = None,
        pre_session_retry_without_handoff: bool = False,
        expected_turn_number: int | None = None,
        turn_id: str | None = None,
    ) -> TurnProgress:
        """Run one protocol-v2 Turn bound to an exact session generation."""
        prompt_bytes = self._validate_prompt(prompt)
        if not isinstance(session_generation, SessionGeneration):
            raise TypeError("session_generation must be a SessionGeneration")
        if not isinstance(prompt_kind, PromptKind):
            raise TypeError("prompt_kind must be a PromptKind")
        if not isinstance(inputs, CanonicalInputSnapshot):
            raise TypeError("inputs must be a CanonicalInputSnapshot")
        if not issue_allowed_paths:
            raise ValueError("issue_allowed_paths must be frozen before starting a Turn")
        if not isinstance(pre_session_retry_without_handoff, bool):
            raise TypeError("pre_session_retry_without_handoff must be a bool")
        if session_generation.work_item_id != work_item_id:
            raise TurnOrchestrationError(
                "session generation does not belong to the requested WorkItem"
            )
        if session_generation.policy_sha256 is None:
            raise TurnOrchestrationError(
                "protocol-v2 session generation has no policy digest"
            )
        work_item = self._require_work_item(work_item_id)
        input_head_sha = work_item.last_published_sha or work_item.base_sha
        work_item, generation, turn, _ = (
            self._store.begin_session_generation_turn(
                work_item_id,
                session_generation_id=session_generation.session_generation_id,
                generation_number=session_generation.generation_number,
                policy_sha256=session_generation.policy_sha256,
                prompt_kind=prompt_kind,
                issue_revision=issue_revision,
                issue_content_sha256=inputs.issue_content_sha256,
                task_spec_sha256=inputs.task_spec_sha256,
                prompt_sha256=prompt.sha256,
                approved_comment_ids=prompt.included_comment_ids,
                approved_context_sha256=inputs.approved_context_sha256,
                issue_allowed_paths=issue_allowed_paths,
                input_head_sha=input_head_sha,
                handoff_id=handoff_id,
                pre_session_retry_without_handoff=(
                    pre_session_retry_without_handoff
                ),
                expected_turn_number=expected_turn_number,
                turn_id=turn_id,
            )
        )
        common_request = {
            "version": NEXT_PROTOCOL_VERSION,
            "turn_id": turn.turn_id,
            "prompt_sha256": prompt.sha256,
            "input_head_sha": input_head_sha,
            "session_generation_id": generation.session_generation_id,
            "session_generation": generation.generation_number,
            "agent_policy_digest": generation.policy_sha256,
        }
        if generation.codex_session_id is None:
            operation = RunnerOperation.START
            request = RunnerRequest(operation, work_item_id, **common_request)
        else:
            operation = RunnerOperation.RESUME
            request = RunnerRequest(
                operation,
                work_item_id,
                session_id=generation.codex_session_id,
                **common_request,
            )
        try:
            output = self._transport.invoke(request, stdin=prompt_bytes)
        except RunnerTransportInterrupted:
            generation, turn = self._store.record_generation_turn_unknown(
                turn.turn_id,
                session_generation_id=generation.session_generation_id,
                generation_number=generation.generation_number,
                policy_sha256=generation.policy_sha256,
            )
            return TurnProgress(self._require_work_item(work_item_id), turn)
        except RunnerTransportRejected:
            work_item, _, turn = self._store.record_generation_turn_failed(
                turn.turn_id,
                session_generation_id=generation.session_generation_id,
                generation_number=generation.generation_number,
                policy_sha256=generation.policy_sha256,
                error_code="runner_request_rejected",
            )
            return TurnProgress(work_item, turn)
        if output.artifact is not None:
            work_item, _, turn = self._store.record_generation_turn_failed(
                turn.turn_id,
                session_generation_id=generation.session_generation_id,
                generation_number=generation.generation_number,
                policy_sha256=generation.policy_sha256,
                error_code="runner_unexpected_artifact",
            )
            return TurnProgress(work_item, turn)
        try:
            reply = parse_runner_turn_reply(output.payload)
        except RunnerProtocolError:
            _, turn = self._store.record_generation_turn_unknown(
                turn.turn_id,
                session_generation_id=generation.session_generation_id,
                generation_number=generation.generation_number,
                policy_sha256=generation.policy_sha256,
            )
            return TurnProgress(self._require_work_item(work_item_id), turn)
        return self._apply_generation_turn_reply(
            turn.turn_id,
            expected_operation=operation,
            generation=generation,
            reply=reply,
        )

    def reconcile_turn(self, turn_id: str) -> TurnProgress:
        turn = self._require_turn(turn_id)
        if turn.state not in {
            TurnState.STARTING,
            TurnState.RUNNING,
            TurnState.RECONCILING,
        }:
            raise TurnOrchestrationError("Turn is not eligible for Runner reconciliation")
        generation = self._store.get_turn_session_generation(turn_id)
        is_v2_generation = (
            generation is not None and generation.policy_sha256 is not None
        )
        if not is_v2_generation:
            request = RunnerRequest(
                RunnerOperation.STATUS,
                turn.work_item_id,
                turn_id=turn.turn_id,
            )
        else:
            assert generation is not None
            assert generation.policy_sha256 is not None
            request = RunnerRequest(
                RunnerOperation.STATUS,
                turn.work_item_id,
                version=NEXT_PROTOCOL_VERSION,
                turn_id=turn.turn_id,
                session_generation_id=generation.session_generation_id,
                session_generation=generation.generation_number,
                agent_policy_digest=generation.policy_sha256,
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
        if is_v2_generation:
            assert generation is not None
            return self._apply_generation_turn_reply(
                turn_id,
                expected_operation=RunnerOperation.STATUS,
                generation=generation,
                reply=reply,
            )
        return self._apply_turn_reply(turn_id, RunnerOperation.STATUS, reply)

    def prepare_publication(
        self,
        turn_id: str,
        *,
        issue_allowed_paths: tuple[str, ...] | None = None,
        repository_allowed_paths: tuple[str, ...],
        repository_denied_paths: tuple[str, ...] = (),
    ) -> PublicationPlan:
        """Build a plan from the path policy frozen with the Turn."""
        turn = self._require_turn(turn_id)
        if (
            issue_allowed_paths is not None
            and issue_allowed_paths != turn.issue_allowed_paths
        ):
            raise TurnOrchestrationError(
                "publication path policy differs from the frozen Turn policy"
            )
        prepared = self.prepare_publication_checkpoint(
            turn_id,
            repository_allowed_paths=repository_allowed_paths,
            repository_denied_paths=repository_denied_paths,
        )
        return prepared.plan

    def prepare_publication_checkpoint(
        self,
        turn_id: str,
        *,
        repository_allowed_paths: tuple[str, ...],
        repository_denied_paths: tuple[str, ...] = (),
    ) -> PreparedPublication:
        turn = self._require_turn(turn_id)
        if turn.state is not TurnState.CHECKPOINTING or turn.output_head_sha is None:
            raise TurnOrchestrationError("Turn does not have a checkpoint ready for publication")
        if not turn.issue_allowed_paths:
            raise TurnOrchestrationError(
                "Turn does not contain a frozen Issue path policy"
            )
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
        plan = plan_publication(
            request=PublishRequest(work_item.work_item_id, turn.output_head_sha),
            work_item=work_item,
            bundle=bundle,
            issue_allowed_paths=turn.issue_allowed_paths,
            repository_allowed_paths=repository_allowed_paths,
            repository_denied_paths=repository_denied_paths,
        )
        return PreparedPublication(artifact, plan)

    def publish_checkpoint(
        self,
        turn_id: str,
        *,
        publisher: TaskBranchPublisher,
        repository_allowed_paths: tuple[str, ...],
        repository_denied_paths: tuple[str, ...] = (),
    ) -> TurnProgress:
        """Recover or publish exactly one checkpoint using only its frozen policy."""
        recovered = self.recover_recorded_publication(turn_id)
        if recovered is not None:
            return recovered
        prepared = self.prepare_publication_checkpoint(
            turn_id,
            repository_allowed_paths=repository_allowed_paths,
            repository_denied_paths=repository_denied_paths,
        )
        work_item = self._require_work_item(prepared.plan.work_item_id)
        receipt = publisher.publish(
            prepared.artifact,
            plan=prepared.plan,
            work_item=work_item,
        )
        return self.complete_publication(
            turn_id,
            plan=prepared.plan,
            observed_remote_sha=receipt.observed_remote_sha,
        )

    def recover_recorded_publication(self, turn_id: str) -> TurnProgress | None:
        """Finish a publication whose verified remote SHA was already committed locally."""
        turn = self._require_turn(turn_id)
        if turn.state not in {TurnState.CHECKPOINTING, TurnState.PUBLISHED}:
            raise TurnOrchestrationError("Turn is not awaiting publication recovery")
        if turn.output_head_sha is None:
            raise TurnOrchestrationError("Turn publication checkpoint has no output HEAD")
        work_item = self._require_work_item(turn.work_item_id)
        generation = self._store.get_turn_session_generation(turn_id)
        generation_recorded = (
            generation is None
            or generation.last_published_sha == turn.output_head_sha
        )
        if (
            work_item.last_published_sha != turn.output_head_sha
            or not generation_recorded
        ):
            if turn.state is TurnState.PUBLISHED:
                raise TurnOrchestrationError(
                    "published Turn conflicts with its recorded publication anchors"
                )
            return None
        if turn.state is TurnState.CHECKPOINTING:
            turn = self._store.update_turn_state(turn.turn_id, TurnState.PUBLISHED)
        return self._finalize_recorded_result(turn.turn_id)

    def reject_publication(self, turn_id: str) -> TurnProgress:
        """Terminalize one mechanically rejected checkpoint with a bounded error code."""
        turn = self._require_turn(turn_id)
        if turn.state not in {TurnState.CHECKPOINTING, TurnState.PUBLISHED}:
            raise TurnOrchestrationError("Turn is not awaiting publication rejection")
        return self._finalize_blocked(
            turn_id,
            error_code="publication_rejected",
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
            bound_generation = self._store.get_turn_session_generation(turn_id)
            if (
                work_item.last_published_sha == plan.source_sha
                and bound_generation is not None
                and bound_generation.last_published_sha != plan.source_sha
            ):
                raise TurnOrchestrationError(
                    "WorkItem and generation publication anchors diverged"
                )
            if work_item.last_published_sha != plan.source_sha:
                if plan.expected_remote_sha != work_item.last_published_sha:
                    raise TurnOrchestrationError("publication anchor changed before read-back")
                previous_sha = work_item.last_published_sha or work_item.base_sha
                if bound_generation is None:
                    work_item = self._store.record_published_sha(
                        work_item.work_item_id,
                        previous_sha=previous_sha,
                        head_sha=plan.source_sha,
                    )
                else:
                    if plan.commit_count is None or plan.size_bytes is None:
                        raise TurnOrchestrationError(
                            "generation publication plan has incomplete evidence"
                        )
                    work_item, _ = self._store.record_generation_publication(
                        turn_id,
                        previous_sha=previous_sha,
                        head_sha=plan.source_sha,
                        bundle_sha256=plan.bundle_sha256,
                        changed_paths=plan.changed_paths,
                        commit_count=plan.commit_count,
                        size_bytes=plan.size_bytes,
                    )
                if self._publication_recorded_hook is not None:
                    self._publication_recorded_hook(work_item, turn)
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

    def _apply_generation_turn_reply(
        self,
        turn_id: str,
        *,
        expected_operation: RunnerOperation,
        generation: SessionGeneration,
        reply: RunnerTurnReply,
    ) -> TurnProgress:
        """Apply one exact v2 reply through the generation transaction boundary."""
        turn = self._require_turn(turn_id)
        work_item = self._require_work_item(turn.work_item_id)
        if generation.policy_sha256 is None:
            raise TurnOrchestrationError("session generation has no policy digest")
        identity_is_valid = (
            reply.version == NEXT_PROTOCOL_VERSION
            and reply.operation is expected_operation
            and reply.work_item_id == work_item.work_item_id
            and reply.turn_id == turn.turn_id
            and reply.session_generation_id == generation.session_generation_id
            and reply.session_generation == generation.generation_number
            and reply.agent_policy_digest == generation.policy_sha256
        )
        identity = {
            "session_generation_id": generation.session_generation_id,
            "generation_number": generation.generation_number,
            "policy_sha256": generation.policy_sha256,
        }
        if not identity_is_valid:
            work_item, _, turn = self._store.record_generation_turn_failed(
                turn_id,
                **identity,
                error_code="runner_reply_identity_invalid",
            )
            return TurnProgress(work_item, turn)
        if reply.state is RunnerTurnRemoteState.UNKNOWN:
            _, turn = self._store.record_generation_turn_unknown(
                turn_id,
                **identity,
                session_id=reply.session_id,
            )
            return TurnProgress(self._require_work_item(work_item.work_item_id), turn)
        if reply.state is RunnerTurnRemoteState.FAILED:
            assert reply.error_code is not None
            work_item, _, turn = self._store.record_generation_turn_failed(
                turn_id,
                **identity,
                error_code=reply.error_code,
                session_id=reply.session_id,
            )
            return TurnProgress(work_item, turn)
        assert reply.session_id is not None
        if reply.state is RunnerTurnRemoteState.RUNNING:
            _, turn = self._store.record_generation_turn_running(
                turn_id,
                **identity,
                session_id=reply.session_id,
            )
            return TurnProgress(self._require_work_item(work_item.work_item_id), turn)

        assert reply.result is not None
        assert reply.output_sha256 is not None
        assert reply.head_sha is not None
        assert reply.usage is not None
        work_item, _, turn, _ = self._store.record_generation_turn_finished(
            turn_id,
            **identity,
            session_id=reply.session_id,
            output_sha256=reply.output_sha256,
            output_head_sha=reply.head_sha,
            result_status=reply.result.status.value,
            result_summary=reply.result.summary,
            agent_result=reply.result,
            input_tokens=reply.usage.input_tokens,
            cached_input_tokens=reply.usage.cached_input_tokens,
            cache_write_input_tokens=reply.usage.cache_write_input_tokens,
            output_tokens=reply.usage.output_tokens,
            reasoning_output_tokens=reply.usage.reasoning_output_tokens,
            delegation_receipt=reply.delegation_receipt,
        )
        return TurnProgress(work_item, turn)

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
