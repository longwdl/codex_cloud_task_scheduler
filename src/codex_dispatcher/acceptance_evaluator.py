"""Mechanical evaluation for the small trusted acceptance-criteria language."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from codex_dispatcher.ci_evidence import (
    ActionsEvidenceSnapshot,
    RequiredCheckStatus,
)
from codex_dispatcher.task_spec import AcceptanceCriterion, is_path_allowed
from codex_dispatcher.runner_protocol import (
    AcceptanceAssertion,
    AcceptanceAssertionStatus,
)
from codex_dispatcher.work_items import SessionGenerationRole


class AcceptanceStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"
    UNVERIFIED = "unverified"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class CriterionEvaluation:
    criterion_id: str
    description: str
    predicate: str
    argument: str | None
    status: AcceptanceStatus
    reason: str
    evidence_refs: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, object]:
        return {
            "argument": self.argument,
            "criterion_id": self.criterion_id,
            "description": self.description,
            "evidence_refs": list(self.evidence_refs),
            "predicate": self.predicate,
            "reason": self.reason,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceEvaluation:
    status: AcceptanceStatus
    criteria: tuple[CriterionEvaluation, ...]

    def to_mapping(self, *, criteria_sha256: str) -> dict[str, object]:
        return {
            "criteria": [item.to_mapping() for item in self.criteria],
            "criteria_sha256": criteria_sha256,
            "status": self.status.value,
        }


def evaluate_acceptance(
    *,
    criteria: tuple[AcceptanceCriterion, ...],
    configured_required_checks: tuple[str, ...],
    allowed_paths: tuple[str, ...],
    publication_evidence_complete: bool,
    verified_changed_paths: tuple[str, ...],
    head_was_published: bool,
    actions_evidence: ActionsEvidenceSnapshot | None,
    session_role: SessionGenerationRole = SessionGenerationRole.IMPLEMENTATION,
    audit_assertions: tuple[AcceptanceAssertion, ...] = (),
    agent_result_evidence_ref: str | None = None,
) -> AcceptanceEvaluation:
    """Evaluate only predicates backed by Dispatcher, Git, or exact Actions facts."""
    if (
        not isinstance(criteria, tuple)
        or not criteria
        or any(not isinstance(item, AcceptanceCriterion) for item in criteria)
    ):
        raise TypeError("criteria must be a non-empty AcceptanceCriterion tuple")
    if any(
        item.predicate == "changed-paths-within-allowed" for item in criteria
    ) and not allowed_paths:
        raise ValueError(
            "changed-paths-within-allowed requires a non-empty Issue path allowlist"
        )
    configured = frozenset(configured_required_checks)
    if not isinstance(session_role, SessionGenerationRole):
        raise TypeError("session_role must be a SessionGenerationRole")
    assertions = {item.criterion_id: item for item in audit_assertions}
    if len(assertions) != len(audit_assertions):
        raise ValueError("audit assertion ids must be unique")
    results: list[CriterionEvaluation] = []
    for criterion in criteria:
        if criterion.predicate == "audit":
            if session_role is not SessionGenerationRole.AUDIT:
                results.append(
                    _result(
                        criterion,
                        AcceptanceStatus.DEFERRED,
                        "fresh_audit_required",
                    )
                )
                continue
            assertion = assertions.get(criterion.criterion_id)
            refs = (
                (agent_result_evidence_ref,)
                if agent_result_evidence_ref is not None
                else ()
            )
            if assertion is None or assertion.status is AcceptanceAssertionStatus.NOT_VERIFIED:
                results.append(
                    _result(
                        criterion,
                        AcceptanceStatus.UNVERIFIED,
                        "audit_assertion_not_verified",
                        refs,
                    )
                )
            elif assertion.status is AcceptanceAssertionStatus.FAILED:
                results.append(
                    _result(
                        criterion,
                        AcceptanceStatus.FAILED,
                        "audit_assertion_failed",
                        refs,
                    )
                )
            else:
                results.append(
                    _result(
                        criterion,
                        AcceptanceStatus.PASSED,
                        "fresh_audit_attested",
                        refs,
                    )
                )
            continue
        if criterion.predicate == "manual":
            results.append(
                _result(
                    criterion,
                    AcceptanceStatus.UNVERIFIED,
                    "manual_evidence_required",
                )
            )
            continue
        if criterion.predicate == "required-check":
            assert criterion.argument is not None
            if criterion.argument not in configured:
                results.append(
                    _result(
                        criterion,
                        AcceptanceStatus.UNVERIFIED,
                        "required_check_not_configured",
                    )
                )
                continue
            if actions_evidence is None:
                results.append(
                    _result(
                        criterion,
                        AcceptanceStatus.UNVERIFIED,
                        "actions_evidence_unavailable",
                    )
                )
                continue
            check = actions_evidence.check(criterion.argument)
            if check is None:
                results.append(
                    _result(
                        criterion,
                        AcceptanceStatus.UNVERIFIED,
                        "required_check_missing_from_snapshot",
                    )
                )
            elif check.status is RequiredCheckStatus.NOT_OBSERVED:
                results.append(
                    _result(
                        criterion,
                        AcceptanceStatus.PENDING,
                        "required_check_not_observed",
                    )
                )
            elif check.status is RequiredCheckStatus.PENDING:
                assert check.run is not None
                results.append(
                    _result(
                        criterion,
                        AcceptanceStatus.PENDING,
                        "required_check_pending",
                        (check.run.evidence_ref,),
                    )
                )
            elif check.status is RequiredCheckStatus.PASSED:
                assert check.run is not None
                results.append(
                    _result(
                        criterion,
                        AcceptanceStatus.PASSED,
                        "required_check_succeeded",
                        (check.run.evidence_ref,),
                    )
                )
            else:
                assert check.status is RequiredCheckStatus.FAILED
                assert check.run is not None
                results.append(
                    _result(
                        criterion,
                        AcceptanceStatus.FAILED,
                        "required_check_did_not_succeed",
                        (check.run.evidence_ref,),
                    )
                )
            continue
        if criterion.predicate == "changed-paths-within-allowed":
            if not publication_evidence_complete:
                results.append(
                    _result(
                        criterion,
                        AcceptanceStatus.UNVERIFIED,
                        "publication_evidence_incomplete",
                    )
                )
            elif all(is_path_allowed(path, allowed_paths) for path in verified_changed_paths):
                results.append(
                    _result(
                        criterion,
                        AcceptanceStatus.PASSED,
                        "all_verified_paths_are_allowed",
                        ("dispatcher-publication-ledger",),
                    )
                )
            else:
                results.append(
                    _result(
                        criterion,
                        AcceptanceStatus.FAILED,
                        "verified_path_outside_issue_allowlist",
                        ("dispatcher-publication-ledger",),
                    )
                )
            continue
        if criterion.predicate == "task-head-published":
            results.append(
                _result(
                    criterion,
                    (
                        AcceptanceStatus.PASSED
                        if head_was_published
                        else AcceptanceStatus.PENDING
                    ),
                    (
                        "task_head_has_publication_receipt"
                        if head_was_published
                        else "task_head_not_yet_published"
                    ),
                    (
                        ("dispatcher-work-item:last-published-sha",)
                        if head_was_published
                        else ()
                    ),
                )
            )
            continue
        raise ValueError(f"unsupported acceptance predicate: {criterion.predicate}")
    return AcceptanceEvaluation(_aggregate(tuple(results)), tuple(results))


def _result(
    criterion: AcceptanceCriterion,
    status: AcceptanceStatus,
    reason: str,
    evidence_refs: tuple[str, ...] = (),
) -> CriterionEvaluation:
    return CriterionEvaluation(
        criterion_id=criterion.criterion_id,
        description=criterion.description,
        predicate=criterion.predicate,
        argument=criterion.argument,
        status=status,
        reason=reason,
        evidence_refs=evidence_refs,
    )


def _aggregate(criteria: tuple[CriterionEvaluation, ...]) -> AcceptanceStatus:
    statuses = frozenset(item.status for item in criteria)
    for status in (
        AcceptanceStatus.FAILED,
        AcceptanceStatus.UNVERIFIED,
        AcceptanceStatus.PENDING,
    ):
        if status in statuses:
            return status
    return AcceptanceStatus.PASSED
