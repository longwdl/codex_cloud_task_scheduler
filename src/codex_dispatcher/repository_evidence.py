"""Pure, deterministic repository recovery and exact-target evidence evaluators."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Mapping

from codex_dispatcher.repository_admission import (
    RepositoryPolicyIdentity,
    RepositoryRecoveryProfile,
    RepositoryTargetReadbackProfile,
)
from codex_dispatcher.trackers.base import PullRequest, TrackerTask
from codex_dispatcher.work_items import Turn, WorkItem


LEGACY_RECOVERY_PROFILE = "legacy-unbound-recovery-v1"
LEGACY_TARGET_READBACK_PROFILE = "legacy-exact-recovery-v1"

_FIXTURE_RECOVERY_ACTIONS = frozenset(
    {
        "reconcile_active_turn",
        "resume_publication",
        "resume_preparation",
        "start_claimed_turn",
        "start_autonomous_turn",
        "start_fresh_final_audit",
        "recover_orphan_claim",
        "sync_tracker_state",
        "complete_merged_work_item",
        "prepare_work_item_discard",
        "reject_discarded_turn_result",
        "close_discarded_pull_request",
        "record_work_item_disposition",
        "close_terminal_issue",
        "archive_disposed_work_item",
        "archive_completed_work_item",
        "reconcile_work_item_archive",
        "delete_terminal_branch",
    }
)

# Higher-value recovery is reachable only through the fixed, exact-target manual
# canary entry point while ordinary higher-value admission remains false.  It may
# persist an operator-approved disposition and reclaim that exact Runner
# WorkItem, but it deliberately cannot delete the Git branch.
_HIGHER_VALUE_RECOVERY_ACTIONS = frozenset(
    {
        "reconcile_active_turn",
        "resume_publication",
        "resume_preparation",
        "start_claimed_turn",
        "start_autonomous_turn",
        "start_fresh_final_audit",
        "recover_orphan_claim",
        "sync_tracker_state",
        "prepare_work_item_discard",
        "reject_discarded_turn_result",
        "close_discarded_pull_request",
        "record_work_item_disposition",
        "close_terminal_issue",
        "archive_disposed_work_item",
        "reconcile_work_item_archive",
    }
)


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def _digest(value: Mapping[str, object]) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_sha256(value: str, field: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class RepositoryRecoveryReceipt:
    receipt_sha256: str
    work_item_id: str | None
    repository: str
    issue_number: int
    policy_sha256: str | None
    recovery_profile: str
    action: str
    decision: str
    code: str | None
    evidence_json: str
    created_at: str

    def __post_init__(self) -> None:
        _validate_sha256(self.receipt_sha256, "receipt_sha256")
        if self.policy_sha256 is not None:
            _validate_sha256(self.policy_sha256, "policy_sha256")
        if self.decision not in {"allowed", "blocked"}:
            raise ValueError("recovery receipt decision is invalid")
        try:
            evidence = json.loads(self.evidence_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("recovery receipt evidence is invalid JSON") from exc
        if not isinstance(evidence, dict) or _canonical_json(evidence) != self.evidence_json:
            raise ValueError("recovery receipt evidence is not canonical")
        expected = _digest(
            {
                "schema_version": 1,
                "work_item_id": self.work_item_id,
                "repository": self.repository,
                "issue_number": self.issue_number,
                "policy_sha256": self.policy_sha256,
                "recovery_profile": self.recovery_profile,
                "action": self.action,
                "decision": self.decision,
                "code": self.code,
                "evidence": evidence,
            }
        )
        if expected != self.receipt_sha256:
            raise ValueError("recovery receipt digest is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "receipt_sha256": self.receipt_sha256,
            "work_item_id": self.work_item_id,
            "repository": self.repository,
            "issue_number": self.issue_number,
            "policy_sha256": self.policy_sha256,
            "recovery_profile": self.recovery_profile,
            "action": self.action,
            "decision": self.decision,
            "code": self.code,
            "evidence": json.loads(self.evidence_json),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class RepositoryTargetReadbackVerdict:
    verdict_sha256: str
    work_item_id: str
    policy_sha256: str | None
    target_readback_profile: str
    action: str
    status: str
    code: str | None
    evidence_json: str
    evidence_sha256: str
    created_at: str

    def __post_init__(self) -> None:
        _validate_sha256(self.verdict_sha256, "verdict_sha256")
        _validate_sha256(self.evidence_sha256, "evidence_sha256")
        if self.policy_sha256 is not None:
            _validate_sha256(self.policy_sha256, "policy_sha256")
        if self.status not in {"passed", "blocked"}:
            raise ValueError("target-readback status is invalid")
        try:
            evidence = json.loads(self.evidence_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("target-readback evidence is invalid JSON") from exc
        if not isinstance(evidence, dict) or _canonical_json(evidence) != self.evidence_json:
            raise ValueError("target-readback evidence is not canonical")
        if sha256(self.evidence_json.encode("utf-8")).hexdigest() != self.evidence_sha256:
            raise ValueError("target-readback evidence digest is invalid")
        expected = _digest(
            {
                "schema_version": 1,
                "work_item_id": self.work_item_id,
                "policy_sha256": self.policy_sha256,
                "target_readback_profile": self.target_readback_profile,
                "action": self.action,
                "status": self.status,
                "code": self.code,
                "evidence_sha256": self.evidence_sha256,
            }
        )
        if expected != self.verdict_sha256:
            raise ValueError("target-readback verdict digest is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "verdict_sha256": self.verdict_sha256,
            "work_item_id": self.work_item_id,
            "policy_sha256": self.policy_sha256,
            "target_readback_profile": self.target_readback_profile,
            "action": self.action,
            "status": self.status,
            "code": self.code,
            "evidence": json.loads(self.evidence_json),
            "evidence_sha256": self.evidence_sha256,
            "created_at": self.created_at,
        }


def evaluate_repository_recovery(
    *,
    action: str,
    policy: RepositoryPolicyIdentity | None,
    task: TrackerTask,
    work_item: WorkItem | None,
    turn: Turn | None,
    pull_request: PullRequest | None,
    planning_code: str | None,
    created_at: str,
) -> RepositoryRecoveryReceipt:
    """Authorize one already-planned recovery using its frozen repository class."""
    if policy is None:
        profile = LEGACY_RECOVERY_PROFILE
        allowed_actions = _FIXTURE_RECOVERY_ACTIONS
        policy_sha256 = None
    elif policy.recovery_profile is RepositoryRecoveryProfile.FIXTURE_LIVE_V1:
        profile = policy.recovery_profile.value
        allowed_actions = _FIXTURE_RECOVERY_ACTIONS
        policy_sha256 = policy.policy_sha256
    else:
        profile = policy.recovery_profile.value
        allowed_actions = _HIGHER_VALUE_RECOVERY_ACTIONS
        policy_sha256 = policy.policy_sha256

    code = planning_code
    decision = "allowed"
    if action == "block":
        decision = "blocked"
        code = planning_code or "recovery_planner_blocked"
    elif action not in allowed_actions:
        decision = "blocked"
        code = "repository_recovery_action_not_allowed"
    elif policy is None and work_item is not None and (
        work_item.repository_policy_sha256 is not None
    ):
        decision = "blocked"
        code = "repository_policy_ledger_missing"
    elif policy is not None and (
        policy.repository != task.repository
        or policy.issue_number != task.issue_number
        or policy.issue_node_id != task.issue_node_id
    ):
        decision = "blocked"
        code = "repository_policy_target_conflict"
    elif policy is not None and work_item is not None and (
        work_item.repository_policy_sha256 != policy.policy_sha256
    ):
        decision = "blocked"
        code = "repository_policy_binding_conflict"
    elif work_item is not None and (
        work_item.repository != task.repository
        or work_item.issue_number != task.issue_number
        or work_item.issue_node_id != task.issue_node_id
    ):
        decision = "blocked"
        code = "repository_recovery_target_conflict"

    evidence = _identity_evidence(task, work_item, turn, pull_request)
    evidence["planning_code"] = planning_code
    evidence["policy_binding"] = (
        "legacy_unbound" if policy is None else "exact_digest"
    )
    evidence_json = _canonical_json(evidence)
    receipt_payload = {
        "schema_version": 1,
        "work_item_id": None if work_item is None else work_item.work_item_id,
        "repository": task.repository,
        "issue_number": task.issue_number,
        "policy_sha256": policy_sha256,
        "recovery_profile": profile,
        "action": action,
        "decision": decision,
        "code": code,
        "evidence": evidence,
    }
    return RepositoryRecoveryReceipt(
        receipt_sha256=_digest(receipt_payload),
        work_item_id=None if work_item is None else work_item.work_item_id,
        repository=task.repository,
        issue_number=task.issue_number,
        policy_sha256=policy_sha256,
        recovery_profile=profile,
        action=action,
        decision=decision,
        code=code,
        evidence_json=evidence_json,
        created_at=created_at,
    )


def evaluate_repository_target_readback(
    *,
    action: str,
    policy: RepositoryPolicyIdentity | None,
    task: TrackerTask,
    work_item: WorkItem,
    turn: Turn | None,
    pull_request: PullRequest | None,
    ledger_evidence: Mapping[str, object],
    created_at: str,
) -> RepositoryTargetReadbackVerdict:
    """Evaluate one exact target from provider readback plus durable local ledgers."""
    profile = (
        LEGACY_TARGET_READBACK_PROFILE
        if policy is None
        else policy.target_readback_profile.value
    )
    policy_sha256 = None if policy is None else policy.policy_sha256
    code: str | None = None
    if policy is None and work_item.repository_policy_sha256 is not None:
        code = "target_policy_ledger_missing"
    elif policy is not None and (
        policy.repository != task.repository
        or policy.issue_number != task.issue_number
        or policy.issue_node_id != task.issue_node_id
    ):
        code = "target_policy_identity_conflict"
    elif work_item.repository != task.repository:
        code = "target_repository_conflict"
    elif work_item.issue_number != task.issue_number:
        code = "target_issue_number_conflict"
    elif work_item.issue_node_id != task.issue_node_id:
        code = "target_issue_node_conflict"
    elif task.branch_name is not None and task.branch_name != work_item.task_branch:
        code = "target_task_branch_conflict"
    elif policy is not None and work_item.repository_policy_sha256 != policy.policy_sha256:
        code = "target_policy_binding_conflict"
    elif turn is not None and turn.work_item_id != work_item.work_item_id:
        code = "target_turn_binding_conflict"
    elif pull_request is not None:
        expected_url = (
            f"https://github.com/{work_item.repository}/pull/{pull_request.number}"
        )
        expected_head = work_item.last_published_sha or work_item.base_sha
        if (
            pull_request.url != expected_url
            or pull_request.branch_name != work_item.task_branch
            or pull_request.base_branch != work_item.base_branch
            or pull_request.is_cross_repository
            or pull_request.head_sha != expected_head
            or (
                work_item.pr_number is not None
                and pull_request.number != work_item.pr_number
            )
        ):
            code = "target_pull_request_conflict"
    if code is None:
        code = _ledger_evidence_error(
            action=action,
            ledger_evidence=ledger_evidence,
            work_item=work_item,
            turn=turn,
        )

    evidence = _identity_evidence(task, work_item, turn, pull_request)
    evidence["ledger_evidence"] = dict(ledger_evidence)
    evidence["policy_binding"] = (
        "legacy_unbound" if policy is None else "exact_digest"
    )
    evidence_json = _canonical_json(evidence)
    evidence_sha256 = sha256(evidence_json.encode("utf-8")).hexdigest()
    status = "passed" if code is None else "blocked"
    verdict_payload = {
        "schema_version": 1,
        "work_item_id": work_item.work_item_id,
        "policy_sha256": policy_sha256,
        "target_readback_profile": profile,
        "action": action,
        "status": status,
        "code": code,
        "evidence_sha256": evidence_sha256,
    }
    return RepositoryTargetReadbackVerdict(
        verdict_sha256=_digest(verdict_payload),
        work_item_id=work_item.work_item_id,
        policy_sha256=policy_sha256,
        target_readback_profile=profile,
        action=action,
        status=status,
        code=code,
        evidence_json=evidence_json,
        evidence_sha256=evidence_sha256,
        created_at=created_at,
    )


def _ledger_evidence_error(
    *,
    action: str,
    ledger_evidence: Mapping[str, object],
    work_item: WorkItem,
    turn: Turn | None,
) -> str | None:
    if action == "claim_binding":
        return (
            None
            if dict(ledger_evidence) == {"stage": "pre_runner_prepare"}
            else "target_claim_stage_evidence_invalid"
        )
    if set(ledger_evidence) != {
        "schema_version",
        "slack_deliveries",
        "actions_completion_gate",
        "runner_terminal_storage",
        "discard_request",
        "disposition",
        "terminal_github_closures",
    } or ledger_evidence.get("schema_version") != 2:
        return "target_ledger_evidence_invalid"

    deliveries = ledger_evidence.get("slack_deliveries")
    if not isinstance(deliveries, list):
        return "target_slack_ledger_invalid"
    for delivery in deliveries:
        if (
            not isinstance(delivery, dict)
            or delivery.get("work_item_id") != work_item.work_item_id
            or delivery.get("state") not in {"prepared", "delivered"}
            or not isinstance(delivery.get("deduplication_key"), str)
            or not isinstance(delivery.get("payload_sha256"), str)
        ):
            return "target_slack_ledger_invalid"

    gate = ledger_evidence.get("actions_completion_gate")
    if gate is not None and (
        not isinstance(gate, dict)
        or gate.get("work_item_id") != work_item.work_item_id
        or turn is None
        or gate.get("turn_id") != turn.turn_id
        or gate.get("head_sha") not in {work_item.base_sha, work_item.last_published_sha}
    ):
        return "target_actions_ledger_invalid"

    terminal = ledger_evidence.get("runner_terminal_storage")
    if not isinstance(terminal, dict) or set(terminal) != {"archive", "absence"}:
        return "target_runner_ledger_invalid"
    archive = terminal.get("archive")
    absence = terminal.get("absence")
    expected_head = work_item.last_published_sha or work_item.base_sha
    if archive is not None and (
        not isinstance(archive, dict)
        or archive.get("work_item_id") != work_item.work_item_id
        or archive.get("expected_head_sha") != expected_head
        or archive.get("status")
        not in {"prepared", "ambiguous", "archived", "blocked"}
    ):
        return "target_runner_archive_invalid"
    if absence is not None and (
        not isinstance(absence, dict)
        or absence.get("work_item_id") != work_item.work_item_id
        or absence.get("expected_head_sha") != expected_head
    ):
        return "target_runner_absence_invalid"
    if (
        isinstance(archive, dict)
        and archive.get("status") == "archived"
        and absence is not None
    ):
        return "target_runner_terminal_evidence_conflict"

    discard_request = ledger_evidence.get("discard_request")
    if discard_request is not None and (
        not isinstance(discard_request, dict)
        or set(discard_request)
        != {
            "work_item_id",
            "expected_head_sha",
            "pr_number",
            "requested_by",
            "request_event_id",
            "requested_at",
            "request_sha256",
        }
        or discard_request.get("work_item_id") != work_item.work_item_id
        or discard_request.get("expected_head_sha") != expected_head
        or not isinstance(discard_request.get("requested_by"), str)
        or not isinstance(discard_request.get("request_event_id"), str)
        or not isinstance(discard_request.get("requested_at"), str)
        or not _is_sha256(discard_request.get("request_sha256"))
        or not _optional_positive_int(discard_request.get("pr_number"))
    ):
        return "target_discard_request_ledger_invalid"

    disposition = ledger_evidence.get("disposition")
    if disposition is not None and (
        discard_request is None
        or not isinstance(disposition, dict)
        or set(disposition)
        != {
            "work_item_id",
            "kind",
            "expected_head_sha",
            "pr_number",
            "request_sha256",
            "reason_code",
        }
        or disposition.get("work_item_id") != work_item.work_item_id
        or disposition.get("expected_head_sha") != expected_head
        or disposition.get("pr_number") != discard_request.get("pr_number")
        or disposition.get("request_sha256")
        != discard_request.get("request_sha256")
        or disposition.get("kind") not in {"abandoned", "superseded"}
        or (
            disposition.get("kind") == "abandoned"
            and disposition.get("pr_number") is not None
        )
        or (
            disposition.get("kind") == "superseded"
            and not (
                type(disposition.get("pr_number")) is int
                and disposition.get("pr_number") > 0
            )
        )
        or disposition.get("reason_code") != "operator_agent_discard"
    ):
        return "target_disposition_ledger_invalid"

    closures = ledger_evidence.get("terminal_github_closures")
    if not isinstance(closures, list):
        return "target_terminal_github_ledger_invalid"
    seen_closure_kinds: set[str] = set()
    for closure in closures:
        if (
            not isinstance(closure, dict)
            or set(closure)
            != {
                "kind",
                "repository",
                "issue_number",
                "issue_node_id",
                "pr_number",
                "expected_head_sha",
                "close_reason",
                "state",
                "request_sha256",
                "outcome",
                "error_code",
            }
            or closure.get("kind")
            not in {"completed_issue", "discarded_pull_request", "discarded_issue"}
            or closure.get("kind") in seen_closure_kinds
            or closure.get("repository") != work_item.repository
            or closure.get("issue_number") != work_item.issue_number
            or closure.get("issue_node_id") != work_item.issue_node_id
            or closure.get("expected_head_sha") != expected_head
            or closure.get("state") not in {"prepared", "completed", "blocked"}
            or not _is_sha256(closure.get("request_sha256"))
        ):
            return "target_terminal_github_ledger_invalid"
        kind = str(closure["kind"])
        seen_closure_kinds.add(kind)
        state = closure.get("state")
        if (
            (state == "prepared" and (
                closure.get("outcome") is not None
                or closure.get("error_code") is not None
            ))
            or (state == "completed" and (
                closure.get("outcome") not in {"closed", "already_closed"}
                or closure.get("error_code") is not None
            ))
            or (state == "blocked" and (
                closure.get("outcome") is not None
                or not isinstance(closure.get("error_code"), str)
                or not closure.get("error_code")
            ))
        ):
            return "target_terminal_github_ledger_invalid"
        if kind == "discarded_pull_request":
            if (
                not isinstance(discard_request, dict)
                or discard_request.get("pr_number") is None
                or closure.get("pr_number") != discard_request.get("pr_number")
                or closure.get("close_reason") is not None
            ):
                return "target_terminal_github_ledger_invalid"
        else:
            if (
                closure.get("pr_number") is not None
                or closure.get("close_reason")
                != ("completed" if kind == "completed_issue" else "not_planned")
                or (
                    kind == "completed_issue"
                    and (
                        work_item.state.value != "completed"
                        or disposition is not None
                    )
                )
                or (kind == "discarded_issue" and disposition is None)
            ):
                return "target_terminal_github_ledger_invalid"
    return None


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _optional_positive_int(value: object) -> bool:
    return value is None or (type(value) is int and value > 0)


def _identity_evidence(
    task: TrackerTask,
    work_item: WorkItem | None,
    turn: Turn | None,
    pull_request: PullRequest | None,
) -> dict[str, object]:
    return {
        "task": {
            "repository": task.repository,
            "task_id": task.task_id,
            "issue_number": task.issue_number,
            "issue_node_id": task.issue_node_id,
            "state": task.state.value,
            "branch_name": task.branch_name,
        },
        "work_item": (
            None
            if work_item is None
            else {
                "work_item_id": work_item.work_item_id,
                "repository": work_item.repository,
                "issue_number": work_item.issue_number,
                "issue_node_id": work_item.issue_node_id,
                "state": work_item.state.value,
                "base_branch": work_item.base_branch,
                "task_branch": work_item.task_branch,
                "base_sha": work_item.base_sha,
                "last_published_sha": work_item.last_published_sha,
                "pr_number": work_item.pr_number,
                "repository_policy_sha256": work_item.repository_policy_sha256,
            }
        ),
        "turn": (
            None
            if turn is None
            else {
                "turn_id": turn.turn_id,
                "work_item_id": turn.work_item_id,
                "turn_number": turn.turn_number,
                "state": turn.state.value,
                "input_head_sha": turn.input_head_sha,
            }
        ),
        "pull_request": (
            None
            if pull_request is None
            else {
                "number": pull_request.number,
                "url": pull_request.url,
                "branch_name": pull_request.branch_name,
                "base_branch": pull_request.base_branch,
                "state": pull_request.state.value,
                "is_cross_repository": pull_request.is_cross_repository,
                "head_sha": pull_request.head_sha,
            }
        ),
    }
