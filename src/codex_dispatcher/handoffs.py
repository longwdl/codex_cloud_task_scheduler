"""Structured, provenance-separated handoffs between Codex session generations."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any
from uuid import uuid4

from codex_dispatcher.runner_protocol import AgentResult, agent_result_to_mapping
from codex_dispatcher.task_spec import normalize_repo_path
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
    if source_agent_result is not None:
        if (
            source_turn_id is None
            or source_result_status != source_agent_result.status.value
            or source_result_summary != source_agent_result.summary
        ):
            raise ValueError("source Agent result conflicts with its Turn receipt")
    elif (source_result_status is None) != (source_result_summary is None):
        raise ValueError("source Turn result status and summary must be recorded together")

    current_head_sha = work_item.last_published_sha or work_item.base_sha
    generation_head_sha = from_generation.last_published_sha or from_generation.start_head_sha
    if current_head_sha != generation_head_sha:
        raise ValueError("WorkItem and source generation publication anchors differ")

    for previous, current in zip(
        published_checkpoints, published_checkpoints[1:]
    ):
        if current.previous_sha != previous.head_sha:
            raise ValueError("publication checkpoint evidence is not one ordered chain")
    if published_checkpoints:
        if published_checkpoints[-1].head_sha != current_head_sha:
            raise ValueError("publication checkpoint evidence does not reach current HEAD")
    elif work_item.last_published_sha is not None:
        # A schema-7 publication can predate the additive evidence ledger. Its durable
        # current anchor remains trusted, but the missing history is explicitly incomplete.
        pass

    published_checkpoint_heads = [item.head_sha for item in published_checkpoints]
    if (
        work_item.last_published_sha is not None
        and current_head_sha not in published_checkpoint_heads
    ):
        published_checkpoint_heads.append(current_head_sha)
    path_set = {
        path
        for checkpoint in published_checkpoints
        if checkpoint.has_complete_evidence
        for path in checkpoint.changed_paths
    }
    publication_evidence_complete = (
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
    required_check_facts = [
        {"name": name, "status": "not_observed"} for name in required_checks
    ]
    remaining_work: list[dict[str, str]] = [
        {
            "kind": "acceptance_verification",
            "status": "remaining",
            "summary": "Verify the acceptance criteria against the current repository state.",
        }
    ]
    remaining_work.extend(
        {
            "kind": "required_check_observation",
            "status": "remaining",
            "summary": f"Observe required check '{name}' for the current HEAD.",
        }
        for name in required_checks
    )
    trusted: dict[str, Any] = {
        "acceptance": {
            "criteria_sha256": sha256(acceptance_criteria.encode("utf-8")).hexdigest(),
            "evidence": [],
            "status": "unverified",
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
            "verified_changed_paths": sorted(path_set),
        },
        "issue": {
            "approved_context_sha256": approved_context_sha256,
            "content_sha256": issue_content_sha256,
            "revision": issue_revision,
            "revision_sha256": sha256(issue_revision.encode("utf-8")).hexdigest(),
            "task_spec_sha256": task_spec_sha256,
        },
        "provenance": "dispatcher_git_state",
        "remaining_work": remaining_work,
        "required_checks": required_check_facts,
        "schema_version": 1,
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
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_HANDOFF_JSON_BYTES:
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
    if not isinstance(checks, list) or not checks:
        raise ValueError("required_checks must be non-empty")
    for check in checks:
        item = _exact_object(check, {"name", "status"}, "required check")
        if not isinstance(item["name"], str) or not item["name"] or item["status"] != "not_observed":
            raise ValueError("required check evidence is invalid")
    remaining = value["remaining_work"]
    if not isinstance(remaining, list) or not remaining:
        raise ValueError("remaining_work must be non-empty")
    for item in remaining:
        parsed = _exact_object(item, {"kind", "status", "summary"}, "remaining work")
        if parsed["status"] != "remaining" or not isinstance(parsed["summary"], str):
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
    if not isinstance(value, list) or len(value) > 1_000 or any(not isinstance(item, str) for item in value):
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
