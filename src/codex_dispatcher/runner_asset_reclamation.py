"""Exact-reference Runner image and release reclamation plans and receipts."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterable

from codex_dispatcher.work_items import validate_work_item_id


_SCHEMA_VERSION = 1
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_IMAGE_REF_RE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")
_RUNNER_IMAGE_SOURCE = "https://github.com/longwdl/codex_cloud_task_scheduler"
_RUNNER_IMAGE_TITLE = "codex-cloud-task-scheduler-runner"
_WORK_ITEM_INVENTORY_MAXIMUM = 1024 * 1024


class RunnerAssetReclamationError(RuntimeError):
    """Raised before a target can be proven unreferenced and exact."""


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    commit: str
    path: str
    tree_sha256: str
    allocated_bytes: int

    def __post_init__(self) -> None:
        _validate_commit(self.commit)
        _validate_sha256(self.tree_sha256, "release tree_sha256")
        path = Path(self.path)
        if not path.is_absolute() or path.name != self.commit or ".." in path.parts:
            raise ValueError("release path is invalid")
        if type(self.allocated_bytes) is not int or self.allocated_bytes < 0:
            raise ValueError("release allocated_bytes is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "commit": self.commit,
            "path": self.path,
            "tree_sha256": self.tree_sha256,
            "allocated_bytes": self.allocated_bytes,
        }


@dataclass(frozen=True, slots=True)
class DockerImageAsset:
    image_id: str
    repo_digests: tuple[str, ...]
    repo_tags: tuple[str, ...]
    source_commit: str
    provenance_kind: str
    size_bytes: int
    unique_size_bytes: int
    container_count: int
    source_label: str | None
    title_label: str | None

    def __post_init__(self) -> None:
        if _IMAGE_ID_RE.fullmatch(self.image_id) is None:
            raise ValueError("Docker image ID is invalid")
        if any(_IMAGE_REF_RE.fullmatch(value) is None for value in self.repo_digests):
            raise ValueError("Docker RepoDigest is invalid")
        _validate_commit(self.source_commit)
        if self.provenance_kind not in {"oci_revision", "publication_run", "tree_equivalent"}:
            raise ValueError("Docker provenance kind is invalid")
        for value, field in (
            (self.size_bytes, "size_bytes"),
            (self.unique_size_bytes, "unique_size_bytes"),
            (self.container_count, "container_count"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"Docker {field} is invalid")
    @property
    def is_runner_image(self) -> bool:
        return (
            self.source_label == _RUNNER_IMAGE_SOURCE
            and self.title_label == _RUNNER_IMAGE_TITLE
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "image_id": self.image_id,
            "repo_digests": list(self.repo_digests),
            "repo_tags": list(self.repo_tags),
            "source_commit": self.source_commit,
            "provenance_kind": self.provenance_kind,
            "size_bytes": self.size_bytes,
            "unique_size_bytes": self.unique_size_bytes,
            "container_count": self.container_count,
            "source_label": self.source_label,
            "title_label": self.title_label,
        }

    @classmethod
    def from_mapping(cls, payload: object) -> DockerImageAsset:
        if not isinstance(payload, dict):
            raise ValueError("Docker image asset must be an object")
        repo_digests = payload.get("repo_digests")
        repo_tags = payload.get("repo_tags")
        if not isinstance(repo_digests, list) or not all(
            isinstance(value, str) for value in repo_digests
        ):
            raise ValueError("Docker RepoDigest inventory is invalid")
        if not isinstance(repo_tags, list) or not all(
            isinstance(value, str) for value in repo_tags
        ):
            raise ValueError("Docker tag inventory is invalid")
        return cls(
            image_id=payload.get("image_id"),  # type: ignore[arg-type]
            repo_digests=tuple(repo_digests),
            repo_tags=tuple(repo_tags),
            source_commit=payload.get("source_commit"),  # type: ignore[arg-type]
            provenance_kind=payload.get("provenance_kind"),  # type: ignore[arg-type]
            size_bytes=payload.get("size_bytes"),  # type: ignore[arg-type]
            unique_size_bytes=payload.get("unique_size_bytes"),  # type: ignore[arg-type]
            container_count=payload.get("container_count"),  # type: ignore[arg-type]
            source_label=payload.get("source_label"),  # type: ignore[arg-type]
            title_label=payload.get("title_label"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class RunnerAssetSnapshot:
    current_release_commit: str
    rollback_release_commits: tuple[str, ...]
    configured_image: str
    rollback_images: tuple[str, ...]
    work_item_images: tuple[str, ...]
    registry_work_item_ids: tuple[str, ...]
    archived_work_item_ids: tuple[str, ...]
    absent_work_item_ids: tuple[str, ...]
    releases: tuple[ReleaseAsset, ...]
    images: tuple[DockerImageAsset, ...]
    blocked_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_commit(self.current_release_commit)
        for commit in self.rollback_release_commits:
            _validate_commit(commit)
        if _IMAGE_REF_RE.fullmatch(self.configured_image) is None:
            raise ValueError("configured image must be an exact RepoDigest")
        for values, field in (
            (self.rollback_images, "rollback image"),
            (self.work_item_images, "WorkItem image"),
        ):
            if any(_IMAGE_REF_RE.fullmatch(value) is None for value in values):
                raise ValueError(f"{field} reference is invalid")
        for values in (
            self.registry_work_item_ids,
            self.archived_work_item_ids,
            self.absent_work_item_ids,
        ):
            if len(values) != len(set(values)):
                raise ValueError("WorkItem inventory contains duplicate identities")
            for work_item_id in values:
                validate_work_item_id(work_item_id)
        if set(self.archived_work_item_ids) & set(self.absent_work_item_ids):
            raise ValueError("archive and absence identities overlap")
        if set(self.archived_work_item_ids) - set(self.registry_work_item_ids):
            raise ValueError("archived WorkItem is missing its permanent registry")
        if set(self.absent_work_item_ids) & set(self.registry_work_item_ids):
            raise ValueError("absence-reconciled WorkItem still has a registry")
        if len({item.commit for item in self.releases}) != len(self.releases):
            raise ValueError("release inventory contains duplicate commits")
        if len({item.image_id for item in self.images}) != len(self.images):
            raise ValueError("image inventory contains duplicate IDs")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "kind": "runner_asset_snapshot",
            "current_release_commit": self.current_release_commit,
            "rollback_release_commits": list(self.rollback_release_commits),
            "configured_image": self.configured_image,
            "rollback_images": list(self.rollback_images),
            "work_item_images": list(self.work_item_images),
            "registry_work_item_ids": list(self.registry_work_item_ids),
            "archived_work_item_ids": list(self.archived_work_item_ids),
            "absent_work_item_ids": list(self.absent_work_item_ids),
            "releases": [item.to_mapping() for item in self.releases],
            "images": [item.to_mapping() for item in self.images],
            "blocked_reasons": list(self.blocked_reasons),
        }


@dataclass(frozen=True, slots=True)
class RunnerAssetReclamationPlan:
    snapshot_sha256: str
    protected_release_commits: tuple[str, ...]
    protected_images: tuple[str, ...]
    release_targets: tuple[ReleaseAsset, ...]
    image_targets: tuple[DockerImageAsset, ...]
    expected_release_bytes: int
    expected_image_unique_bytes: int
    expected_total_bytes: int
    plan_sha256: str

    def __post_init__(self) -> None:
        _validate_sha256(self.snapshot_sha256, "snapshot_sha256")
        _validate_sha256(self.plan_sha256, "plan_sha256")
        for commit in self.protected_release_commits:
            _validate_commit(commit)
        if any(_IMAGE_REF_RE.fullmatch(value) is None for value in self.protected_images):
            raise ValueError("protected image reference is invalid")
        for value, field in (
            (self.expected_release_bytes, "expected_release_bytes"),
            (self.expected_image_unique_bytes, "expected_image_unique_bytes"),
            (self.expected_total_bytes, "expected_total_bytes"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{field} is invalid")
        if self.expected_release_bytes != sum(
            item.allocated_bytes for item in self.release_targets
        ):
            raise ValueError("release byte total conflicts with targets")
        if self.expected_image_unique_bytes != sum(
            item.unique_size_bytes for item in self.image_targets
        ):
            raise ValueError("image byte total conflicts with targets")
        if self.expected_total_bytes != (
            self.expected_release_bytes + self.expected_image_unique_bytes
        ):
            raise ValueError("total byte estimate conflicts with targets")

    def to_mapping(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "kind": "runner_asset_reclamation_plan",
            "authorizes_apply": False,
            "snapshot_sha256": self.snapshot_sha256,
            "protected_release_commits": list(self.protected_release_commits),
            "protected_images": list(self.protected_images),
            "release_targets": [item.to_mapping() for item in self.release_targets],
            "image_targets": [item.to_mapping() for item in self.image_targets],
            "expected_release_bytes": self.expected_release_bytes,
            "expected_image_unique_bytes": self.expected_image_unique_bytes,
            "expected_total_bytes": self.expected_total_bytes,
            "deletion_commands": [
                f"delete exact release tree {item.path} at tree_sha256={item.tree_sha256}"
                for item in self.release_targets
            ]
            + [
                f"docker image rm {item.image_id}"
                for item in self.image_targets
            ],
        }
        if include_digest:
            payload["plan_sha256"] = self.plan_sha256
        return payload

    @classmethod
    def from_mapping(cls, payload: object) -> RunnerAssetReclamationPlan:
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != _SCHEMA_VERSION
            or payload.get("kind") != "runner_asset_reclamation_plan"
        ):
            raise ValueError("Runner asset plan is invalid")
        protected_release_commits = payload.get("protected_release_commits")
        protected_images = payload.get("protected_images")
        release_targets = payload.get("release_targets")
        image_targets = payload.get("image_targets")
        if not all(
            isinstance(value, list)
            for value in (
                protected_release_commits,
                protected_images,
                release_targets,
                image_targets,
            )
        ):
            raise ValueError("Runner asset plan collections are invalid")
        assert isinstance(protected_release_commits, list)
        assert isinstance(protected_images, list)
        assert isinstance(release_targets, list)
        assert isinstance(image_targets, list)
        if not all(isinstance(value, str) for value in protected_release_commits):
            raise ValueError("protected release inventory is invalid")
        if not all(isinstance(value, str) for value in protected_images):
            raise ValueError("protected image inventory is invalid")
        if not all(isinstance(item, dict) for item in release_targets):
            raise ValueError("release target inventory is invalid")
        plan = cls(
            snapshot_sha256=payload.get("snapshot_sha256"),  # type: ignore[arg-type]
            protected_release_commits=tuple(protected_release_commits),
            protected_images=tuple(protected_images),
            release_targets=tuple(
                ReleaseAsset(
                    item.get("commit"),  # type: ignore[arg-type]
                    item.get("path"),  # type: ignore[arg-type]
                    item.get("tree_sha256"),  # type: ignore[arg-type]
                    item.get("allocated_bytes"),  # type: ignore[arg-type]
                )
                for item in release_targets
            ),
            image_targets=tuple(
                DockerImageAsset.from_mapping(item)
                for item in image_targets
            ),
            expected_release_bytes=payload.get("expected_release_bytes"),  # type: ignore[arg-type]
            expected_image_unique_bytes=payload.get("expected_image_unique_bytes"),  # type: ignore[arg-type]
            expected_total_bytes=payload.get("expected_total_bytes"),  # type: ignore[arg-type]
            plan_sha256=payload.get("plan_sha256"),  # type: ignore[arg-type]
        )
        if _canonical_sha256(plan.to_mapping(include_digest=False)) != plan.plan_sha256:
            raise ValueError("Runner asset plan digest is invalid")
        return plan


def plan_runner_asset_reclamation(
    snapshot: RunnerAssetSnapshot,
) -> RunnerAssetReclamationPlan:
    """Select only exact assets absent from every current recovery reference."""
    if not isinstance(snapshot, RunnerAssetSnapshot):
        raise TypeError("snapshot must be a RunnerAssetSnapshot")
    if snapshot.blocked_reasons:
        raise RunnerAssetReclamationError("Runner asset inventory is incomplete")
    snapshot_sha256 = _canonical_sha256(snapshot.to_mapping())
    protected_releases = tuple(
        sorted(
            {
                snapshot.current_release_commit,
                *snapshot.rollback_release_commits,
            }
        )
    )
    available_releases = {item.commit for item in snapshot.releases}
    if set(protected_releases) - available_releases:
        raise RunnerAssetReclamationError("protected release is absent from inventory")
    release_targets = tuple(
        item
        for item in sorted(snapshot.releases, key=lambda value: value.commit)
        if item.commit not in protected_releases
    )

    protected_images = tuple(
        sorted(
            {
                snapshot.configured_image,
                *snapshot.rollback_images,
                *snapshot.work_item_images,
            }
        )
    )
    observed_refs = {
        reference
        for image in snapshot.images
        for reference in image.repo_digests
    }
    if snapshot.configured_image not in observed_refs:
        raise RunnerAssetReclamationError("configured image is absent from Docker inventory")
    image_targets = tuple(
        image
        for image in sorted(snapshot.images, key=lambda value: value.image_id)
        if image.is_runner_image
        and image.container_count == 0
        and not set(image.repo_digests).intersection(protected_images)
    )
    if any(not image.repo_digests for image in image_targets):
        raise RunnerAssetReclamationError("candidate Runner image has no exact RepoDigest")
    release_bytes = sum(item.allocated_bytes for item in release_targets)
    image_bytes = sum(item.unique_size_bytes for item in image_targets)
    provisional = RunnerAssetReclamationPlan(
        snapshot_sha256=snapshot_sha256,
        protected_release_commits=protected_releases,
        protected_images=protected_images,
        release_targets=release_targets,
        image_targets=image_targets,
        expected_release_bytes=release_bytes,
        expected_image_unique_bytes=image_bytes,
        expected_total_bytes=release_bytes + image_bytes,
        plan_sha256="0" * 64,
    )
    plan_sha256 = _canonical_sha256(provisional.to_mapping(include_digest=False))
    return RunnerAssetReclamationPlan(
        snapshot_sha256=provisional.snapshot_sha256,
        protected_release_commits=provisional.protected_release_commits,
        protected_images=provisional.protected_images,
        release_targets=provisional.release_targets,
        image_targets=provisional.image_targets,
        expected_release_bytes=provisional.expected_release_bytes,
        expected_image_unique_bytes=provisional.expected_image_unique_bytes,
        expected_total_bytes=provisional.expected_total_bytes,
        plan_sha256=plan_sha256,
    )


def apply_runner_asset_reclamation(
    *,
    approved_plan: RunnerAssetReclamationPlan,
    reinspected_snapshot: RunnerAssetSnapshot,
    receipt_directory: Path,
    delete_release: Callable[[ReleaseAsset], int],
    delete_image: Callable[[DockerImageAsset], int],
    now: datetime | None = None,
) -> dict[str, object]:
    """Write intent, delete exact identities, and permanently publish the outcome."""
    if not isinstance(approved_plan, RunnerAssetReclamationPlan):
        raise TypeError("approved_plan must be a RunnerAssetReclamationPlan")
    _protected_directory(receipt_directory, "reclamation receipt directory")
    observed_plan = plan_runner_asset_reclamation(reinspected_snapshot)
    if observed_plan != approved_plan:
        raise RunnerAssetReclamationError("Runner assets changed after plan approval")
    receipt_path = receipt_directory / f"{approved_plan.plan_sha256}.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        payload = _read_receipt(receipt_path)
        if (
            payload.get("plan_sha256") == approved_plan.plan_sha256
            and payload.get("status") == "reclaimed"
        ):
            return payload
        raise RunnerAssetReclamationError("reclamation receipt already exists")
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("reclamation timestamp must be timezone-aware")
    completed_releases: list[str] = []
    completed_images: list[str] = []
    reclaimed_bytes = 0
    receipt: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "kind": "runner_asset_reclamation_receipt",
        "status": "prepared",
        "plan_sha256": approved_plan.plan_sha256,
        "snapshot_sha256": approved_plan.snapshot_sha256,
        "expected_total_bytes": approved_plan.expected_total_bytes,
        "release_targets": [item.to_mapping() for item in approved_plan.release_targets],
        "image_targets": [item.to_mapping() for item in approved_plan.image_targets],
        "completed_release_commits": completed_releases,
        "completed_image_ids": completed_images,
        "reclaimed_bytes": reclaimed_bytes,
        "started_at": moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _write_json_atomic(receipt_path, receipt)
    try:
        for target in approved_plan.release_targets:
            reclaimed_bytes += delete_release(target)
            completed_releases.append(target.commit)
            receipt.update(
                status="in_progress",
                completed_release_commits=list(completed_releases),
                reclaimed_bytes=reclaimed_bytes,
            )
            _write_json_atomic(receipt_path, receipt)
        for target in approved_plan.image_targets:
            reclaimed_bytes += delete_image(target)
            completed_images.append(target.image_id)
            receipt.update(
                status="in_progress",
                completed_image_ids=list(completed_images),
                reclaimed_bytes=reclaimed_bytes,
            )
            _write_json_atomic(receipt_path, receipt)
    except BaseException:
        receipt.update(
            status="failed",
            completed_release_commits=list(completed_releases),
            completed_image_ids=list(completed_images),
            reclaimed_bytes=reclaimed_bytes,
            rollback_boundary=(
                "deleted assets are not recreated automatically; retain protected releases, "
                "configured images, Git history, and this receipt"
            ),
        )
        _write_json_atomic(receipt_path, receipt)
        raise
    receipt.update(
        status="reclaimed",
        completed_release_commits=list(completed_releases),
        completed_image_ids=list(completed_images),
        reclaimed_bytes=reclaimed_bytes,
        completed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    _write_json_atomic(receipt_path, receipt)
    return receipt


def collect_runner_asset_snapshot(
    *,
    config_path: Path,
    releases_root: Path,
    current_link: Path,
    rollback_release_commits: tuple[str, ...],
    rollback_image_refs: tuple[str, ...],
    images: tuple[DockerImageAsset, ...],
    trusted_work_items_owner_uid: int | None = None,
    trusted_work_items_owner_gid: int | None = None,
) -> RunnerAssetSnapshot:
    """Collect every local reference that can retain a Runner release or image."""
    for commit in rollback_release_commits:
        _validate_commit(commit)
    if any(_IMAGE_REF_RE.fullmatch(value) is None for value in rollback_image_refs):
        raise RunnerAssetReclamationError("rollback image reference is not digest-pinned")
    config = _read_protected_json(config_path, "Runner config", maximum=32 * 1024)
    if config.get("execution_mode") != "rootless_docker":
        raise RunnerAssetReclamationError("Runner asset reclamation requires rootless Docker")
    docker_runtime = config.get("docker_runtime")
    if not isinstance(docker_runtime, dict):
        raise RunnerAssetReclamationError("Runner Docker config is invalid")
    configured_image = docker_runtime.get("image")
    if not isinstance(configured_image, str) or _IMAGE_REF_RE.fullmatch(configured_image) is None:
        raise RunnerAssetReclamationError("Runner configured image is not digest-pinned")
    work_items_root_value = config.get("work_items_root")
    if not isinstance(work_items_root_value, str):
        raise RunnerAssetReclamationError("Runner work-items root is invalid")
    work_items_root = Path(work_items_root_value)
    work_items_owner_uid = (
        os.geteuid()
        if trusted_work_items_owner_uid is None
        else trusted_work_items_owner_uid
    )
    if type(work_items_owner_uid) is not int or work_items_owner_uid < 0:
        raise RunnerAssetReclamationError("Runner work-items owner is invalid")
    _protected_directory(
        work_items_root,
        "Runner work-items root",
        trusted_owner_uid=work_items_owner_uid,
    )
    work_items_owner_gid = (
        work_items_root.stat(follow_symlinks=False).st_gid
        if trusted_work_items_owner_gid is None
        else trusted_work_items_owner_gid
    )
    if (
        type(work_items_owner_gid) is not int
        or work_items_owner_gid < 0
        or work_items_root.stat(follow_symlinks=False).st_gid != work_items_owner_gid
    ):
        raise RunnerAssetReclamationError("Runner work-items group is invalid")

    try:
        current_value = os.readlink(current_link)
    except OSError as exc:
        raise RunnerAssetReclamationError("Runner current release link is unavailable") from exc
    current_commit = _validate_commit(current_value.rstrip("/").split("/")[-1])
    if (releases_root / current_commit).resolve() != current_link.resolve():
        raise RunnerAssetReclamationError("Runner current link escapes the release root")

    registry = _identity_directory(
        work_items_root / ".registry", trusted_owner_uid=work_items_owner_uid
    )
    archives = _identity_directory(
        work_items_root / ".archives", trusted_owner_uid=work_items_owner_uid
    )
    absences = _identity_directory(
        work_items_root / ".absences", trusted_owner_uid=work_items_owner_uid
    )
    if set(archives) - set(registry):
        raise RunnerAssetReclamationError("Runner archive is missing its permanent registry")
    if set(absences) & set(registry):
        raise RunnerAssetReclamationError("Runner absence still has a registry")

    work_item_images, blocked_work_item_ids = _collect_active_work_item_images(
        registry=registry,
        archived_work_item_ids=frozenset(archives),
        work_items_root=work_items_root,
        work_items_owner_uid=work_items_owner_uid,
        work_items_owner_gid=work_items_owner_gid,
    )
    blockers = [
        f"active registry {work_item_id} is not inspectable"
        for work_item_id in blocked_work_item_ids
    ]

    return RunnerAssetSnapshot(
        current_release_commit=current_commit,
        rollback_release_commits=tuple(sorted(set(rollback_release_commits))),
        configured_image=configured_image,
        rollback_images=tuple(sorted(set(rollback_image_refs))),
        work_item_images=tuple(sorted(work_item_images)),
        registry_work_item_ids=tuple(sorted(registry)),
        archived_work_item_ids=tuple(sorted(archives)),
        absent_work_item_ids=tuple(sorted(absences)),
        releases=inspect_release_assets(releases_root),
        images=tuple(sorted(images, key=lambda value: value.image_id)),
        blocked_reasons=tuple(blockers),
    )


def _collect_active_work_item_images(
    *,
    registry: dict[str, dict[str, object]],
    archived_work_item_ids: frozenset[str],
    work_items_root: Path,
    work_items_owner_uid: int,
    work_items_owner_gid: int,
) -> tuple[set[str], tuple[str, ...]]:
    """Inspect FUSE state in an irreversibly unprivileged child when root calls."""
    active_ids = tuple(sorted(set(registry) - archived_work_item_ids))
    if not active_ids:
        return set(), ()
    user_credentials = _process_uids()
    group_credentials = _process_gids()
    owner_user_credentials = (work_items_owner_uid,) * 3
    owner_group_credentials = (work_items_owner_gid,) * 3
    if (
        user_credentials == owner_user_credentials
        and group_credentials == owner_group_credentials
    ):
        return _inspect_active_work_item_images(
            registry=registry,
            active_work_item_ids=active_ids,
            work_items_root=work_items_root,
            work_items_owner_uid=work_items_owner_uid,
        )
    if user_credentials != (0, 0, 0):
        raise RunnerAssetReclamationError(
            "Runner WorkItem inspection requires root or the trusted owner"
        )
    set_resuid = getattr(os, "setresuid", None)
    set_resgid = getattr(os, "setresgid", None)
    if not callable(set_resuid) or not callable(set_resgid):
        raise RunnerAssetReclamationError(
            "Runner WorkItem child requires Linux credential isolation"
        )

    read_descriptor, write_descriptor = os.pipe()
    try:
        child_pid = os.fork()
    except BaseException:
        os.close(read_descriptor)
        os.close(write_descriptor)
        raise
    if child_pid == 0:
        os.close(read_descriptor)
        exit_code = 1
        failure_stage = "credential_group_drop"
        try:
            if _process_gids() != owner_group_credentials:
                set_resgid(*owner_group_credentials)
                if _process_gids() != owner_group_credentials:
                    raise RunnerAssetReclamationError(
                        "Runner WorkItem child did not drop root group credentials"
                    )
            failure_stage = "credential_user_drop"
            set_resuid(*owner_user_credentials)
            if _process_uids() != owner_user_credentials:
                raise RunnerAssetReclamationError(
                    "Runner WorkItem child did not drop root credentials"
                )
            failure_stage = "inventory_inspection"
            images, blocked_ids = _inspect_active_work_item_images(
                registry=registry,
                active_work_item_ids=active_ids,
                work_items_root=work_items_root,
                work_items_owner_uid=work_items_owner_uid,
            )
            failure_stage = "inventory_encoding"
            raw = json.dumps(
                {
                    "schema_version": 1,
                    "images": sorted(images),
                    "blocked_work_item_ids": list(blocked_ids),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            if len(raw) > _WORK_ITEM_INVENTORY_MAXIMUM:
                raise RunnerAssetReclamationError(
                    "Runner WorkItem inventory exceeds its size boundary"
                )
            failure_stage = "inventory_write"
            _write_descriptor_all(write_descriptor, raw)
            exit_code = 0
        except BaseException:
            try:
                _write_descriptor_all(
                    write_descriptor,
                    json.dumps(
                        {
                            "schema_version": 1,
                            "error_code": f"{failure_stage}_failed",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode(),
                )
            except BaseException:
                pass
        finally:
            os.close(write_descriptor)
            os._exit(exit_code)

    os.close(write_descriptor)
    raw = bytearray()
    try:
        while chunk := os.read(read_descriptor, 65536):
            raw.extend(chunk)
            if len(raw) > _WORK_ITEM_INVENTORY_MAXIMUM:
                raise RunnerAssetReclamationError(
                    "Runner WorkItem inventory exceeds its size boundary"
                )
    finally:
        os.close(read_descriptor)
        waited_pid, wait_status = os.waitpid(child_pid, 0)
    child_failed = (
        waited_pid != child_pid
        or not os.WIFEXITED(wait_status)
        or os.WEXITSTATUS(wait_status) != 0
    )
    try:
        payload = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RunnerAssetReclamationError(
            "Runner WorkItem inventory child returned invalid JSON"
        ) from exc
    if child_failed:
        error_code = payload.get("error_code") if isinstance(payload, dict) else None
        allowed_error_codes = {
            "credential_group_drop_failed",
            "credential_user_drop_failed",
            "inventory_inspection_failed",
            "inventory_encoding_failed",
            "inventory_write_failed",
        }
        if error_code not in allowed_error_codes:
            error_code = "unknown_failure"
        raise RunnerAssetReclamationError(
            f"Runner WorkItem inventory child failed: {error_code}"
        )
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RunnerAssetReclamationError("Runner WorkItem inventory child is invalid")
    images = payload.get("images")
    blocked_ids = payload.get("blocked_work_item_ids")
    if (
        not isinstance(images, list)
        or not all(isinstance(value, str) for value in images)
        or images != sorted(set(images))
        or any(_IMAGE_REF_RE.fullmatch(value) is None for value in images)
        or not isinstance(blocked_ids, list)
        or not all(isinstance(value, str) for value in blocked_ids)
        or blocked_ids != sorted(set(blocked_ids))
        or not set(blocked_ids).issubset(active_ids)
    ):
        raise RunnerAssetReclamationError("Runner WorkItem inventory child is invalid")
    return set(images), tuple(blocked_ids)


def _inspect_active_work_item_images(
    *,
    registry: dict[str, dict[str, object]],
    active_work_item_ids: tuple[str, ...],
    work_items_root: Path,
    work_items_owner_uid: int,
) -> tuple[set[str], tuple[str, ...]]:
    work_item_images: set[str] = set()
    blocked_ids: list[str] = []
    for work_item_id in active_work_item_ids:
        payload = registry[work_item_id]
        repository = payload.get("repository")
        issue_number = payload.get("issue_number")
        if (
            not isinstance(repository, str)
            or "/" not in repository
            or type(issue_number) is not int
            or issue_number <= 0
        ):
            raise RunnerAssetReclamationError("Runner registry identity is malformed")
        state = (
            work_items_root
            / repository.replace("/", "__")
            / f"issue-{issue_number}"
            / "runner-state"
        )
        try:
            _protected_directory(
                state,
                "WorkItem state directory",
                trusted_owner_uid=work_items_owner_uid,
            )
        except RunnerAssetReclamationError:
            blocked_ids.append(work_item_id)
            continue
        for binding in sorted(state.rglob("codex-session.json")):
            try:
                relative = binding.relative_to(state)
            except ValueError as exc:
                raise RunnerAssetReclamationError("session binding escapes WorkItem") from exc
            if len(relative.parts) > 4:
                raise RunnerAssetReclamationError("session binding depth is invalid")
            binding_payload = _read_protected_json(
                binding,
                "WorkItem session binding",
                maximum=4096,
                trusted_owner_uid=work_items_owner_uid,
            )
            image = binding_payload.get("image")
            if (
                binding_payload.get("work_item_id") != work_item_id
                or not isinstance(image, str)
                or _IMAGE_REF_RE.fullmatch(image) is None
            ):
                raise RunnerAssetReclamationError("WorkItem image binding is invalid")
            work_item_images.add(image)
    return work_item_images, tuple(blocked_ids)


def _write_descriptor_all(descriptor: int, raw: bytes) -> None:
    offset = 0
    while offset < len(raw):
        written = os.write(descriptor, raw[offset:])
        if written <= 0:
            raise RunnerAssetReclamationError("Runner WorkItem inventory pipe failed")
        offset += written


def _process_uids() -> tuple[int, int, int]:
    get_resuid = getattr(os, "getresuid", None)
    if callable(get_resuid):
        return get_resuid()
    real_uid = os.getuid()
    effective_uid = os.geteuid()
    return real_uid, effective_uid, effective_uid


def _process_gids() -> tuple[int, int, int]:
    get_resgid = getattr(os, "getresgid", None)
    if callable(get_resgid):
        return get_resgid()
    real_gid = os.getgid()
    effective_gid = os.getegid()
    return real_gid, effective_gid, effective_gid


def inspect_release_assets(releases_root: Path) -> tuple[ReleaseAsset, ...]:
    """Hash every exact root-owned release without following links."""
    _protected_directory(releases_root, "Runner release root")
    assets: list[ReleaseAsset] = []
    for path in sorted(releases_root.iterdir()):
        if not path.is_dir() or path.is_symlink() or _COMMIT_RE.fullmatch(path.name) is None:
            raise RunnerAssetReclamationError("Runner release root has an unknown entry")
        tree_sha256, allocated_bytes = release_tree_identity(path)
        assets.append(
            ReleaseAsset(path.name, str(path), tree_sha256, allocated_bytes)
        )
    return tuple(assets)


def release_tree_identity(root: Path) -> tuple[str, int]:
    """Return a canonical content/metadata identity and allocated byte estimate."""
    entries: list[dict[str, object]] = []
    allocated = 0
    root_device = root.stat(follow_symlinks=False).st_dev
    for path in sorted(root.rglob("*")):
        metadata = path.stat(follow_symlinks=False)
        if metadata.st_dev != root_device or path.is_symlink():
            raise RunnerAssetReclamationError("release tree crosses a filesystem or link")
        relative = path.relative_to(root).as_posix()
        allocated += metadata.st_blocks * 512
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({"path": relative, "type": "directory", "mode": stat.S_IMODE(metadata.st_mode)})
        elif stat.S_ISREG(metadata.st_mode):
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "size_bytes": metadata.st_size,
                    "sha256": _sha256_file(path),
                }
            )
        else:
            raise RunnerAssetReclamationError("release tree has an unsupported entry")
    return _canonical_sha256(entries), allocated


def delete_exact_release(target: ReleaseAsset) -> int:
    """Delete one revalidated release tree without a recursive shell command."""
    path = Path(target.path)
    identity, allocated = release_tree_identity(path)
    if identity != target.tree_sha256 or allocated != target.allocated_bytes:
        raise RunnerAssetReclamationError("release changed after exact planning")
    for child in sorted(path.rglob("*"), key=lambda value: len(value.parts), reverse=True):
        if child.is_dir():
            child.rmdir()
        else:
            child.unlink()
    path.rmdir()
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return allocated


def docker_image_assets_from_json(
    *,
    inspections: Iterable[dict[str, object]],
    disk_usage: dict[str, object],
    provenance: dict[str, tuple[str, str]],
) -> tuple[DockerImageAsset, ...]:
    """Parse exact Docker inspect and system-df JSON captured from one daemon."""
    usage_rows = disk_usage.get("Images")
    if not isinstance(usage_rows, list):
        raise RunnerAssetReclamationError("Docker disk-usage image list is invalid")
    usage: dict[str, tuple[int, int]] = {}
    for row in usage_rows:
        if not isinstance(row, dict) or not isinstance(row.get("ID"), str):
            raise RunnerAssetReclamationError("Docker disk-usage row is invalid")
        usage[str(row["ID"])] = (
            _parse_human_size(row.get("UniqueSize")),
            _parse_nonnegative_int(row.get("Containers"), "container count"),
        )
    assets: list[DockerImageAsset] = []
    for payload in inspections:
        image_id = payload.get("Id")
        if not isinstance(image_id, str) or _IMAGE_ID_RE.fullmatch(image_id) is None:
            raise RunnerAssetReclamationError("Docker inspection image ID is invalid")
        config = payload.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if labels is None:
            labels = {}
        if not isinstance(labels, dict):
            raise RunnerAssetReclamationError("Docker image labels are invalid")
        if (
            labels.get("org.opencontainers.image.source") != _RUNNER_IMAGE_SOURCE
            or labels.get("org.opencontainers.image.title") != _RUNNER_IMAGE_TITLE
        ):
            continue
        repo_digests = tuple(sorted(set(payload.get("RepoDigests") or ())))
        repo_tags = tuple(sorted(set(payload.get("RepoTags") or ())))
        if not repo_digests and not repo_tags:
            # Build intermediates can inherit final-image labels, but Docker gives
            # them no stable reference suitable for an exact deletion receipt.
            continue
        source_commit = labels.get("org.opencontainers.image.revision")
        provenance_kind = "oci_revision"
        if not isinstance(source_commit, str):
            matched = provenance.get(image_id)
            if matched is None:
                for digest in payload.get("RepoDigests") or []:
                    if isinstance(digest, str) and digest in provenance:
                        matched = provenance[digest]
                        break
            if matched is None:
                raise RunnerAssetReclamationError("Docker image has no exact commit provenance")
            source_commit, provenance_kind = matched
        unique_size, containers = _docker_usage_for_image(image_id, usage)
        size = payload.get("Size")
        if type(size) is not int or size < 0:
            raise RunnerAssetReclamationError("Docker image size is invalid")
        assets.append(
            DockerImageAsset(
                image_id=image_id,
                repo_digests=repo_digests,
                repo_tags=repo_tags,
                source_commit=source_commit,
                provenance_kind=provenance_kind,
                size_bytes=size,
                unique_size_bytes=unique_size,
                container_count=containers,
                source_label=labels.get("org.opencontainers.image.source"),
                title_label=labels.get("org.opencontainers.image.title"),
            )
        )
    return tuple(sorted(assets, key=lambda value: value.image_id))


def _parse_human_size(value: object) -> int:
    if not isinstance(value, str):
        raise RunnerAssetReclamationError("Docker size is invalid")
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kMGT]?B)", value)
    if match is None:
        raise RunnerAssetReclamationError("Docker size unit is invalid")
    multipliers = {"B": 1, "kB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}
    return int(float(match.group(1)) * multipliers[match.group(2)])


def _docker_usage_for_image(
    image_id: str, usage: dict[str, tuple[int, int]]
) -> tuple[int, int]:
    direct = usage.get(image_id)
    if direct is not None:
        return direct
    hexadecimal = image_id.removeprefix("sha256:")
    matches = tuple(
        value
        for candidate, value in usage.items()
        if len(candidate.removeprefix("sha256:")) >= 12
        and hexadecimal.startswith(candidate.removeprefix("sha256:"))
    )
    if len(matches) != 1:
        raise RunnerAssetReclamationError(
            "Docker image is absent or ambiguous in disk usage"
        )
    return matches[0]


def _parse_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, str) and value.isdecimal():
        result = int(value)
    elif type(value) is int:
        result = value
    else:
        raise RunnerAssetReclamationError(f"Docker {field} is invalid")
    if result < 0:
        raise RunnerAssetReclamationError(f"Docker {field} is invalid")
    return result


def _read_receipt(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerAssetReclamationError("reclamation receipt is malformed") from exc
    if not isinstance(payload, dict):
        raise RunnerAssetReclamationError("reclamation receipt is invalid")
    return payload


def _identity_directory(
    path: Path, *, trusted_owner_uid: int
) -> dict[str, dict[str, object]]:
    _protected_directory(
        path,
        "Runner identity directory",
        trusted_owner_uid=trusted_owner_uid,
    )
    result: dict[str, dict[str, object]] = {}
    for child in sorted(path.iterdir()):
        if child.suffix != ".json":
            raise RunnerAssetReclamationError("Runner identity directory has an unknown entry")
        payload = _read_protected_json(
            child,
            "Runner identity",
            maximum=16 * 1024,
            trusted_owner_uid=trusted_owner_uid,
        )
        work_item_id = validate_work_item_id(payload.get("work_item_id"))
        if child.stem != work_item_id or work_item_id in result:
            raise RunnerAssetReclamationError("Runner identity filename conflicts")
        result[work_item_id] = payload
    return result


def _read_protected_json(
    path: Path,
    field: str,
    *,
    maximum: int,
    trusted_owner_uid: int | None = None,
) -> dict[str, object]:
    try:
        metadata = path.stat(follow_symlinks=False)
        raw = path.read_bytes()
    except OSError as exc:
        raise RunnerAssetReclamationError(f"{field} is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid
        not in (
            {0, os.geteuid()}
            if trusted_owner_uid is None
            else {trusted_owner_uid}
        )
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or not 0 < len(raw) <= maximum
        or b"\x00" in raw
    ):
        raise RunnerAssetReclamationError(f"{field} exceeds its protected boundary")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RunnerAssetReclamationError(f"{field} is malformed") from exc
    if not isinstance(payload, dict):
        raise RunnerAssetReclamationError(f"{field} must be a JSON object")
    return payload


def _protected_directory(
    path: Path, field: str, *, trusted_owner_uid: int | None = None
) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RunnerAssetReclamationError(f"{field} is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid
        not in (
            {0, os.geteuid()}
            if trusted_owner_uid is None
            else {trusted_owner_uid}
        )
        or metadata.st_mode & 0o022
    ):
        raise RunnerAssetReclamationError(f"{field} must be owned and protected")


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass


def _canonical_sha256(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_commit(value: object) -> str:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise ValueError("release commit must be full lowercase hex")
    return value


def _validate_sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be lowercase SHA-256")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result
