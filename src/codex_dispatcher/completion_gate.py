"""Durable exact-HEAD CI and structured acceptance completion evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any

from codex_dispatcher.acceptance_evaluator import (
    AcceptanceEvaluation,
    AcceptanceStatus,
    evaluate_acceptance,
)
from codex_dispatcher.ci_evidence import (
    ActionsEvidenceSnapshot,
    ActionsRunEvidence,
    RequiredCheckStatus,
)
from codex_dispatcher.handoffs import PublicationEvidence
from codex_dispatcher.task_spec import TaskSpec
from codex_dispatcher.runner_protocol import AcceptanceAssertion
from codex_dispatcher.work_items import (
    SessionGenerationRole,
    Turn,
    WorkItem,
    validate_git_sha,
    validate_sha256,
    validate_turn_id,
    validate_work_item_id,
)


MAX_COMPLETION_GATE_JSON_BYTES = 256 * 1024


class CompletionGateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class CompletionGateSnapshot:
    """Canonical trusted evidence bound to one completion-candidate Turn."""

    turn_id: str
    work_item_id: str
    status: CompletionGateStatus
    head_sha: str
    task_spec_sha256: str
    evidence_json: str
    evidence_sha256: str
    observed_at: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        validate_turn_id(self.turn_id)
        validate_work_item_id(self.work_item_id)
        if not isinstance(self.status, CompletionGateStatus):
            raise TypeError("status must be a CompletionGateStatus")
        validate_git_sha(self.head_sha, "head_sha")
        validate_sha256(self.task_spec_sha256, "task_spec_sha256")
        for field in ("observed_at", "created_at", "updated_at"):
            _aware_timestamp(getattr(self, field), field)
        raw = self.evidence_json.encode("utf-8")
        if len(raw) > MAX_COMPLETION_GATE_JSON_BYTES:
            raise ValueError("completion gate evidence exceeds its byte boundary")
        validate_sha256(self.evidence_sha256, "evidence_sha256")
        if sha256(raw).hexdigest() != self.evidence_sha256:
            raise ValueError("completion gate evidence digest is invalid")
        evidence = _canonical_object(self.evidence_json)
        _validate_evidence_identity(evidence, self)

    @property
    def evidence(self) -> dict[str, Any]:
        return _canonical_object(self.evidence_json)


def repairable_ci_failures(
    snapshot: CompletionGateSnapshot,
    actions_evidence: ActionsEvidenceSnapshot,
) -> tuple[dict[str, str], ...]:
    """Return exact code-attributable CI failures, never ambiguous infrastructure exits."""
    if (
        not isinstance(snapshot, CompletionGateSnapshot)
        or not isinstance(actions_evidence, ActionsEvidenceSnapshot)
        or snapshot.status is not CompletionGateStatus.FAILED
    ):
        return ()
    acceptance_items = snapshot.evidence["acceptance"].get("criteria")
    if not isinstance(acceptance_items, list) or any(
        not isinstance(item, dict)
        or item.get("status")
        in {
            AcceptanceStatus.UNVERIFIED.value,
            AcceptanceStatus.PENDING.value,
        }
        or (
            item.get("status") == AcceptanceStatus.FAILED.value
            and item.get("predicate") != "required-check"
        )
        for item in acceptance_items
    ):
        return ()
    failed = tuple(
        item
        for item in actions_evidence.required_checks
        if item.status is RequiredCheckStatus.FAILED
    )
    if not failed or any(
        item.run is None or item.run.conclusion != "failure" for item in failed
    ):
        return ()
    return tuple(
        {
            "name": item.name,
            "conclusion": item.run.conclusion,
            "url": item.run.html_url,
            "evidence_ref": item.run.evidence_ref,
        }
        for item in failed
        if item.run is not None
    )


def build_completion_gate_snapshot(
    *,
    work_item: WorkItem,
    turn: Turn,
    task_spec: TaskSpec,
    task_spec_sha256: str,
    required_checks: tuple[str, ...],
    publication: PublicationEvidence,
    actions_evidence: ActionsEvidenceSnapshot,
    observed_at: str,
    session_role: SessionGenerationRole = SessionGenerationRole.IMPLEMENTATION,
    audit_assertions: tuple[AcceptanceAssertion, ...] = (),
    agent_result_evidence_ref: str | None = None,
    created_at: str | None = None,
) -> CompletionGateSnapshot:
    """Evaluate a completion candidate from Dispatcher/Git/Actions facts only."""
    if not isinstance(work_item, WorkItem):
        raise TypeError("work_item must be a WorkItem")
    if not isinstance(turn, Turn):
        raise TypeError("turn must be a Turn")
    if turn.work_item_id != work_item.work_item_id:
        raise ValueError("Turn does not belong to the WorkItem")
    if turn.result_status != "completed" or turn.output_head_sha is None:
        raise ValueError("Turn is not a completion candidate")
    if not isinstance(task_spec, TaskSpec):
        raise TypeError("task_spec must be a TaskSpec")
    validate_sha256(task_spec_sha256, "task_spec_sha256")
    if (
        not isinstance(required_checks, tuple)
        or not required_checks
        or len(required_checks) > 100
        or len(set(required_checks)) != len(required_checks)
    ):
        raise ValueError("required_checks must be a bounded unique tuple")
    if not isinstance(publication, PublicationEvidence):
        raise TypeError("publication must be PublicationEvidence")
    if not isinstance(actions_evidence, ActionsEvidenceSnapshot):
        raise TypeError("actions_evidence must be ActionsEvidenceSnapshot")
    if (
        publication.current_head_sha != turn.output_head_sha
        or actions_evidence.repository != work_item.repository
        or actions_evidence.task_branch != work_item.task_branch
        or actions_evidence.head_sha != turn.output_head_sha
        or tuple(item.name for item in actions_evidence.required_checks)
        != required_checks
    ):
        raise ValueError("completion evidence conflicts with its exact target")
    if task_spec.allowed_paths != turn.issue_allowed_paths:
        raise ValueError("completion TaskSpec conflicts with the frozen Turn path policy")

    acceptance = evaluate_acceptance(
        criteria=task_spec.acceptance_items,
        configured_required_checks=required_checks,
        allowed_paths=task_spec.allowed_paths,
        publication_evidence_complete=(
            publication.publication_evidence_complete
        ),
        verified_changed_paths=publication.verified_changed_paths,
        head_was_published=publication.head_was_published,
        actions_evidence=actions_evidence,
        session_role=session_role,
        audit_assertions=audit_assertions,
        agent_result_evidence_ref=agent_result_evidence_ref,
    )
    gate_status, gate_reason = _gate_outcome(acceptance, actions_evidence)
    criteria_sha256 = sha256(
        task_spec.acceptance_criteria.encode("utf-8")
    ).hexdigest()
    evidence: dict[str, Any] = {
        "acceptance": acceptance.to_mapping(criteria_sha256=criteria_sha256),
        "ci_evidence": {
            "head_sha": actions_evidence.head_sha,
            "observed_at": actions_evidence.observed_at,
            "provider": "github_actions",
            "remote_ref_sha": actions_evidence.remote_ref_sha,
            "repository": actions_evidence.repository,
            "runs": [
                _actions_run_mapping(item.run)
                for item in actions_evidence.required_checks
                if item.run is not None
            ],
            "task_branch": actions_evidence.task_branch,
        },
        "gate": {"reason": gate_reason, "status": gate_status.value},
        "git": {
            "base_sha": work_item.base_sha,
            "current_head_sha": publication.current_head_sha,
            "publication_evidence_complete": (
                publication.publication_evidence_complete
            ),
            "published_checkpoint_heads": list(
                publication.published_checkpoint_heads
            ),
            "task_branch": work_item.task_branch,
            "verified_changed_paths": list(publication.verified_changed_paths),
        },
        "identity": {
            "turn_id": turn.turn_id,
            "work_item_id": work_item.work_item_id,
        },
        "issue": {
            "criteria_sha256": criteria_sha256,
            "task_spec_sha256": task_spec_sha256,
        },
        "provenance": "dispatcher_git_ci_state",
        "required_checks": [
            {
                "evidence_ref": (
                    item.run.evidence_ref if item.run is not None else None
                ),
                "name": item.name,
                "status": item.status.value,
            }
            for item in actions_evidence.required_checks
        ],
        "schema_version": 1,
    }
    evidence_json = json.dumps(
        evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    now = created_at or observed_at
    return CompletionGateSnapshot(
        turn_id=turn.turn_id,
        work_item_id=work_item.work_item_id,
        status=gate_status,
        head_sha=turn.output_head_sha,
        task_spec_sha256=task_spec_sha256,
        evidence_json=evidence_json,
        evidence_sha256=sha256(evidence_json.encode("utf-8")).hexdigest(),
        observed_at=observed_at,
        created_at=now,
        updated_at=now,
    )


def _gate_outcome(
    acceptance: AcceptanceEvaluation,
    actions_evidence: ActionsEvidenceSnapshot,
) -> tuple[CompletionGateStatus, str]:
    check_statuses = frozenset(
        item.status for item in actions_evidence.required_checks
    )
    if (
        acceptance.status is AcceptanceStatus.FAILED
        or RequiredCheckStatus.FAILED in check_statuses
    ):
        return CompletionGateStatus.FAILED, "trusted_evidence_failed"
    if acceptance.status is AcceptanceStatus.UNVERIFIED:
        return CompletionGateStatus.UNVERIFIED, "acceptance_unverified"
    if (
        acceptance.status is AcceptanceStatus.PENDING
        or RequiredCheckStatus.PENDING in check_statuses
        or RequiredCheckStatus.NOT_OBSERVED in check_statuses
    ):
        return CompletionGateStatus.PENDING, "trusted_evidence_pending"
    return CompletionGateStatus.PASSED, "all_required_evidence_passed"


def _actions_run_mapping(run: ActionsRunEvidence) -> dict[str, object]:
    return {
        "conclusion": run.conclusion,
        "created_at": run.created_at,
        "event": run.event,
        "head_branch": run.head_branch,
        "head_repository": run.head_repository,
        "head_sha": run.head_sha,
        "html_url": run.html_url,
        "name": run.name,
        "repository": run.repository,
        "run_attempt": run.run_attempt,
        "run_id": run.run_id,
        "status": run.status,
        "updated_at": run.updated_at,
        "workflow_id": run.workflow_id,
    }


def _canonical_object(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("completion gate evidence is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("completion gate evidence must be an object")
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if canonical != content:
        raise ValueError("completion gate evidence must use canonical JSON")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("completion gate evidence contains duplicate keys")
        value[key] = item
    return value


def _validate_evidence_identity(
    evidence: dict[str, Any], snapshot: CompletionGateSnapshot
) -> None:
    expected = {
        "acceptance",
        "ci_evidence",
        "gate",
        "git",
        "identity",
        "issue",
        "provenance",
        "required_checks",
        "schema_version",
    }
    if set(evidence) != expected or evidence["schema_version"] != 1:
        raise ValueError("completion gate evidence has unexpected fields")
    if evidence["provenance"] != "dispatcher_git_ci_state":
        raise ValueError("completion gate evidence provenance is invalid")
    identity = evidence["identity"]
    gate = evidence["gate"]
    git = evidence["git"]
    issue = evidence["issue"]
    ci = evidence["ci_evidence"]
    if not all(isinstance(item, dict) for item in (identity, gate, git, issue, ci)):
        raise ValueError("completion gate evidence identity fields are invalid")
    if (
        identity.get("turn_id") != snapshot.turn_id
        or identity.get("work_item_id") != snapshot.work_item_id
        or gate.get("status") != snapshot.status.value
        or git.get("current_head_sha") != snapshot.head_sha
        or issue.get("task_spec_sha256") != snapshot.task_spec_sha256
        or ci.get("head_sha") != snapshot.head_sha
        or ci.get("remote_ref_sha") != snapshot.head_sha
    ):
        raise ValueError("completion gate evidence conflicts with its durable binding")


def _aware_timestamp(value: object, field: str) -> datetime:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field} must be non-empty bounded text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed
