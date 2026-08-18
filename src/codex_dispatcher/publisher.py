"""Pure, fixed-parameter planning for task-branch publication."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from codex_dispatcher.task_spec import is_path_allowed, normalize_repo_path
from codex_dispatcher.work_items import (
    WorkItem,
    WorkItemState,
    validate_git_sha,
    validate_sha256,
    validate_work_item_id,
)


MAX_PUBLISH_REQUEST_BYTES = 16 * 1024
MAX_BUNDLE_BYTES = 100 * 1024 * 1024
MAX_COMMITS_PER_PUBLISH = 100
MAX_CHANGED_PATHS = 1_000


class PublicationError(ValueError):
    """Raised before any Git or GitHub write when publication cannot be proven safe."""


@dataclass(frozen=True, slots=True)
class PublishRequest:
    work_item_id: str
    expected_head_sha: str

    def __post_init__(self) -> None:
        validate_work_item_id(self.work_item_id)
        validate_git_sha(self.expected_head_sha, "expected_head_sha")

    def to_json(self) -> str:
        return json.dumps(
            {
                "expected_head_sha": self.expected_head_sha,
                "work_item_id": self.work_item_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def parse_publish_request(value: str | bytes) -> PublishRequest:
    payload = _load_request(value)
    if set(payload) != {"work_item_id", "expected_head_sha"}:
        raise PublicationError("publish request has unexpected or missing fields")
    try:
        return PublishRequest(payload["work_item_id"], payload["expected_head_sha"])
    except (TypeError, ValueError) as exc:
        raise PublicationError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class VerifiedBundle:
    """Evidence produced by the future quarantine verifier, not by the Runner manifest."""

    bundle_sha256: str
    head_sha: str
    parent_anchor_sha: str
    changed_paths: tuple[str, ...]
    commit_count: int
    size_bytes: int

    def __post_init__(self) -> None:
        validate_sha256(self.bundle_sha256, "bundle_sha256")
        validate_git_sha(self.head_sha, "head_sha")
        validate_git_sha(self.parent_anchor_sha, "parent_anchor_sha")
        if (
            type(self.commit_count) is not int
            or not 1 <= self.commit_count <= MAX_COMMITS_PER_PUBLISH
        ):
            raise PublicationError("bundle commit count exceeds the publication limit")
        if type(self.size_bytes) is not int or not 1 <= self.size_bytes <= MAX_BUNDLE_BYTES:
            raise PublicationError("bundle size exceeds the publication limit")
        if self.head_sha == self.parent_anchor_sha:
            raise PublicationError("bundle HEAD must advance its publication anchor")
        if not isinstance(self.changed_paths, tuple) or not self.changed_paths:
            raise PublicationError("bundle must contain at least one changed path")
        if len(self.changed_paths) > MAX_CHANGED_PATHS:
            raise PublicationError("bundle changed path count exceeds the publication limit")
        try:
            normalized = tuple(normalize_repo_path(path) for path in self.changed_paths)
        except (TypeError, ValueError) as exc:
            raise PublicationError("bundle contains an unsafe changed path") from exc
        if normalized != self.changed_paths or len(set(normalized)) != len(normalized):
            raise PublicationError("bundle changed paths must be normalized and unique")


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    work_item_id: str
    repository: str
    source_sha: str
    target_ref: str
    expected_remote_sha: str | None
    bundle_sha256: str
    changed_paths: tuple[str, ...]
    force: bool = False
    delete: bool = False


def plan_publication(
    *,
    request: PublishRequest,
    work_item: WorkItem,
    bundle: VerifiedBundle,
    issue_allowed_paths: tuple[str, ...],
    repository_allowed_paths: tuple[str, ...],
    repository_denied_paths: tuple[str, ...] = (),
) -> PublicationPlan:
    """Return an exact ref update plan without executing Git or accepting ref parameters."""
    if request.work_item_id != work_item.work_item_id:
        raise PublicationError("publish request is bound to a different work item")
    if work_item.state is WorkItemState.COMPLETED:
        raise PublicationError("completed work items cannot publish")
    if request.expected_head_sha != bundle.head_sha:
        raise PublicationError("publish request HEAD does not match the verified bundle")
    anchor = work_item.last_published_sha or work_item.base_sha
    if bundle.parent_anchor_sha != anchor:
        raise PublicationError("bundle is not based on the recorded publication anchor")
    policy_sets = (issue_allowed_paths, repository_allowed_paths, repository_denied_paths)
    if any(
        not isinstance(paths, tuple) or any(not isinstance(path, str) for path in paths)
        for paths in policy_sets
    ):
        raise TypeError("publication path policies must be tuples of strings")
    for path in bundle.changed_paths:
        if not is_path_allowed(path, issue_allowed_paths, repository_denied_paths):
            raise PublicationError(f"changed path is outside Issue policy: {path}")
        if not is_path_allowed(path, repository_allowed_paths, repository_denied_paths):
            raise PublicationError(f"changed path is outside repository policy: {path}")
    target_ref = f"refs/heads/{work_item.task_branch}"
    if target_ref in {
        f"refs/heads/{work_item.base_branch}",
        "refs/heads/main",
        "refs/heads/master",
    }:
        raise PublicationError("Publisher cannot target a protected branch")
    return PublicationPlan(
        work_item_id=work_item.work_item_id,
        repository=work_item.repository,
        source_sha=bundle.head_sha,
        target_ref=target_ref,
        # The first publication must create a previously absent remote task branch.
        # Later publications must advance exactly the last SHA recorded after read-back.
        expected_remote_sha=work_item.last_published_sha,
        bundle_sha256=bundle.bundle_sha256,
        changed_paths=bundle.changed_paths,
    )


def _load_request(value: str | bytes) -> dict[str, Any]:
    if isinstance(value, str):
        raw = value.encode("utf-8")
    elif isinstance(value, bytes):
        raw = value
    else:
        raise PublicationError("publish request must be UTF-8 JSON")
    if not raw or len(raw) > MAX_PUBLISH_REQUEST_BYTES or b"\x00" in raw:
        raise PublicationError("publish request exceeds its safe size or encoding boundary")
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublicationError("publish request must be valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise PublicationError("publish request must be a JSON object")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PublicationError(f"duplicate publish request field: {key}")
        result[key] = value
    return result
