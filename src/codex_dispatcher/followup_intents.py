"""Immutable, crash-recoverable decisions to continue one SSH WorkItem."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any, Mapping

from codex_dispatcher.runner_protocol import AgentResult, agent_result_to_mapping
from codex_dispatcher.work_items import (
    SessionGenerationRole,
    validate_git_sha,
    validate_session_generation_id,
    validate_sha256,
    validate_turn_id,
    validate_work_item_id,
)


MAX_FOLLOWUP_CONTEXT_BYTES = 256 * 1024


class FollowupCause(StrEnum):
    AGENT_CHECKPOINT = "agent_checkpoint"
    CI_FAILURE = "ci_failure"
    AUDIT_GAP = "audit_gap"


class FollowupIntentState(StrEnum):
    PLANNED = "planned"
    STARTED = "started"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class TurnFollowupIntent:
    source_turn_id: str
    work_item_id: str
    cause: FollowupCause
    target_role: SessionGenerationRole
    state: FollowupIntentState
    head_sha: str
    context_json: str
    context_sha256: str
    created_at: str
    updated_at: str
    target_session_generation_id: str | None = None
    target_turn_id: str | None = None

    def __post_init__(self) -> None:
        validate_turn_id(self.source_turn_id)
        validate_work_item_id(self.work_item_id)
        validate_git_sha(self.head_sha, "head_sha")
        if not isinstance(self.cause, FollowupCause):
            raise TypeError("cause must be a FollowupCause")
        if self.target_role not in {
            SessionGenerationRole.IMPLEMENTATION,
            SessionGenerationRole.CI_REPAIR,
        }:
            raise ValueError("follow-up target role is unsupported")
        if not isinstance(self.state, FollowupIntentState):
            raise TypeError("state must be a FollowupIntentState")
        raw = self.context_json.encode("utf-8")
        if not raw or len(raw) > MAX_FOLLOWUP_CONTEXT_BYTES:
            raise ValueError("follow-up context exceeds its byte boundary")
        validate_sha256(self.context_sha256, "context_sha256")
        if sha256(raw).hexdigest() != self.context_sha256:
            raise ValueError("follow-up context digest is invalid")
        context = _canonical_object(self.context_json)
        if context != {
            "cause": self.cause.value,
            "classification": context.get("classification"),
            "head_sha": self.head_sha,
            "payload": context.get("payload"),
            "schema_version": 1,
            "source_turn_id": self.source_turn_id,
        }:
            raise ValueError("follow-up context identity is invalid")
        if context["classification"] not in {
            "untrusted_agent_advisory",
            "trusted_dispatcher_ci_evidence",
        } or not isinstance(context["payload"], dict):
            raise ValueError("follow-up context provenance is invalid")
        for value in (self.created_at, self.updated_at):
            if not isinstance(value, str) or not value or len(value) > 64:
                raise ValueError("follow-up timestamp is invalid")
        if self.target_session_generation_id is not None:
            validate_session_generation_id(self.target_session_generation_id)
        if self.target_turn_id is not None:
            validate_turn_id(self.target_turn_id)
        if self.state is FollowupIntentState.STARTED and (
            self.target_session_generation_id is None or self.target_turn_id is None
        ):
            raise ValueError("started follow-up must bind its target generation and Turn")

    @property
    def context(self) -> dict[str, Any]:
        return _canonical_object(self.context_json)

    def bind_generation(
        self, session_generation_id: str, *, at: str
    ) -> "TurnFollowupIntent":
        validate_session_generation_id(session_generation_id)
        if self.state is not FollowupIntentState.PLANNED:
            raise ValueError("only a planned follow-up can bind a generation")
        if (
            self.target_session_generation_id is not None
            and self.target_session_generation_id != session_generation_id
        ):
            raise ValueError("follow-up is already bound to another generation")
        return replace(
            self,
            target_session_generation_id=session_generation_id,
            updated_at=at,
        )

    def start(
        self, *, session_generation_id: str, turn_id: str, at: str
    ) -> "TurnFollowupIntent":
        validate_session_generation_id(session_generation_id)
        validate_turn_id(turn_id)
        if self.state is not FollowupIntentState.PLANNED:
            raise ValueError("only a planned follow-up can start")
        if self.target_session_generation_id not in {None, session_generation_id}:
            raise ValueError("follow-up target generation changed")
        return replace(
            self,
            state=FollowupIntentState.STARTED,
            target_session_generation_id=session_generation_id,
            target_turn_id=turn_id,
            updated_at=at,
        )


def build_agent_followup_intent(
    *,
    source_turn_id: str,
    work_item_id: str,
    head_sha: str,
    cause: FollowupCause,
    target_role: SessionGenerationRole,
    agent_result: AgentResult,
    created_at: str,
) -> TurnFollowupIntent:
    if cause not in {FollowupCause.AGENT_CHECKPOINT, FollowupCause.AUDIT_GAP}:
        raise ValueError("agent result cannot create this follow-up cause")
    context = {
        "cause": cause.value,
        "classification": "untrusted_agent_advisory",
        "head_sha": head_sha,
        "payload": {"agent_result": agent_result_to_mapping(agent_result)},
        "schema_version": 1,
        "source_turn_id": source_turn_id,
    }
    return _new_intent(
        source_turn_id=source_turn_id,
        work_item_id=work_item_id,
        cause=cause,
        target_role=target_role,
        head_sha=head_sha,
        context=context,
        created_at=created_at,
    )


def build_ci_followup_intent(
    *,
    source_turn_id: str,
    work_item_id: str,
    head_sha: str,
    gate_evidence_sha256: str,
    failed_checks: tuple[Mapping[str, object], ...],
    created_at: str,
) -> TurnFollowupIntent:
    validate_sha256(gate_evidence_sha256, "gate_evidence_sha256")
    checks: list[dict[str, object]] = []
    for item in failed_checks:
        exact = dict(item)
        if set(exact) != {"name", "conclusion", "url", "evidence_ref"} or any(
            not isinstance(exact[key], str) or not exact[key]
            for key in exact
        ):
            raise ValueError("failed check feedback is invalid")
        checks.append(exact)
    if not checks:
        raise ValueError("CI follow-up requires at least one failed check")
    context = {
        "cause": FollowupCause.CI_FAILURE.value,
        "classification": "trusted_dispatcher_ci_evidence",
        "head_sha": head_sha,
        "payload": {
            "failed_checks": checks,
            "gate_evidence_sha256": gate_evidence_sha256,
        },
        "schema_version": 1,
        "source_turn_id": source_turn_id,
    }
    return _new_intent(
        source_turn_id=source_turn_id,
        work_item_id=work_item_id,
        cause=FollowupCause.CI_FAILURE,
        target_role=SessionGenerationRole.CI_REPAIR,
        head_sha=head_sha,
        context=context,
        created_at=created_at,
    )


def _new_intent(
    *,
    source_turn_id: str,
    work_item_id: str,
    cause: FollowupCause,
    target_role: SessionGenerationRole,
    head_sha: str,
    context: dict[str, object],
    created_at: str,
) -> TurnFollowupIntent:
    encoded = json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return TurnFollowupIntent(
        source_turn_id=source_turn_id,
        work_item_id=work_item_id,
        cause=cause,
        target_role=target_role,
        state=FollowupIntentState.PLANNED,
        head_sha=head_sha,
        context_json=encoded,
        context_sha256=sha256(encoded.encode("utf-8")).hexdigest(),
        created_at=created_at,
        updated_at=created_at,
    )


def _canonical_object(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("follow-up context is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("follow-up context must be an object")
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if canonical != content:
        raise ValueError("follow-up context must use canonical JSON")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field: {key}")
        value[key] = item
    return value
