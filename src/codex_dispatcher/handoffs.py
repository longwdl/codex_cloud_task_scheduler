"""Structured, provenance-separated handoffs between Codex session generations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Any
from uuid import uuid4

from codex_dispatcher.acceptance_evaluator import (
    AcceptanceStatus,
    evaluate_acceptance,
)
from codex_dispatcher.ci_evidence import (
    ActionsEvidenceSnapshot,
    ActionsRunEvidence,
    RequiredCheckStatus,
)
from codex_dispatcher.runner_protocol import (
    AgentResult,
    agent_result_to_mapping,
    agent_result_turn_status,
)
from codex_dispatcher.task_spec import (
    AcceptanceCriterion,
    normalize_repo_path,
    parse_acceptance_criteria,
)
from codex_dispatcher.work_items import (
    SessionGeneration,
    WorkItem,
    validate_git_sha,
    validate_branch,
    validate_repository,
    validate_session_generation_id,
    validate_sha256,
    validate_turn_id,
    validate_work_item_id,
)


_HANDOFF_ID_RE = re.compile(r"handoff_[0-9a-f]{32}")
MAX_HANDOFF_JSON_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class PublishedCheckpoint:
    """One Dispatcher-recorded task-branch publication event."""

    head_sha: str
    previous_sha: str
    bundle_sha256: str | None = None
    changed_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_git_sha(self.head_sha, "head_sha")
        validate_git_sha(self.previous_sha, "previous_sha")
        if self.head_sha == self.previous_sha:
            raise ValueError("published checkpoint must advance its previous SHA")
        if self.bundle_sha256 is None:
            if self.changed_paths:
                raise ValueError("legacy publication evidence cannot contain changed paths")
            return
        validate_sha256(self.bundle_sha256, "bundle_sha256")
        if not self.changed_paths or len(self.changed_paths) > 1_000:
            raise ValueError("publication changed paths must be a bounded non-empty tuple")
        normalized = tuple(normalize_repo_path(path) for path in self.changed_paths)
        if normalized != self.changed_paths or len(set(normalized)) != len(normalized):
            raise ValueError("publication changed paths must be normalized and unique")

    @property
    def has_complete_evidence(self) -> bool:
        return self.bundle_sha256 is not None


@dataclass(frozen=True, slots=True)
class PublicationEvidence:
    """Mechanically reconstructed evidence for the durable task-branch chain."""

    current_head_sha: str
    published_checkpoint_heads: tuple[str, ...]
    publication_evidence_complete: bool
    verified_changed_paths: tuple[str, ...]
    head_was_published: bool


def build_publication_evidence(
    *,
    work_item: WorkItem,
    published_checkpoints: tuple[PublishedCheckpoint, ...],
) -> PublicationEvidence:
    """Validate and summarize the additive publication ledger without guessing gaps."""
    if not isinstance(work_item, WorkItem):
        raise TypeError("work_item must be a WorkItem")
    if (
        not isinstance(published_checkpoints, tuple)
        or any(
            not isinstance(item, PublishedCheckpoint)
            for item in published_checkpoints
        )
    ):
        raise TypeError(
            "published_checkpoints must contain PublishedCheckpoint values"
        )
    current_head_sha = work_item.last_published_sha or work_item.base_sha
    for previous, current in zip(
        published_checkpoints, published_checkpoints[1:]
    ):
        if current.previous_sha != previous.head_sha:
            raise ValueError(
                "publication checkpoint evidence is not one ordered chain"
            )
    if (
        published_checkpoints
        and published_checkpoints[-1].head_sha != current_head_sha
    ):
        raise ValueError("publication checkpoint evidence does not reach current HEAD")

    published_checkpoint_heads = [
        item.head_sha for item in published_checkpoints
    ]
    if (
        work_item.last_published_sha is not None
        and current_head_sha not in published_checkpoint_heads
    ):
        # Schema-7 publication anchors can predate the additive evidence ledger.
        published_checkpoint_heads.append(current_head_sha)
    verified_changed_paths = tuple(
        sorted(
            {
                path
                for checkpoint in published_checkpoints
                if checkpoint.has_complete_evidence
                for path in checkpoint.changed_paths
            }
        )
    )
    complete = (
        all(item.has_complete_evidence for item in published_checkpoints)
        and (
            work_item.last_published_sha is None
            or (
                bool(published_checkpoints)
                and published_checkpoints[0].previous_sha == work_item.base_sha
            )
        )
        and (
            not published_checkpoint_heads
            or published_checkpoint_heads[-1] == current_head_sha
        )
    )
    return PublicationEvidence(
        current_head_sha=current_head_sha,
        published_checkpoint_heads=tuple(published_checkpoint_heads),
        publication_evidence_complete=complete,
        verified_changed_paths=verified_changed_paths,
        head_was_published=work_item.last_published_sha is not None,
    )


@dataclass(frozen=True, slots=True)
class SessionHandoffSnapshot:
    """Canonical handoff bytes durably bound to exactly one generation replacement."""

    handoff_id: str
    work_item_id: str
    from_session_generation_id: str
    to_session_generation_id: str
    trusted_facts_json: str
    untrusted_advisory_json: str
    handoff_sha256: str
    created_at: str

    def __post_init__(self) -> None:
        validate_handoff_id(self.handoff_id)
        validate_work_item_id(self.work_item_id)
        validate_session_generation_id(self.from_session_generation_id)
        validate_session_generation_id(self.to_session_generation_id)
        if self.from_session_generation_id == self.to_session_generation_id:
            raise ValueError("handoff source and target generation must differ")
        validate_sha256(self.handoff_sha256, "handoff_sha256")
        if (
            not isinstance(self.created_at, str)
            or not self.created_at
            or len(self.created_at) > 64
            or any(ord(character) < 32 or ord(character) == 127 for character in self.created_at)
        ):
            raise ValueError("created_at must be non-empty bounded text")
        trusted = _load_canonical_object(self.trusted_facts_json, "trusted handoff facts")
        advisory = _load_canonical_object(
            self.untrusted_advisory_json, "untrusted handoff advisory"
        )
        _validate_trusted_facts(trusted)
        _validate_untrusted_advisory(advisory)
        if trusted["schema_version"] == 2:
            observed_at = trusted["ci_evidence"]["observed_at"]
            if observed_at is not None and _aware_timestamp(
                observed_at, "CI observed_at"
            ) > _aware_timestamp(self.created_at, "handoff created_at"):
                raise ValueError("CI evidence observation occurs after Handoff creation")
        if (
            trusted["work_item"]["work_item_id"] != self.work_item_id
            or trusted["generation"]["from_session_generation_id"]
            != self.from_session_generation_id
            or trusted["generation"]["to_session_generation_id"]
            != self.to_session_generation_id
            or advisory["source_session_generation_id"]
            != self.from_session_generation_id
        ):
            raise ValueError("handoff JSON identity conflicts with its durable binding")
        expected = _handoff_digest(trusted, advisory)
        if self.handoff_sha256 != expected:
            raise ValueError("handoff_sha256 does not match its canonical content")

    @property
    def trusted_facts(self) -> dict[str, Any]:
        return _load_canonical_object(self.trusted_facts_json, "trusted handoff facts")

    @property
    def untrusted_advisory(self) -> dict[str, Any]:
        return _load_canonical_object(
            self.untrusted_advisory_json, "untrusted handoff advisory"
        )

    @property
    def to_generation_number(self) -> int:
        return int(self.trusted_facts["generation"]["to_generation_number"])

    @property
    def from_generation_number(self) -> int:
        return int(self.trusted_facts["generation"]["from_generation_number"])

    @property
    def current_head_sha(self) -> str:
        return str(self.trusted_facts["git"]["current_head_sha"])

    @property
    def issue_revision(self) -> str:
        return str(self.trusted_facts["issue"]["revision"])

    @property
    def issue_content_sha256(self) -> str:
        return str(self.trusted_facts["issue"]["content_sha256"])

    @property
    def task_spec_sha256(self) -> str:
        return str(self.trusted_facts["issue"]["task_spec_sha256"])

    @property
    def approved_context_sha256(self) -> str:
        return str(self.trusted_facts["issue"]["approved_context_sha256"])

    @property
    def to_agent_policy_sha256(self) -> str:
        return str(self.trusted_facts["generation"]["to_agent_policy_sha256"])


def build_session_handoff_snapshot(
    *,
    work_item: WorkItem,
    from_generation: SessionGeneration,
    to_session_generation_id: str,
    to_generation_number: int,
    to_agent_policy_sha256: str,
    rotation_reason: str,
    issue_revision: str,
    issue_content_sha256: str,
    task_spec_sha256: str,
    approved_context_sha256: str,
    acceptance_criteria: str,
    required_checks: tuple[str, ...],
    published_checkpoints: tuple[PublishedCheckpoint, ...],
    source_turn_id: str | None,
    source_result_status: str | None,
    source_result_summary: str | None,
    source_agent_result: AgentResult | None,
    created_at: str,
    handoff_id: str | None = None,
    acceptance_items: tuple[AcceptanceCriterion, ...] | None = None,
    allowed_paths: tuple[str, ...] = (),
    actions_evidence: ActionsEvidenceSnapshot | None = None,
) -> SessionHandoffSnapshot:
    """Build a canonical handoff without promoting Agent output to trusted facts."""
    if not isinstance(work_item, WorkItem):
        raise TypeError("work_item must be a WorkItem")
    if not isinstance(from_generation, SessionGeneration):
        raise TypeError("from_generation must be a SessionGeneration")
    validate_session_generation_id(to_session_generation_id)
    validate_sha256(to_agent_policy_sha256, "to_agent_policy_sha256")
    if from_generation.work_item_id != work_item.work_item_id:
        raise ValueError("from generation does not belong to the WorkItem")
    if to_session_generation_id == from_generation.session_generation_id:
        raise ValueError("handoff source and target generation must differ")
    if to_generation_number != from_generation.generation_number + 1:
        raise ValueError("handoff target generation must immediately follow its source")
    if source_turn_id is not None:
        validate_turn_id(source_turn_id)
    if (
        not isinstance(rotation_reason, str)
        or not rotation_reason
        or len(rotation_reason) > 256
        or _contains_control(rotation_reason)
    ):
        raise ValueError("rotation_reason must be non-empty bounded text")
    for field, digest in (
        ("issue_content_sha256", issue_content_sha256),
        ("task_spec_sha256", task_spec_sha256),
        ("approved_context_sha256", approved_context_sha256),
    ):
        validate_sha256(digest, field)
    for field, value, maximum in (
        ("issue_revision", issue_revision, 256),
        ("acceptance_criteria", acceptance_criteria, 256 * 1024),
        ("created_at", created_at, 64),
    ):
        if (
            not isinstance(value, str)
            or not value
            or len(value) > maximum
            or _contains_control(value, allow_newlines=field == "acceptance_criteria")
        ):
            raise ValueError(f"{field} must be non-empty bounded text")
    if (
        not isinstance(required_checks, tuple)
        or not required_checks
        or len(required_checks) > 100
        or len(set(required_checks)) != len(required_checks)
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 256
            or _contains_control(item)
            for item in required_checks
        )
    ):
        raise ValueError("required_checks must be a bounded unique non-empty tuple")
    if (
        not isinstance(published_checkpoints, tuple)
        or any(not isinstance(item, PublishedCheckpoint) for item in published_checkpoints)
    ):
        raise TypeError("published_checkpoints must contain PublishedCheckpoint values")
    if acceptance_items is None:
        acceptance_items = parse_acceptance_criteria(acceptance_criteria)
    if (
        not isinstance(acceptance_items, tuple)
        or not acceptance_items
        or any(not isinstance(item, AcceptanceCriterion) for item in acceptance_items)
    ):
        raise TypeError("acceptance_items must be a non-empty criterion tuple")
    if (
        not isinstance(allowed_paths, tuple)
        or any(not isinstance(path, str) for path in allowed_paths)
    ):
        raise TypeError("allowed_paths must be a tuple of repository paths")
    if source_agent_result is not None:
        if (
            source_turn_id is None
            or source_result_status != agent_result_turn_status(source_agent_result)
            or source_result_summary != source_agent_result.summary
        ):
            raise ValueError("source Agent result conflicts with its Turn receipt")
    elif (source_result_status is None) != (source_result_summary is None):
        raise ValueError("source Turn result status and summary must be recorded together")

    current_head_sha = work_item.last_published_sha or work_item.base_sha
    generation_head_sha = from_generation.last_published_sha or from_generation.start_head_sha
    if current_head_sha != generation_head_sha:
        raise ValueError("WorkItem and source generation publication anchors differ")
    if actions_evidence is not None:
        if not isinstance(actions_evidence, ActionsEvidenceSnapshot):
            raise TypeError("actions_evidence must be an ActionsEvidenceSnapshot or None")
        if (
            actions_evidence.repository != work_item.repository
            or actions_evidence.task_branch != work_item.task_branch
            or actions_evidence.head_sha != current_head_sha
            or tuple(item.name for item in actions_evidence.required_checks)
            != required_checks
        ):
            raise ValueError("Actions evidence conflicts with the Handoff target")

    publication = build_publication_evidence(
        work_item=work_item,
        published_checkpoints=published_checkpoints,
    )
    published_checkpoint_heads = list(publication.published_checkpoint_heads)
    publication_evidence_complete = publication.publication_evidence_complete
    verified_changed_paths = publication.verified_changed_paths
    if actions_evidence is None:
        required_check_facts = [
            {"name": name, "status": RequiredCheckStatus.NOT_OBSERVED.value}
            for name in required_checks
        ]
    else:
        required_check_facts = [
            {"name": item.name, "status": item.status.value}
            for item in actions_evidence.required_checks
        ]
    acceptance = evaluate_acceptance(
        criteria=acceptance_items,
        configured_required_checks=required_checks,
        allowed_paths=allowed_paths,
        publication_evidence_complete=publication_evidence_complete,
        verified_changed_paths=verified_changed_paths,
        head_was_published=work_item.last_published_sha is not None,
        actions_evidence=actions_evidence,
    )
    remaining_work: list[dict[str, str]] = [
        {
            "kind": "acceptance_criterion",
            "status": "remaining",
            "summary": (
                f"{item.criterion_id} [{item.status.value}]: {item.description}"
            ),
        }
        for item in acceptance.criteria
        if item.status is not AcceptanceStatus.PASSED
    ]
    remaining_work.extend(
        {
            "kind": "required_check_observation",
            "status": "remaining",
            "summary": f"Observe required check '{name}' for the current HEAD.",
        }
        for name, status in (
            (item["name"], item["status"]) for item in required_check_facts
        )
        if status != RequiredCheckStatus.PASSED.value
    )
    ci_runs = (
        []
        if actions_evidence is None
        else [
            _actions_run_mapping(item.run)
            for item in actions_evidence.required_checks
            if item.run is not None
        ]
    )
    trusted: dict[str, Any] = {
        "acceptance": acceptance.to_mapping(
            criteria_sha256=sha256(acceptance_criteria.encode("utf-8")).hexdigest()
        ),
        "ci_evidence": {
            "head_sha": current_head_sha,
            "observed_at": (
                actions_evidence.observed_at if actions_evidence is not None else None
            ),
            "provider": (
                "github_actions" if actions_evidence is not None else "not_available"
            ),
            "remote_ref_sha": (
                actions_evidence.remote_ref_sha
                if actions_evidence is not None
                else None
            ),
            "repository": work_item.repository,
            "runs": ci_runs,
            "task_branch": work_item.task_branch,
        },
        "generation": {
            "from_generation_number": from_generation.generation_number,
            "from_session_generation_id": from_generation.session_generation_id,
            "rotation_reason": rotation_reason,
            "to_agent_policy_sha256": to_agent_policy_sha256,
            "to_generation_number": to_generation_number,
            "to_session_generation_id": to_session_generation_id,
        },
        "git": {
            "base_sha": work_item.base_sha,
            "current_head_sha": current_head_sha,
            "publication_evidence_complete": publication_evidence_complete,
            "published_checkpoint_heads": published_checkpoint_heads,
            "task_branch": work_item.task_branch,
            "verified_changed_paths": list(verified_changed_paths),
        },
        "issue": {
            "approved_context_sha256": approved_context_sha256,
            "content_sha256": issue_content_sha256,
            "revision": issue_revision,
            "revision_sha256": sha256(issue_revision.encode("utf-8")).hexdigest(),
            "task_spec_sha256": task_spec_sha256,
        },
        "provenance": "dispatcher_git_ci_state",
        "remaining_work": remaining_work,
        "required_checks": required_check_facts,
        "schema_version": 2,
        "work_item": {
            "issue_number": work_item.issue_number,
            "repository": work_item.repository,
            "work_item_id": work_item.work_item_id,
        },
    }

    if source_agent_result is not None:
        previous_result: dict[str, Any] | None = agent_result_to_mapping(source_agent_result)
        result_receipt_complete = True
    elif source_result_status is not None and source_result_summary is not None:
        previous_result = {
            "status": source_result_status,
            "summary": source_result_summary,
        }
        result_receipt_complete = False
    else:
        previous_result = None
        result_receipt_complete = False
    advisory: dict[str, Any] = {
        "classification": "untrusted_advisory",
        "previous_result": previous_result,
        "recommended_next_action": (
            source_agent_result.next_step if source_agent_result is not None else ""
        ),
        "rejected_approaches": [],
        "result_receipt_complete": result_receipt_complete,
        "schema_version": 1,
        "source_session_generation_id": from_generation.session_generation_id,
        "source_turn_id": source_turn_id,
        "stable_decisions": [],
        "suspected_root_causes": [],
    }
    trusted_json = _canonical_json(trusted)
    advisory_json = _canonical_json(advisory)
    return SessionHandoffSnapshot(
        handoff_id=handoff_id or f"handoff_{uuid4().hex}",
        work_item_id=work_item.work_item_id,
        from_session_generation_id=from_generation.session_generation_id,
        to_session_generation_id=to_session_generation_id,
        trusted_facts_json=trusted_json,
        untrusted_advisory_json=advisory_json,
        handoff_sha256=_handoff_digest(trusted, advisory),
        created_at=created_at,
    )


def validate_handoff_id(value: str) -> str:
    if not isinstance(value, str) or _HANDOFF_ID_RE.fullmatch(value) is None:
        raise ValueError("handoff_id must be a canonical handoff identifier")
    return value


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


def _handoff_digest(trusted: dict[str, Any], advisory: dict[str, Any]) -> str:
    return sha256(
        _canonical_json(
            {"trusted_facts": trusted, "untrusted_advisory": advisory}
        ).encode("utf-8")
    ).hexdigest()


def _canonical_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_HANDOFF_JSON_BYTES:
        raise ValueError("handoff JSON exceeds its safe boundary")
    return encoded


def _load_canonical_object(value: object, field: str) -> dict[str, Any]:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > MAX_HANDOFF_JSON_BYTES
    ):
        raise ValueError(f"{field} exceeds its safe boundary")
    try:
        parsed = json.loads(value, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must be valid JSON") from exc
    if not isinstance(parsed, dict) or _canonical_json(parsed) != value:
        raise ValueError(f"{field} must be a canonical JSON object")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate handoff JSON field: {key}")
        result[key] = value
    return result


def _validate_trusted_facts(value: dict[str, Any]) -> None:
    schema_version = value.get("schema_version")
    if schema_version == 1:
        _validate_trusted_facts_v1(value)
        return
    if schema_version == 2:
        _validate_trusted_facts_v2(value)
        return
    raise ValueError("trusted handoff facts have an unsupported schema")


def _validate_trusted_facts_v1(value: dict[str, Any]) -> None:
    expected = {
        "acceptance",
        "generation",
        "git",
        "issue",
        "provenance",
        "remaining_work",
        "required_checks",
        "schema_version",
        "work_item",
    }
    if set(value) != expected or value["schema_version"] != 1:
        raise ValueError("trusted handoff facts have an unsupported schema")
    if value["provenance"] != "dispatcher_git_state":
        raise ValueError("trusted handoff facts have invalid provenance")
    work_item = _exact_object(
        value["work_item"], {"issue_number", "repository", "work_item_id"}, "work_item"
    )
    validate_work_item_id(work_item["work_item_id"])
    validate_repository(work_item["repository"])
    if type(work_item["issue_number"]) is not int or work_item["issue_number"] <= 0:
        raise ValueError("handoff issue_number must be positive")
    generation = _exact_object(
        value["generation"],
        {
            "from_generation_number",
            "from_session_generation_id",
            "rotation_reason",
            "to_agent_policy_sha256",
            "to_generation_number",
            "to_session_generation_id",
        },
        "generation",
    )
    validate_session_generation_id(generation["from_session_generation_id"])
    validate_session_generation_id(generation["to_session_generation_id"])
    if generation["from_session_generation_id"] == generation["to_session_generation_id"]:
        raise ValueError("handoff source and target generation must differ")
    validate_sha256(generation["to_agent_policy_sha256"], "to_agent_policy_sha256")
    if (
        type(generation["from_generation_number"]) is not int
        or type(generation["to_generation_number"]) is not int
        or generation["to_generation_number"] != generation["from_generation_number"] + 1
    ):
        raise ValueError("handoff generation numbers are not consecutive")
    issue = _exact_object(
        value["issue"],
        {
            "approved_context_sha256",
            "content_sha256",
            "revision",
            "revision_sha256",
            "task_spec_sha256",
        },
        "issue",
    )
    for field in (
        "approved_context_sha256",
        "content_sha256",
        "revision_sha256",
        "task_spec_sha256",
    ):
        validate_sha256(issue[field], field)
    if not isinstance(issue["revision"], str) or not issue["revision"]:
        raise ValueError("handoff Issue revision must be non-empty text")
    if sha256(issue["revision"].encode("utf-8")).hexdigest() != issue["revision_sha256"]:
        raise ValueError("handoff Issue revision digest is invalid")
    git = _exact_object(
        value["git"],
        {
            "base_sha",
            "current_head_sha",
            "publication_evidence_complete",
            "published_checkpoint_heads",
            "task_branch",
            "verified_changed_paths",
        },
        "git",
    )
    validate_git_sha(git["base_sha"], "base_sha")
    validate_git_sha(git["current_head_sha"], "current_head_sha")
    validate_branch(git["task_branch"])
    if type(git["publication_evidence_complete"]) is not bool:
        raise ValueError("publication_evidence_complete must be boolean")
    checkpoint_heads = _string_list(
        git["published_checkpoint_heads"], "published_checkpoint_heads"
    )
    for sha_value in checkpoint_heads:
        validate_git_sha(sha_value, "published_checkpoint_head")
    if checkpoint_heads and checkpoint_heads[-1] != git["current_head_sha"]:
        raise ValueError("published_checkpoint_heads do not reach current_head_sha")
    if (
        git["publication_evidence_complete"]
        and not checkpoint_heads
        and git["current_head_sha"] != git["base_sha"]
    ):
        raise ValueError("complete publication evidence cannot omit a checkpoint HEAD")
    paths = _string_list(git["verified_changed_paths"], "verified_changed_paths")
    if paths != sorted(paths) or tuple(normalize_repo_path(path) for path in paths) != tuple(paths):
        raise ValueError("verified_changed_paths must be canonical")
    acceptance = _exact_object(
        value["acceptance"],
        {"criteria_sha256", "evidence", "status"},
        "acceptance",
    )
    if acceptance["status"] != "unverified" or acceptance["evidence"] != []:
        raise ValueError("Dispatcher must not invent acceptance evidence")
    validate_sha256(acceptance["criteria_sha256"], "acceptance criteria digest")
    checks = value["required_checks"]
    if not isinstance(checks, list) or not checks or len(checks) > 100:
        raise ValueError("required_checks must be non-empty")
    for check in checks:
        item = _exact_object(check, {"name", "status"}, "required check")
        if (
            not isinstance(item["name"], str)
            or not item["name"]
            or item["status"] != "not_observed"
        ):
            raise ValueError("required check evidence is invalid")
    remaining = value["remaining_work"]
    if not isinstance(remaining, list) or not remaining:
        raise ValueError("remaining_work must be non-empty")
    for item in remaining:
        parsed = _exact_object(item, {"kind", "status", "summary"}, "remaining work")
        if parsed["status"] != "remaining" or not isinstance(parsed["summary"], str):
            raise ValueError("remaining_work contains invalid state")


def _validate_trusted_facts_v2(value: dict[str, Any]) -> None:
    expected = {
        "acceptance",
        "ci_evidence",
        "generation",
        "git",
        "issue",
        "provenance",
        "remaining_work",
        "required_checks",
        "schema_version",
        "work_item",
    }
    if set(value) != expected or value["schema_version"] != 2:
        raise ValueError("trusted handoff facts have an unsupported schema")
    if value["provenance"] != "dispatcher_git_ci_state":
        raise ValueError("trusted handoff facts have invalid provenance")
    work_item = _exact_object(
        value["work_item"], {"issue_number", "repository", "work_item_id"}, "work_item"
    )
    validate_work_item_id(work_item["work_item_id"])
    validate_repository(work_item["repository"])
    if type(work_item["issue_number"]) is not int or work_item["issue_number"] <= 0:
        raise ValueError("handoff issue_number must be positive")
    generation = _exact_object(
        value["generation"],
        {
            "from_generation_number",
            "from_session_generation_id",
            "rotation_reason",
            "to_agent_policy_sha256",
            "to_generation_number",
            "to_session_generation_id",
        },
        "generation",
    )
    validate_session_generation_id(generation["from_session_generation_id"])
    validate_session_generation_id(generation["to_session_generation_id"])
    if generation["from_session_generation_id"] == generation["to_session_generation_id"]:
        raise ValueError("handoff source and target generation must differ")
    validate_sha256(generation["to_agent_policy_sha256"], "to_agent_policy_sha256")
    if (
        type(generation["from_generation_number"]) is not int
        or type(generation["to_generation_number"]) is not int
        or generation["to_generation_number"] != generation["from_generation_number"] + 1
    ):
        raise ValueError("handoff generation numbers are not consecutive")
    issue = _exact_object(
        value["issue"],
        {
            "approved_context_sha256",
            "content_sha256",
            "revision",
            "revision_sha256",
            "task_spec_sha256",
        },
        "issue",
    )
    for field in (
        "approved_context_sha256",
        "content_sha256",
        "revision_sha256",
        "task_spec_sha256",
    ):
        validate_sha256(issue[field], field)
    if not isinstance(issue["revision"], str) or not issue["revision"]:
        raise ValueError("handoff Issue revision must be non-empty text")
    if sha256(issue["revision"].encode("utf-8")).hexdigest() != issue["revision_sha256"]:
        raise ValueError("handoff Issue revision digest is invalid")
    git = _exact_object(
        value["git"],
        {
            "base_sha",
            "current_head_sha",
            "publication_evidence_complete",
            "published_checkpoint_heads",
            "task_branch",
            "verified_changed_paths",
        },
        "git",
    )
    validate_git_sha(git["base_sha"], "base_sha")
    validate_git_sha(git["current_head_sha"], "current_head_sha")
    validate_branch(git["task_branch"])
    if type(git["publication_evidence_complete"]) is not bool:
        raise ValueError("publication_evidence_complete must be boolean")
    checkpoint_heads = _string_list(
        git["published_checkpoint_heads"], "published_checkpoint_heads"
    )
    for sha_value in checkpoint_heads:
        validate_git_sha(sha_value, "published_checkpoint_head")
    if checkpoint_heads and checkpoint_heads[-1] != git["current_head_sha"]:
        raise ValueError("published_checkpoint_heads do not reach current_head_sha")
    if (
        git["publication_evidence_complete"]
        and not checkpoint_heads
        and git["current_head_sha"] != git["base_sha"]
    ):
        raise ValueError("complete publication evidence cannot omit a checkpoint HEAD")
    paths = _string_list(git["verified_changed_paths"], "verified_changed_paths")
    if paths != sorted(paths) or tuple(normalize_repo_path(path) for path in paths) != tuple(paths):
        raise ValueError("verified_changed_paths must be canonical")

    ci = _exact_object(
        value["ci_evidence"],
        {
            "head_sha",
            "observed_at",
            "provider",
            "remote_ref_sha",
            "repository",
            "runs",
            "task_branch",
        },
        "ci_evidence",
    )
    if (
        ci["repository"] != work_item["repository"]
        or ci["task_branch"] != git["task_branch"]
        or ci["head_sha"] != git["current_head_sha"]
    ):
        raise ValueError("CI evidence identity conflicts with trusted Git facts")
    runs_value = ci["runs"]
    if not isinstance(runs_value, list) or len(runs_value) > 1_000:
        raise ValueError("CI evidence runs must be a bounded array")
    runs: list[ActionsRunEvidence] = []
    for run_value in runs_value:
        run_object = _exact_object(
            run_value,
            {
                "conclusion",
                "created_at",
                "event",
                "head_branch",
                "head_repository",
                "head_sha",
                "html_url",
                "name",
                "repository",
                "run_attempt",
                "run_id",
                "status",
                "updated_at",
                "workflow_id",
            },
            "Actions run",
        )
        runs.append(
            ActionsRunEvidence(
                name=run_object["name"],
                workflow_id=run_object["workflow_id"],
                run_id=run_object["run_id"],
                run_attempt=run_object["run_attempt"],
                repository=run_object["repository"],
                head_repository=run_object["head_repository"],
                head_branch=run_object["head_branch"],
                head_sha=run_object["head_sha"],
                event=run_object["event"],
                status=run_object["status"],
                conclusion=run_object["conclusion"],
                created_at=run_object["created_at"],
                updated_at=run_object["updated_at"],
                html_url=run_object["html_url"],
            )
        )
    if len({run.run_id for run in runs}) != len(runs):
        raise ValueError("CI evidence runs contain duplicate run ids")
    if ci["provider"] == "not_available":
        if ci["observed_at"] is not None or ci["remote_ref_sha"] is not None or runs:
            raise ValueError("unavailable CI evidence must not contain provider observations")
    elif ci["provider"] == "github_actions":
        validate_git_sha(ci["remote_ref_sha"], "remote_ref_sha")
        if ci["remote_ref_sha"] != ci["head_sha"]:
            raise ValueError("CI remote ref does not equal the observed HEAD")
        observed_at = _aware_timestamp(ci["observed_at"], "CI observed_at")
        for run in runs:
            if (
                run.repository != ci["repository"]
                or run.head_repository != ci["repository"]
                or run.head_branch != ci["task_branch"]
                or run.head_sha != ci["head_sha"]
            ):
                raise ValueError("CI run identity conflicts with its evidence snapshot")
            if _aware_timestamp(
                run.updated_at, "Actions run updated_at"
            ) > observed_at:
                raise ValueError("CI run update occurs after its evidence observation")
    else:
        raise ValueError("CI evidence provider is unsupported")

    checks = value["required_checks"]
    if not isinstance(checks, list) or not checks or len(checks) > 100:
        raise ValueError("required_checks must be non-empty")
    run_by_name = {run.name: run for run in runs}
    if len(run_by_name) != len(runs):
        raise ValueError("CI evidence contains duplicate workflow names")
    check_names: list[str] = []
    for check in checks:
        item = _exact_object(check, {"name", "status"}, "required check")
        if not isinstance(item["name"], str) or not item["name"]:
            raise ValueError("required check name is invalid")
        check_names.append(item["name"])
        try:
            status = RequiredCheckStatus(item["status"])
        except (TypeError, ValueError) as exc:
            raise ValueError("required check status is invalid") from exc
        run = run_by_name.get(item["name"])
        if status is RequiredCheckStatus.NOT_OBSERVED:
            if run is not None:
                raise ValueError("not_observed required check contains Actions evidence")
        elif run is None or run.check_status is not status:
            raise ValueError("required check status conflicts with Actions evidence")
    if len(set(check_names)) != len(check_names):
        raise ValueError("required_checks must not contain duplicates")
    if set(run_by_name) != {
        item["name"] for item in checks if item["status"] != "not_observed"
    }:
        raise ValueError("CI runs do not exactly cover observed required checks")
    if ci["provider"] == "not_available" and any(
        item["status"] != "not_observed" for item in checks
    ):
        raise ValueError("unavailable CI provider cannot assert check observations")

    acceptance = _exact_object(
        value["acceptance"], {"criteria", "criteria_sha256", "status"}, "acceptance"
    )
    validate_sha256(acceptance["criteria_sha256"], "acceptance criteria digest")
    criteria_value = acceptance["criteria"]
    if not isinstance(criteria_value, list) or not criteria_value or len(criteria_value) > 100:
        raise ValueError("acceptance criteria evaluations must be a bounded non-empty array")
    criterion_ids: list[str] = []
    statuses: list[AcceptanceStatus] = []
    valid_action_refs = {run.evidence_ref for run in runs}
    fixed_refs = {
        "dispatcher-publication-ledger",
        "dispatcher-work-item:last-published-sha",
    }
    for criterion in criteria_value:
        item = _exact_object(
            criterion,
            {
                "argument",
                "criterion_id",
                "description",
                "evidence_refs",
                "predicate",
                "reason",
                "status",
            },
            "acceptance criterion",
        )
        if (
            not isinstance(item["criterion_id"], str)
            or not item["criterion_id"]
            or not isinstance(item["description"], str)
            or not item["description"]
            or item["predicate"]
            not in {
                "audit",
                "manual",
                "required-check",
                "changed-paths-within-allowed",
                "task-head-published",
            }
            or (item["argument"] is not None and not isinstance(item["argument"], str))
            or not isinstance(item["reason"], str)
            or not item["reason"]
        ):
            raise ValueError("acceptance criterion evaluation is invalid")
        refs = _string_list(item["evidence_refs"], "acceptance evidence_refs")
        if any(ref not in valid_action_refs | fixed_refs for ref in refs):
            raise ValueError("acceptance criterion cites unknown trusted evidence")
        criterion_ids.append(item["criterion_id"])
        try:
            statuses.append(AcceptanceStatus(item["status"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("acceptance criterion status is invalid") from exc
    if len(set(criterion_ids)) != len(criterion_ids):
        raise ValueError("acceptance criterion ids must be unique")
    aggregate = AcceptanceStatus.PASSED
    for candidate in (
        AcceptanceStatus.FAILED,
        AcceptanceStatus.UNVERIFIED,
        AcceptanceStatus.PENDING,
    ):
        if candidate in statuses:
            aggregate = candidate
            break
    if acceptance["status"] != aggregate.value:
        raise ValueError("acceptance aggregate status conflicts with criterion evaluations")

    remaining = value["remaining_work"]
    if not isinstance(remaining, list) or len(remaining) > 1_000:
        raise ValueError("remaining_work must be a bounded array")
    for remaining_item in remaining:
        parsed = _exact_object(
            remaining_item, {"kind", "status", "summary"}, "remaining work"
        )
        if (
            parsed["status"] != "remaining"
            or not isinstance(parsed["kind"], str)
            or not parsed["kind"]
            or not isinstance(parsed["summary"], str)
            or not parsed["summary"]
        ):
            raise ValueError("remaining_work contains invalid state")


def _validate_untrusted_advisory(value: dict[str, Any]) -> None:
    expected = {
        "classification",
        "previous_result",
        "recommended_next_action",
        "rejected_approaches",
        "result_receipt_complete",
        "schema_version",
        "source_session_generation_id",
        "source_turn_id",
        "stable_decisions",
        "suspected_root_causes",
    }
    if set(value) != expected or value["schema_version"] != 1:
        raise ValueError("untrusted handoff advisory has an unsupported schema")
    if value["classification"] != "untrusted_advisory":
        raise ValueError("handoff advisory must remain explicitly untrusted")
    validate_session_generation_id(value["source_session_generation_id"])
    if value["source_turn_id"] is not None:
        validate_turn_id(value["source_turn_id"])
    if type(value["result_receipt_complete"]) is not bool:
        raise ValueError("result_receipt_complete must be boolean")
    if not isinstance(value["recommended_next_action"], str):
        raise ValueError("recommended_next_action must be text")
    for field in ("stable_decisions", "rejected_approaches", "suspected_root_causes"):
        _string_list(value[field], field)
    if value["previous_result"] is not None and not isinstance(value["previous_result"], dict):
        raise ValueError("previous_result must be an object or null")


def _exact_object(value: object, fields: set[str], description: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"handoff {description} has unexpected or missing fields")
    return value


def _string_list(value: object, description: str) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > 1_000
        or any(not isinstance(item, str) for item in value)
    ):
        raise ValueError(f"handoff {description} must be a bounded string array")
    if len(set(value)) != len(value):
        raise ValueError(f"handoff {description} must not contain duplicates")
    return value


def _contains_control(value: str, *, allow_newlines: bool = False) -> bool:
    allowed = {9, 10, 13} if allow_newlines else set()
    return any(
        (ord(character) < 32 and ord(character) not in allowed)
        or ord(character) == 127
        for character in value
    )


def _aware_timestamp(value: object, field: str) -> datetime:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or _contains_control(value)
    ):
        raise ValueError(f"{field} must be non-empty bounded text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed
