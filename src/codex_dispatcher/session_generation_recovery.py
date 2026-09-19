"""Read-only guards for session-generation histories that need manual recovery."""

from __future__ import annotations

from codex_dispatcher.config import SessionRuntimeConfig
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.work_items import (
    PRE_SESSION_RETRY_ROTATION_REASON,
    SessionGeneration,
    SessionGenerationRole,
    SessionGenerationState,
    TurnState,
    WorkItem,
)


SESSION_GENERATION_RECOVERY_REQUIRED = "session_generation_recovery_required"
SESSION_GENERATION_BUDGET_EXHAUSTED = "session_generation_budget_exhausted"
TOTAL_TURN_BUDGET_EXHAUSTED = "total_turn_budget_exhausted"


def pre_session_rejection_history_is_retryable(
    store: StateStore,
    work_item: WorkItem,
    generations: tuple[SessionGeneration, ...],
) -> bool:
    """Prove every earlier generation ended before Codex created a session."""
    if (
        not generations
        or work_item.last_published_sha is not None
        or store.list_work_item_publication_checkpoints(work_item.work_item_id)
        or tuple(item.generation_number for item in generations)
        != tuple(range(1, len(generations) + 1))
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
            or store.get_session_handoff_for_generation(generation.session_generation_id)
            is not None
        ):
            return False
        turns = store.list_session_generation_turns(
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
            or store.get_turn_agent_result(turn.turn_id) is not None
            or store.get_turn_usage(turn.turn_id) is not None
        ):
            return False
    return True


def session_generation_recovery_reason(
    store: StateStore,
    work_item: WorkItem,
    *,
    session_runtime: SessionRuntimeConfig | None,
) -> str | None:
    """Return the fail-closed reason for persisted session-generation history."""
    generations = store.list_session_generations(work_item.work_item_id)
    if not generations:
        return None
    if session_runtime is None:
        generation = generations[0]
        if (
            len(generations) == 1
            and generation.generation_number == 1
            and generation.state is SessionGenerationState.ACTIVE
            and generation.role is SessionGenerationRole.IMPLEMENTATION
            and generation.rotation_reason == "legacy_migration"
            and generation.policy_sha256 is None
            and generation.codex_session_id is not None
            and generation.codex_session_id == work_item.codex_session_id
        ):
            return None
        return SESSION_GENERATION_RECOVERY_REQUIRED
    if any(generation.is_live for generation in generations):
        return None
    if not pre_session_rejection_history_is_retryable(store, work_item, generations):
        return SESSION_GENERATION_RECOVERY_REQUIRED
    if len(generations) >= session_runtime.max_session_generations:
        return SESSION_GENERATION_BUDGET_EXHAUSTED
    next_turn = max(
        (turn.turn_number for turn in store.list_turns(work_item.work_item_id)),
        default=0,
    ) + 1
    if next_turn > session_runtime.max_total_turns:
        return TOTAL_TURN_BUDGET_EXHAUSTED
    return None
