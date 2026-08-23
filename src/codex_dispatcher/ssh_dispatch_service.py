"""Provider-independent coordination for one claimed SSH CLI Issue Turn."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable
from uuid import uuid4

from codex_dispatcher.ci_evidence import CiEvidenceError, CiEvidenceImporter
from codex_dispatcher.completion_gate import (
    CompletionGateStatus,
    build_completion_gate_snapshot,
)
from codex_dispatcher.config import Config, RepositoryConfig
from codex_dispatcher.git_bundle_verifier import GitBundleVerificationError
from codex_dispatcher.git_publisher import (
    GitPublicationInterrupted,
    GitPublicationRejected,
)
from codex_dispatcher.publisher import PublicationError
from codex_dispatcher.runner_protocol import RunnerProtocolError
from codex_dispatcher.runner_transport import (
    RunnerTransportInterrupted,
    RunnerTransportRejected,
)
from codex_dispatcher.scheduler import DryRunPlan, build_ssh_dry_run_plan
from codex_dispatcher.handoffs import build_publication_evidence
from codex_dispatcher.prompt_builder import build_canonical_input_snapshot
from codex_dispatcher.source_bundle import SourceBundle
from codex_dispatcher.ssh_dispatch_planning import (
    SshDispatchPlanningError,
    WorkItemAction,
    build_ssh_generation_turn_plan,
    build_ssh_session_handoff_snapshot,
    build_ssh_turn_plan,
    resolve_ssh_work_item,
    validate_ssh_active_generation_continuity,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.task_spec import TaskSpecError, parse_task_spec
from codex_dispatcher.trackers.base import Tracker, TrackerTask
from codex_dispatcher.turn_orchestration import (
    OfflineTurnOrchestrator,
    TaskBranchPublisher,
    TurnOrchestrationError,
    TurnProgress,
)
from codex_dispatcher.work_items import (
    PRE_SESSION_RETRY_ROTATION_REASON,
    SessionGeneration,
    SessionGenerationRole,
    SessionGenerationState,
    Turn,
    TurnState,
    WorkItem,
    WorkItemState,
)
from codex_dispatcher.work_item_lifecycle import (
    WorkItemAbsenceReconciliation,
    WorkItemArchive,
)


class OfflineSshDispatchService:
    """Join selection, WorkItem recovery, Prompt planning, and the fixed Runner port.

    GitHub claim/state writes and Slack delivery remain outside this service.
    Callers pass the verified post-claim Issue snapshot and an explicit fixed
    Publisher port for checkpoint completion.
    """

    def __init__(
        self,
        *,
        config: Config,
        store: StateStore,
        orchestrator: OfflineTurnOrchestrator,
        ci_evidence_importer: CiEvidenceImporter | None = None,
    ) -> None:
        if not isinstance(config, Config):
            raise TypeError("config must be a Config")
        if ci_evidence_importer is not None and not callable(
            getattr(ci_evidence_importer, "import_for_head", None)
        ):
            raise TypeError(
                "ci_evidence_importer must provide import_for_head or be None"
            )
        self._config = config
        self._store = store
        self._orchestrator = orchestrator
        self._ci_evidence_importer = ci_evidence_importer
        self._repositories = {item.slug: item for item in config.repositories}

    def plan_candidates(self, tracker: Tracker) -> DryRunPlan:
        """Perform one SSH candidate sweep using tracker reads only."""
        return build_ssh_dry_run_plan(
            self._config,
            tracker,
            active_turn_exists=self._store.get_active_turn() is not None,
        )

    def archive_completed_work_item(
        self, work_item_id: str, *, eligible_at: str
    ) -> WorkItemArchive:
        return self._orchestrator.archive_completed_work_item(
            work_item_id, eligible_at=eligible_at
        )

    def archive_disposed_work_item(
        self, work_item_id: str, *, eligible_at: str
    ) -> WorkItemArchive:
        return self._orchestrator.archive_disposed_work_item(
            work_item_id, eligible_at=eligible_at
        )

    def reconcile_work_item_archive(self, work_item_id: str) -> WorkItemArchive:
        return self._orchestrator.reconcile_work_item_archive(work_item_id)

    def reconcile_completed_work_item_absence(
        self, work_item_id: str, *, eligible_at: str
    ) -> WorkItemAbsenceReconciliation:
        return self._orchestrator.reconcile_completed_work_item_absence(
            work_item_id, eligible_at=eligible_at
        )

    def resolve_and_prepare(
        self,
        task: TrackerTask,
        *,
        base_sha: str,
        source_bundle: SourceBundle | None = None,
        runner_root: str = "/srv/codex-runner/work-items",
        created_at: str | None = None,
    ) -> WorkItem:
        """Persist/recover the unique WorkItem and idempotently prepare its Runner repo."""
        repository = self._repository(task.repository)
        existing = self._store.get_work_item_by_issue(task.repository, task.issue_number)
        preparation_acknowledged = (
            self._store.runner_preparation_was_acknowledged(existing.work_item_id)
            if existing is not None
            and existing.state in {WorkItemState.BLOCKED, WorkItemState.PAUSED}
            else None
        )
        resolution = resolve_ssh_work_item(
            task=task,
            repository=repository,
            base_sha=base_sha,
            existing_work_item=existing,
            runner_preparation_acknowledged=preparation_acknowledged,
            runner_root=runner_root,
            created_at=created_at,
        )
        needs_prepare = resolution.action in {
            WorkItemAction.PREPARE,
            WorkItemAction.RETRY_PREPARE,
        }
        if needs_prepare and source_bundle is None:
            raise SshDispatchPlanningError("Runner preparation requires an exact source bundle")
        if source_bundle is not None and (
            source_bundle.base_sha != resolution.work_item.base_sha
        ):
            raise SshDispatchPlanningError(
                "source bundle base SHA conflicts with persisted WorkItem"
            )

        if resolution.is_new:
            self._store.create_work_item(resolution.work_item)
        if resolution.action is WorkItemAction.REACTIVATE:
            return self._store.update_work_item_state(
                resolution.work_item.work_item_id, WorkItemState.READY
            )
        if needs_prepare:
            assert source_bundle is not None
            if resolution.work_item.state in {
                WorkItemState.BLOCKED,
                WorkItemState.PAUSED,
            }:
                self._store.update_work_item_state(
                    resolution.work_item.work_item_id, WorkItemState.PREPARING
                )
            return self._orchestrator.prepare_work_item(
                resolution.work_item.work_item_id,
                source_bundle=source_bundle.artifact,
            )
        return resolution.work_item

    def run_claimed_turn(
        self,
        task: TrackerTask,
        *,
        comments: Iterable[object] = (),
        turn_id: str | None = None,
    ) -> TurnProgress:
        """Freeze and run the next Turn while atomically checking its Prompt number."""
        comments = tuple(comments)
        repository = self._repository(task.repository)
        work_item = self._store.get_work_item_by_issue(
            task.repository, task.issue_number
        )
        if work_item is None:
            raise SshDispatchPlanningError("Issue does not have a persisted WorkItem")
        turn_number = self._store.next_turn_number(work_item.work_item_id)
        session_runtime = self._config.session_runtime
        if session_runtime is not None:
            generations = self._store.list_session_generations(work_item.work_item_id)
            if len(generations) >= session_runtime.max_session_generations and not any(
                generation.is_live for generation in generations
            ):
                raise SshDispatchPlanningError(
                    "session generation budget is exhausted"
                )
            if turn_number > session_runtime.max_total_turns:
                raise SshDispatchPlanningError("total Turn budget is exhausted")
            generation = self._store.get_live_session_generation(
                work_item.work_item_id
            )
            if generation is None:
                retryable_pre_session_history = bool(generations) and (
                    self._pre_session_rejection_history_is_retryable(
                        work_item, generations
                    )
                )
                if generations and not retryable_pre_session_history:
                    raise SshDispatchPlanningError(
                        "WorkItem without a live generation requires explicit recovery"
                    )
                generation = self._store.plan_session_generation(
                    work_item.work_item_id,
                    role=SessionGenerationRole.IMPLEMENTATION,
                    policy_sha256=session_runtime.agent_policy_digest,
                    rotation_reason=(
                        PRE_SESSION_RETRY_ROTATION_REASON
                        if retryable_pre_session_history
                        else None
                    ),
                )
                generations = (*generations, generation)
            generation_turns = self._store.list_session_generation_turns(
                generation.session_generation_id
            )
            prompt_inputs = self._store.list_session_generation_turn_prompt_inputs(
                generation.session_generation_id
            )
            delivered_comment_ids: tuple[str, ...] = ()
            delivered_context_sha256: str | None = None
            prior_status: str | None = None
            prior_summary: str | None = None
            handoff = None
            pre_session_retry_without_handoff = False
            if generation.state is SessionGenerationState.PLANNED:
                if generation_turns or prompt_inputs:
                    raise SshDispatchPlanningError(
                        "planned session generation already contains Turn state"
                    )
                if generation.policy_sha256 != session_runtime.agent_policy_digest:
                    raise SshDispatchPlanningError(
                        "planned session generation policy conflicts with configuration"
                    )
                if generation.generation_number > 1:
                    handoff = self._store.get_session_handoff_for_generation(
                        generation.session_generation_id
                    )
                    if handoff is None:
                        previous_generations = tuple(
                            item
                            for item in generations
                            if item.generation_number < generation.generation_number
                        )
                        pre_session_retry = (
                            generation.rotation_reason
                            in {None, PRE_SESSION_RETRY_ROTATION_REASON}
                            and self._pre_session_rejection_history_is_retryable(
                                work_item, previous_generations
                            )
                        )
                        if not pre_session_retry:
                            raise SshDispatchPlanningError(
                                "replacement session generation has no durable handoff"
                            )
                        pre_session_retry_without_handoff = True
            elif generation.state is SessionGenerationState.ACTIVE:
                is_legacy = generation.policy_sha256 is None
                if is_legacy:
                    if generation.rotation_reason != "legacy_migration":
                        raise SshDispatchPlanningError(
                            "policy-free generation is not an imported legacy session"
                        )
                else:
                    if (
                        not generation_turns
                        or len(generation_turns) != len(prompt_inputs)
                        or prompt_inputs[-1].turn_id != generation_turns[-1].turn_id
                    ):
                        raise SshDispatchPlanningError(
                            "active session generation has incomplete prompt receipts"
                        )
                    prior_turn = generation_turns[-1]
                    context_failure_rotation = (
                        self._context_failure_rotation_required(prior_turn)
                    )
                    if not context_failure_rotation and (
                        prior_turn.result_status is None
                        or prior_turn.result_summary is None
                    ):
                        raise SshDispatchPlanningError(
                            "active session generation has no prior structured result"
                        )
                    delivered_comment_ids = tuple(
                        sorted(
                            {
                                comment_id
                                for item in generation_turns
                                for comment_id in item.included_comment_ids
                            }
                        )
                    )
                    delivered_context_sha256 = (
                        prompt_inputs[-1].cumulative_approved_context_sha256
                    )
                    if not context_failure_rotation:
                        prior_status = prior_turn.result_status
                        prior_summary = prior_turn.result_summary
                    validate_ssh_active_generation_continuity(
                        task=task,
                        repository=repository,
                        work_item=work_item,
                        session_generation=generation,
                        comments=comments,
                        delivered_comment_ids=delivered_comment_ids,
                        delivered_context_sha256=delivered_context_sha256,
                    )

                if generation.policy_sha256 != session_runtime.agent_policy_digest:
                    rotation_reason = (
                        "legacy_policy_activation" if is_legacy else "policy_changed"
                    )
                else:
                    rotation_reason = self._rotation_reason(
                        generation=generation,
                        generation_turns=generation_turns,
                    )
                if rotation_reason is not None:
                    if len(generations) >= session_runtime.max_session_generations:
                        raise SshDispatchPlanningError(
                            "session generation budget is exhausted before required rotation"
                        )
                    next_session_generation_id = f"sg_{uuid4().hex}"
                    source_turn = generation_turns[-1] if generation_turns else None
                    source_agent_result = (
                        self._store.get_turn_agent_result(source_turn.turn_id)
                        if source_turn is not None
                        else None
                    )
                    handoff_created_at = datetime.now(timezone.utc).isoformat()
                    actions_evidence = None
                    if (
                        work_item.last_published_sha is not None
                        and self._ci_evidence_importer is not None
                    ):
                        try:
                            actions_evidence = (
                                self._ci_evidence_importer.import_for_head(
                                    repository=work_item.repository,
                                    task_branch=work_item.task_branch,
                                    head_sha=work_item.last_published_sha,
                                    required_checks=repository.required_checks,
                                    observed_at=handoff_created_at,
                                )
                            )
                        except CiEvidenceError as exc:
                            raise SshDispatchPlanningError(
                                f"Actions evidence import failed: {exc}"
                            ) from exc
                    handoff = build_ssh_session_handoff_snapshot(
                        task=task,
                        repository=repository,
                        work_item=work_item,
                        from_generation=generation,
                        to_session_generation_id=next_session_generation_id,
                        to_generation_number=generation.generation_number + 1,
                        to_agent_policy_sha256=session_runtime.agent_policy_digest,
                        rotation_reason=rotation_reason,
                        published_checkpoints=(
                            self._store.list_work_item_publication_checkpoints(
                                work_item.work_item_id
                            )
                        ),
                        source_turn=source_turn,
                        source_agent_result=source_agent_result,
                        comments=comments,
                        created_at=handoff_created_at,
                        actions_evidence=actions_evidence,
                    )
                    _, generation = self._store.rotate_session_generation(
                        work_item.work_item_id,
                        current_session_generation_id=(
                            generation.session_generation_id
                        ),
                        current_generation_number=generation.generation_number,
                        expected_current_policy_sha256=generation.policy_sha256,
                        new_policy_sha256=session_runtime.agent_policy_digest,
                        new_role=generation.role,
                        rotation_reason=rotation_reason,
                        new_session_generation_id=next_session_generation_id,
                        handoff=handoff,
                    )
                    generation_turns = ()
                    prompt_inputs = ()
                    delivered_comment_ids = ()
                    delivered_context_sha256 = None
                    prior_status = None
                    prior_summary = None
            else:
                raise SshDispatchPlanningError(
                    "live session generation is not eligible for a new Turn"
                )
            generation_plan = build_ssh_generation_turn_plan(
                task=task,
                repository=repository,
                work_item=work_item,
                session_generation=generation,
                turn_number=turn_number,
                agent_policy_digest=session_runtime.agent_policy_digest,
                comments=comments,
                delivered_comment_ids=delivered_comment_ids,
                delivered_context_sha256=delivered_context_sha256,
                prior_status=prior_status,
                prior_summary=prior_summary,
                handoff=handoff,
                pre_session_retry_without_handoff=(
                    pre_session_retry_without_handoff
                ),
            )
            return self._orchestrator.run_generation_turn(
                work_item.work_item_id,
                session_generation=generation,
                issue_revision=generation_plan.issue_revision,
                prompt=generation_plan.prompt,
                prompt_kind=generation_plan.prompt_kind,
                inputs=generation_plan.inputs,
                issue_allowed_paths=generation_plan.task_spec.allowed_paths,
                handoff_id=(
                    generation_plan.handoff.handoff_id
                    if generation_plan.handoff is not None
                    else None
                ),
                pre_session_retry_without_handoff=(
                    pre_session_retry_without_handoff
                ),
                expected_turn_number=generation_plan.turn_number,
                turn_id=turn_id,
            )
        plan = build_ssh_turn_plan(
            task=task,
            repository=repository,
            work_item=work_item,
            turn_number=turn_number,
            comments=comments,
        )
        return self._orchestrator.run_turn(
            work_item.work_item_id,
            issue_revision=plan.issue_revision,
            prompt=plan.prompt,
            issue_allowed_paths=plan.task_spec.allowed_paths,
            expected_turn_number=plan.turn_number,
            turn_id=turn_id,
        )

    def _pre_session_rejection_history_is_retryable(
        self,
        work_item: WorkItem,
        generations: tuple[SessionGeneration, ...],
    ) -> bool:
        """Prove every earlier generation ended before Codex created a session."""
        if (
            not generations
            or work_item.last_published_sha is not None
            or self._store.list_work_item_publication_checkpoints(work_item.work_item_id)
        ):
            return False
        for generation in generations:
            if (
                generation.state is not SessionGenerationState.FAILED
                or generation.role is not SessionGenerationRole.IMPLEMENTATION
                or generation.codex_session_id is not None
                or generation.last_published_sha is not None
                or generation.start_head_sha != work_item.base_sha
                or generation.rotation_reason
                not in {None, PRE_SESSION_RETRY_ROTATION_REASON}
            ):
                return False
            turns = self._store.list_session_generation_turns(
                generation.session_generation_id
            )
            if len(turns) != 1:
                return False
            turn = turns[0]
            if (
                turn.state is not TurnState.BLOCKED
                or turn.error_code != "runner_request_rejected"
                or turn.input_head_sha != work_item.base_sha
                or turn.output_sha256 is not None
                or turn.output_head_sha is not None
                or turn.result_status is not None
                or turn.result_summary is not None
                or self._store.get_turn_agent_result(turn.turn_id) is not None
                or self._store.get_turn_usage(turn.turn_id) is not None
            ):
                return False
        return True

    def _rotation_reason(
        self,
        *,
        generation: SessionGeneration,
        generation_turns: tuple[Turn, ...],
    ) -> str | None:
        runtime = self._config.session_runtime
        assert runtime is not None
        if generation_turns and self._context_failure_rotation_required(
            generation_turns[-1]
        ):
            return "context_failure"
        if len(generation_turns) >= runtime.max_turns_per_session:
            return "turn_budget"
        if not runtime.use_incremental_resume_prompts and generation_turns:
            return "incremental_prompts_disabled"
        if generation_turns:
            usage = self._store.get_turn_usage(generation_turns[-1].turn_id)
            if usage is None:
                raise SshDispatchPlanningError(
                    "protocol-v2 Turn is missing its usage receipt"
                )
            if usage.input_tokens >= runtime.rotate_after_input_tokens:
                return "context_pressure"
            trailing_no_progress = 0
            for turn in reversed(generation_turns):
                if turn.output_head_sha != turn.input_head_sha:
                    break
                trailing_no_progress += 1
            if trailing_no_progress >= runtime.max_no_progress_turns:
                if generation.generation_number > 1:
                    raise SshDispatchPlanningError(
                        "no progress continued after a session rotation"
                    )
                return "no_progress"
        if generation.started_at is None:
            raise SshDispatchPlanningError(
                "active session generation has no start timestamp"
            )
        try:
            started_at = datetime.fromisoformat(
                generation.started_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise SshDispatchPlanningError(
                "session generation start timestamp is invalid"
            ) from exc
        if started_at.tzinfo is None:
            raise SshDispatchPlanningError(
                "session generation start timestamp is invalid"
            )
        age_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
        if age_seconds < 0:
            raise SshDispatchPlanningError(
                "session generation start timestamp is in the future"
            )
        if age_seconds >= runtime.rotate_after_session_age_seconds:
            return "session_age"
        return None

    def _context_failure_rotation_required(self, turn: Turn) -> bool:
        receipt = self._store.get_turn_context_failure(turn.turn_id)
        return (
            turn.state is TurnState.INTERRUPTED
            and turn.error_code == "session_context_failure_clean"
            and turn.output_sha256 is None
            and turn.output_head_sha is None
            and turn.result_status is None
            and turn.result_summary is None
            and receipt is not None
            and receipt.head_sha == turn.input_head_sha
            and receipt.worktree_clean
        )

    def publish_checkpoint(
        self,
        turn_id: str,
        *,
        publisher: TaskBranchPublisher,
    ) -> TurnProgress:
        """Recover or publish a checkpoint; mechanically unsafe evidence blocks it."""
        turn = self._store.get_turn(turn_id)
        if turn is None:
            raise KeyError(f"turn not found: {turn_id}")
        work_item = self._store.get_work_item(turn.work_item_id)
        if work_item is None:
            raise KeyError(f"work item not found: {turn.work_item_id}")
        repository = self._repository(work_item.repository)
        try:
            return self._orchestrator.publish_checkpoint(
                turn_id,
                publisher=publisher,
                repository_allowed_paths=repository.allowed_paths,
                repository_denied_paths=repository.denied_paths,
            )
        except (RunnerTransportInterrupted, GitPublicationInterrupted) as exc:
            raise CheckpointPublicationInterrupted(
                "checkpoint publication outcome is ambiguous"
            ) from exc
        except (
            RunnerTransportRejected,
            RunnerProtocolError,
            GitBundleVerificationError,
            GitPublicationRejected,
            PublicationError,
            TurnOrchestrationError,
        ):
            return self._orchestrator.reject_publication(turn_id)

    def evaluate_completion_gate(
        self, task: TrackerTask, turn_id: str
    ) -> TurnProgress:
        """Evaluate one v2 completion candidate against exact trusted evidence."""
        turn = self._store.get_turn(turn_id)
        if turn is None:
            raise KeyError(f"turn not found: {turn_id}")
        work_item = self._store.get_work_item(turn.work_item_id)
        if work_item is None:
            raise KeyError(f"work item not found: {turn.work_item_id}")
        if (
            task.repository != work_item.repository
            or task.issue_number != work_item.issue_number
            or turn.state is not TurnState.PUBLISHED
            or turn.result_status != "completed"
            or turn.output_head_sha is None
        ):
            return self._orchestrator.reject_completion_candidate(
                turn_id, error_code="completion_candidate_identity_invalid"
            )
        generation = self._store.get_turn_session_generation(turn_id)
        prompt_input = self._store.get_turn_prompt_input(turn_id)
        if (
            generation is None
            or generation.policy_sha256 is None
            or prompt_input is None
        ):
            return self._orchestrator.reject_completion_candidate(
                turn_id, error_code="completion_input_receipt_missing"
            )
        generation_head_sha = (
            generation.last_published_sha or generation.start_head_sha
        )
        if (
            work_item.last_published_sha != turn.output_head_sha
            or generation_head_sha != turn.output_head_sha
        ):
            return self._orchestrator.reject_completion_candidate(
                turn_id, error_code="completion_head_unpublished"
            )
        try:
            task_spec = parse_task_spec(task.body)
            inputs = build_canonical_input_snapshot(
                issue_title=task.title,
                task_spec=task_spec,
            )
        except (TaskSpecError, ValueError):
            return self._orchestrator.reject_completion_candidate(
                turn_id, error_code="completion_task_spec_invalid"
            )
        if (
            inputs.task_spec_sha256 != prompt_input.task_spec_sha256
            or inputs.issue_content_sha256 != prompt_input.issue_content_sha256
            or task_spec.allowed_paths != turn.issue_allowed_paths
        ):
            return self._orchestrator.reject_completion_candidate(
                turn_id, error_code="completion_task_spec_changed"
            )
        try:
            publication = build_publication_evidence(
                work_item=work_item,
                published_checkpoints=(
                    self._store.list_work_item_publication_checkpoints(
                        work_item.work_item_id
                    )
                ),
            )
        except (TypeError, ValueError):
            return self._orchestrator.reject_completion_candidate(
                turn_id, error_code="completion_publication_evidence_invalid"
            )
        if self._ci_evidence_importer is None:
            return self._orchestrator.reject_completion_candidate(
                turn_id, error_code="completion_ci_evidence_unavailable"
            )
        observed_at = datetime.now(timezone.utc).isoformat()
        try:
            actions_evidence = self._ci_evidence_importer.import_for_head(
                repository=work_item.repository,
                task_branch=work_item.task_branch,
                head_sha=turn.output_head_sha,
                required_checks=self._repository(work_item.repository).required_checks,
                observed_at=observed_at,
            )
        except CiEvidenceError:
            return self._orchestrator.reject_completion_candidate(
                turn_id, error_code="completion_ci_evidence_ambiguous"
            )
        try:
            snapshot = build_completion_gate_snapshot(
                work_item=work_item,
                turn=turn,
                task_spec=task_spec,
                task_spec_sha256=inputs.task_spec_sha256,
                required_checks=self._repository(
                    work_item.repository
                ).required_checks,
                publication=publication,
                actions_evidence=actions_evidence,
                observed_at=observed_at,
            )
            work_item, turn, _ = self._store.record_turn_completion_gate(snapshot)
        except (TypeError, ValueError):
            return self._orchestrator.reject_completion_candidate(
                turn_id, error_code="completion_evidence_invalid"
            )
        return TurnProgress(work_item, turn)

    def requires_fresh_final_audit(self, turn_id: str) -> bool:
        """Return whether a passed implementation candidate must start fresh audit."""
        runtime = self._config.session_runtime
        if runtime is None or not runtime.rotate_before_final_audit:
            return False
        gate = self._store.get_turn_completion_gate(turn_id)
        generation = self._store.get_turn_session_generation(turn_id)
        return (
            gate is not None
            and gate.status is CompletionGateStatus.PASSED
            and generation is not None
            and generation.role is SessionGenerationRole.IMPLEMENTATION
        )

    def run_fresh_final_audit(
        self,
        task: TrackerTask,
        *,
        comments: Iterable[object] = (),
        turn_id: str | None = None,
    ) -> TurnProgress:
        """Rotate a passed implementation candidate into an independent Audit Turn."""
        comments = tuple(comments)
        runtime = self._config.session_runtime
        if runtime is None or not runtime.rotate_before_final_audit:
            raise FinalAuditPreparationError("fresh_final_audit_not_enabled")
        work_item = self._store.get_work_item_by_issue(
            task.repository, task.issue_number
        )
        if work_item is None:
            raise FinalAuditPreparationError("final_audit_work_item_missing")
        generations = self._store.list_session_generations(work_item.work_item_id)
        live = self._store.get_live_session_generation(work_item.work_item_id)
        if live is None:
            raise FinalAuditPreparationError("final_audit_generation_missing")
        if live.role is SessionGenerationRole.AUDIT:
            if live.state is not SessionGenerationState.PLANNED:
                raise FinalAuditPreparationError(
                    "final_audit_generation_state_invalid"
                )
            return self.run_claimed_turn(
                task, comments=comments, turn_id=turn_id
            )
        if live.role is not SessionGenerationRole.IMPLEMENTATION:
            raise FinalAuditPreparationError("final_audit_source_role_invalid")
        if len(generations) >= runtime.max_session_generations:
            raise FinalAuditPreparationError("final_audit_generation_budget_exhausted")
        turns = self._store.list_turns(work_item.work_item_id)
        if not turns:
            raise FinalAuditPreparationError("final_audit_source_turn_missing")
        source_turn = turns[-1]
        gate = self._store.get_turn_completion_gate(source_turn.turn_id)
        if (
            source_turn.state is not TurnState.FINISHED
            or source_turn.result_status != "completed"
            or gate is None
            or gate.status is not CompletionGateStatus.PASSED
            or self._store.get_turn_session_generation(source_turn.turn_id) != live
            or work_item.state not in {WorkItemState.REVIEW, WorkItemState.READY}
        ):
            raise FinalAuditPreparationError("final_audit_source_evidence_invalid")
        if self._ci_evidence_importer is None or work_item.last_published_sha is None:
            raise FinalAuditPreparationError("final_audit_ci_evidence_unavailable")
        created_at = datetime.now(timezone.utc).isoformat()
        repository = self._repository(work_item.repository)
        try:
            actions_evidence = self._ci_evidence_importer.import_for_head(
                repository=work_item.repository,
                task_branch=work_item.task_branch,
                head_sha=work_item.last_published_sha,
                required_checks=repository.required_checks,
                observed_at=created_at,
            )
        except CiEvidenceError as exc:
            raise FinalAuditPreparationError(
                "final_audit_ci_evidence_ambiguous"
            ) from exc
        next_generation_id = f"sg_{uuid4().hex}"
        try:
            handoff = build_ssh_session_handoff_snapshot(
                task=task,
                repository=repository,
                work_item=work_item,
                from_generation=live,
                to_session_generation_id=next_generation_id,
                to_generation_number=live.generation_number + 1,
                to_agent_policy_sha256=runtime.agent_policy_digest,
                rotation_reason="completion_candidate",
                published_checkpoints=(
                    self._store.list_work_item_publication_checkpoints(
                        work_item.work_item_id
                    )
                ),
                source_turn=source_turn,
                source_agent_result=self._store.get_turn_agent_result(
                    source_turn.turn_id
                ),
                comments=comments,
                created_at=created_at,
                actions_evidence=actions_evidence,
            )
        except SshDispatchPlanningError as exc:
            raise FinalAuditPreparationError(
                "final_audit_handoff_invalid"
            ) from exc
        if work_item.state is WorkItemState.REVIEW:
            work_item = self._store.update_work_item_state(
                work_item.work_item_id, WorkItemState.READY
            )
        try:
            self._store.rotate_session_generation(
                work_item.work_item_id,
                current_session_generation_id=live.session_generation_id,
                current_generation_number=live.generation_number,
                expected_current_policy_sha256=live.policy_sha256,
                new_policy_sha256=runtime.agent_policy_digest,
                new_role=SessionGenerationRole.AUDIT,
                rotation_reason="completion_candidate",
                new_session_generation_id=next_generation_id,
                handoff=handoff,
            )
        except (TypeError, ValueError) as exc:
            raise FinalAuditPreparationError(
                "final_audit_rotation_invalid"
            ) from exc
        return self.run_claimed_turn(task, comments=comments, turn_id=turn_id)

    def reconcile_turn(self, turn_id: str) -> TurnProgress:
        """Reconcile one ambiguous active Turn without replaying its Prompt."""
        return self._orchestrator.reconcile_turn(turn_id)

    def _repository(self, slug: str) -> RepositoryConfig:
        try:
            return self._repositories[slug]
        except KeyError as exc:
            raise SshDispatchPlanningError("Issue repository is not configured") from exc


class CheckpointPublicationInterrupted(RuntimeError):
    """Raised when retry must reconcile a possibly successful publication."""


class FinalAuditPreparationError(RuntimeError):
    """A bounded fail-closed reason for fresh Audit generation preparation."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code
