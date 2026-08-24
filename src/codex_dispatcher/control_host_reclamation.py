"""Exact Control Host release and recovery-artifact reclamation lifecycle."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import grp
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Sequence

from codex_dispatcher.control_reclamation_status import (
    build_control_reclamation_status,
)
from codex_dispatcher.runner_asset_reclamation import inspect_release_assets


_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_BUNDLE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SCHEMA_VERSION = 2

RELEASES_ROOT = Path("/opt/codex-dispatcher/releases")
CURRENT_LINK = Path("/opt/codex-dispatcher/current")
RELEASE_RECEIPTS = Path("/opt/codex-dispatcher/release-receipts")
DR_ROOTS = Path("/var/lib/codex-dispatcher/disaster-recovery-drills")
DR_INPUTS = Path("/var/lib/codex-dispatcher/disaster-recovery-inputs")
DR_BUNDLES = Path("/var/lib/codex-dispatcher/disaster-recovery-bundles")
RELEASE_ARCHIVES = Path("/var/lib/codex-dispatcher/release-archives")
PLAN_DIRECTORY = Path("/var/lib/codex-dispatcher/control-reclamation-plans")
RECEIPT_DIRECTORY = Path("/var/lib/codex-dispatcher/control-reclamation-receipts")
STATUS_DIRECTORY = Path("/var/lib/codex-dispatcher/control-reclamation-status")
STATUS_PATH = STATUS_DIRECTORY / "latest.json"
BUNDLE_CONFIRMATION_DIRECTORY = Path(
    "/var/lib/codex-dispatcher/offhost-bundle-confirmations"
)


class ControlHostReclamationError(RuntimeError):
    """Raised when a Control Host inventory cannot be proven exact."""


@dataclass(frozen=True, slots=True)
class ControlPathAsset:
    kind: str
    path: str
    identity: str
    content_sha256: str
    allocated_bytes: int

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "path": self.path,
            "identity": self.identity,
            "content_sha256": self.content_sha256,
            "allocated_bytes": self.allocated_bytes,
        }


@dataclass(frozen=True, slots=True)
class ControlHostReclamationPlan:
    current_release_commit: str
    rollback_release_commit: str
    release_count: int
    recovery_root_count: int
    bundle_count: int
    protected_paths: tuple[str, ...]
    offhost_confirmation_sha256s: tuple[str, ...]
    unconfirmed_bundle_paths: tuple[str, ...]
    targets: tuple[ControlPathAsset, ...]
    expected_total_bytes: int
    plan_sha256: str

    def to_mapping(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "kind": "control_host_reclamation_plan",
            "authorizes_apply": False,
            "current_release_commit": self.current_release_commit,
            "rollback_release_commit": self.rollback_release_commit,
            "release_count": self.release_count,
            "recovery_root_count": self.recovery_root_count,
            "bundle_count": self.bundle_count,
            "protected_paths": list(self.protected_paths),
            "offhost_confirmation_sha256s": list(
                self.offhost_confirmation_sha256s
            ),
            "unconfirmed_bundle_paths": list(self.unconfirmed_bundle_paths),
            "targets": [target.to_mapping() for target in self.targets],
            "expected_total_bytes": self.expected_total_bytes,
            "deletion_commands": [
                f"delete exact {target.kind} {target.path} at "
                f"content_sha256={target.content_sha256}"
                for target in self.targets
            ],
        }
        if include_digest:
            payload["plan_sha256"] = self.plan_sha256
        return payload

    @classmethod
    def from_mapping(cls, payload: object) -> ControlHostReclamationPlan:
        expected = {
            "schema_version",
            "kind",
            "authorizes_apply",
            "current_release_commit",
            "rollback_release_commit",
            "release_count",
            "recovery_root_count",
            "bundle_count",
            "protected_paths",
            "offhost_confirmation_sha256s",
            "unconfirmed_bundle_paths",
            "targets",
            "expected_total_bytes",
            "deletion_commands",
            "plan_sha256",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload.get("schema_version") != _SCHEMA_VERSION
            or payload.get("kind") != "control_host_reclamation_plan"
            or payload.get("authorizes_apply") is not False
        ):
            raise ControlHostReclamationError("Control reclamation plan fields are invalid")
        protected = payload.get("protected_paths")
        confirmations = payload.get("offhost_confirmation_sha256s")
        unconfirmed = payload.get("unconfirmed_bundle_paths")
        target_rows = payload.get("targets")
        if (
            not isinstance(protected, list)
            or not all(isinstance(item, str) for item in protected)
            or not isinstance(confirmations, list)
            or not all(
                isinstance(item, str) and _SHA256_RE.fullmatch(item)
                for item in confirmations
            )
            or not isinstance(unconfirmed, list)
            or not all(isinstance(item, str) for item in unconfirmed)
            or not isinstance(target_rows, list)
            or not all(isinstance(item, dict) for item in target_rows)
        ):
            raise ControlHostReclamationError("Control reclamation plan lists are invalid")
        targets: list[ControlPathAsset] = []
        for row in target_rows:
            if set(row) != {
                "kind",
                "path",
                "identity",
                "content_sha256",
                "allocated_bytes",
            }:
                raise ControlHostReclamationError("Control reclamation target fields are invalid")
            target = ControlPathAsset(
                kind=row.get("kind"),  # type: ignore[arg-type]
                path=row.get("path"),  # type: ignore[arg-type]
                identity=row.get("identity"),  # type: ignore[arg-type]
                content_sha256=row.get("content_sha256"),  # type: ignore[arg-type]
                allocated_bytes=row.get("allocated_bytes"),  # type: ignore[arg-type]
            )
            _validate_path_asset(target)
            targets.append(target)
        plan = cls(
            current_release_commit=payload.get("current_release_commit"),  # type: ignore[arg-type]
            rollback_release_commit=payload.get("rollback_release_commit"),  # type: ignore[arg-type]
            release_count=payload.get("release_count"),  # type: ignore[arg-type]
            recovery_root_count=payload.get("recovery_root_count"),  # type: ignore[arg-type]
            bundle_count=payload.get("bundle_count"),  # type: ignore[arg-type]
            protected_paths=tuple(protected),
            offhost_confirmation_sha256s=tuple(confirmations),
            unconfirmed_bundle_paths=tuple(unconfirmed),
            targets=tuple(targets),
            expected_total_bytes=payload.get("expected_total_bytes"),  # type: ignore[arg-type]
            plan_sha256=payload.get("plan_sha256"),  # type: ignore[arg-type]
        )
        _validate_plan(plan)
        commands = payload.get("deletion_commands")
        if commands != plan.to_mapping()["deletion_commands"]:
            raise ControlHostReclamationError("Control deletion commands conflict with targets")
        if _canonical_sha256(plan.to_mapping(include_digest=False)) != plan.plan_sha256:
            raise ControlHostReclamationError("Control reclamation plan digest is invalid")
        return plan


def plan_control_host_reclamation(
    *,
    releases_root: Path,
    current_link: Path,
    current_release_receipt: Path,
    disaster_recovery_roots: Path,
    disaster_recovery_inputs: Path,
    disaster_recovery_bundles: Path | None = None,
    bundle_confirmation_root: Path | None = None,
    release_archives_root: Path | None = None,
) -> ControlHostReclamationPlan:
    """Inventory exact unprotected artifacts without deleting or changing them."""
    receipt = _read_json(current_release_receipt, maximum=256 * 1024)
    current = _commit_from_link(current_link, releases_root)
    rollback = _commit_from_release_path(receipt.get("previous_control"))
    if (
        receipt.get("kind") != "codex_dispatcher_release"
        or receipt.get("operation") != "apply"
        or receipt.get("status") != "committed"
        or receipt.get("committed") is not True
        or receipt.get("release_commit") != current
        or current == rollback
    ):
        raise ControlHostReclamationError("current release receipt is inconsistent")

    targets: list[ControlPathAsset] = []
    protected_paths = {
        str(releases_root / current),
        str(releases_root / rollback),
        str(current_release_receipt),
    }
    releases = inspect_release_assets(releases_root)
    observed = {release.commit for release in releases}
    if {current, rollback} - observed:
        raise ControlHostReclamationError("protected Control release is unavailable")
    for release in releases:
        if release.commit not in {current, rollback}:
            targets.append(
                _path_asset("release_tree", Path(release.path), release.commit)
            )

    recovery_roots = _safe_children(disaster_recovery_roots)
    protected_dr_roots = {
        path
        for commit in (current, rollback)
        if (
            path := _latest_successful_dr_root(
                disaster_recovery_roots, commit
            )
        )
        is not None
    }
    protected_paths.update(str(path) for path in protected_dr_roots)
    for child in recovery_roots:
        if child in protected_dr_roots:
            continue
        targets.append(_path_asset("disaster_recovery_root", child, child.name))

    for child in _safe_children(disaster_recovery_inputs):
        if current in child.name or rollback in child.name:
            protected_paths.add(str(child))
            continue
        targets.append(_path_asset("disaster_recovery_input", child, child.name))

    if release_archives_root is not None and release_archives_root.exists():
        for child in _safe_children(release_archives_root):
            if current in child.name or rollback in child.name:
                protected_paths.add(str(child))
                continue
            targets.append(_path_asset("release_archive", child, child.name))

    confirmation_sha256s: set[str] = set()
    unconfirmed_bundles: list[str] = []
    bundle_count = 0
    if (disaster_recovery_bundles is None) != (bundle_confirmation_root is None):
        raise ControlHostReclamationError("bundle inventory inputs are incomplete")
    if disaster_recovery_bundles is not None:
        assert bundle_confirmation_root is not None
        bundles = _safe_children(disaster_recovery_bundles)
        bundle_count = len(bundles)
        confirmations = _load_bundle_confirmations(bundle_confirmation_root)
        protected_bundles = {
            bundle
            for dr_root in protected_dr_roots
            if (
                bundle := _bundle_for_dr_receipt(
                    dr_root, disaster_recovery_bundles
                )
            )
            is not None
        }
        protected_paths.update(str(path) for path in protected_bundles)
        for child in bundles:
            if child in protected_bundles:
                continue
            manifest = _bundle_manifest_sha256(child)
            confirmation = confirmations.get(manifest)
            if confirmation is None:
                protected_paths.add(str(child))
                unconfirmed_bundles.append(str(child))
                continue
            confirmation_sha256s.add(confirmation)
            targets.append(_path_asset("disaster_recovery_bundle", child, child.name))

    ordered = tuple(sorted(targets, key=lambda target: (target.kind, target.path)))
    provisional = ControlHostReclamationPlan(
        current_release_commit=current,
        rollback_release_commit=rollback,
        release_count=len(releases),
        recovery_root_count=len(recovery_roots),
        bundle_count=bundle_count,
        protected_paths=tuple(sorted(protected_paths)),
        offhost_confirmation_sha256s=tuple(sorted(confirmation_sha256s)),
        unconfirmed_bundle_paths=tuple(sorted(unconfirmed_bundles)),
        targets=ordered,
        expected_total_bytes=sum(target.allocated_bytes for target in ordered),
        plan_sha256="0" * 64,
    )
    digest = _canonical_sha256(provisional.to_mapping(include_digest=False))
    final = ControlHostReclamationPlan(
        current_release_commit=provisional.current_release_commit,
        rollback_release_commit=provisional.rollback_release_commit,
        release_count=provisional.release_count,
        recovery_root_count=provisional.recovery_root_count,
        bundle_count=provisional.bundle_count,
        protected_paths=provisional.protected_paths,
        offhost_confirmation_sha256s=provisional.offhost_confirmation_sha256s,
        unconfirmed_bundle_paths=provisional.unconfirmed_bundle_paths,
        targets=provisional.targets,
        expected_total_bytes=provisional.expected_total_bytes,
        plan_sha256=digest,
    )
    _validate_plan(final)
    return final


def _latest_successful_dr_root(root: Path, current_commit: str) -> Path | None:
    matches: list[Path] = []
    for child in _safe_children(root):
        if not child.is_dir():
            continue
        receipt = child / "receipt.json"
        if not receipt.is_file() or receipt.is_symlink():
            continue
        payload = _read_json(receipt, maximum=256 * 1024)
        if (
            payload.get("kind") == "schema18_disaster_recovery_drill"
            and payload.get("status") == "passed"
            and payload.get("release_commit") == current_commit
            and payload.get("online_state_modified") is False
        ):
            matches.append(child)
    return max(matches, key=lambda path: path.name) if matches else None


def _bundle_for_dr_receipt(dr_root: Path | None, bundle_root: Path) -> Path | None:
    if dr_root is None:
        return None
    payload = _read_json(dr_root / "receipt.json", maximum=256 * 1024)
    source = payload.get("source_backup")
    if not isinstance(source, str):
        raise ControlHostReclamationError("successful DR receipt lacks its source bundle")
    source_path = Path(source)
    candidate = source_path.parent
    if (
        not source_path.is_absolute()
        or source_path.name != "state.db"
        or candidate.parent != bundle_root
        or candidate not in _safe_children(bundle_root)
        or not source_path.is_file()
        or source_path.is_symlink()
    ):
        raise ControlHostReclamationError("successful DR receipt source bundle is unsafe")
    return candidate


def _bundle_manifest_sha256(bundle: Path) -> str:
    if not bundle.is_dir() or bundle.is_symlink():
        raise ControlHostReclamationError("disaster recovery bundle is unsafe")
    manifest = bundle / "manifest.json"
    payload = _read_json(manifest, maximum=1024 * 1024)
    if (
        payload.get("kind") != "codex_dispatcher_disaster_recovery_bundle"
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("release_commit"), str)
        or _COMMIT_RE.fullmatch(str(payload.get("release_commit"))) is None
    ):
        raise ControlHostReclamationError("disaster recovery bundle manifest is invalid")
    return _sha256_file(manifest)


def _load_bundle_confirmations(root: Path) -> dict[str, str]:
    confirmations: dict[str, str] = {}
    for path in _safe_children(root):
        if not path.is_file() or path.suffix != ".json":
            raise ControlHostReclamationError("off-host confirmation inventory is invalid")
        payload = _read_json(path, maximum=64 * 1024)
        manifest = _validate_bundle_confirmation(payload)
        if path.name != f"{manifest}.json":
            raise ControlHostReclamationError("off-host confirmation filename is invalid")
        confirmations[manifest] = _sha256_file(path)
    return confirmations


def _validate_bundle_confirmation(payload: dict[str, object]) -> str:
    manifest = payload.get("manifest_sha256")
    bundle_id = payload.get("bundle_id")
    copy_id = payload.get("off_host_copy_id")
    confirmed_at = payload.get("confirmed_at")
    try:
        moment = datetime.fromisoformat(str(confirmed_at).replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ControlHostReclamationError(
            "off-host confirmation timestamp is invalid"
        ) from exc
    if (
        set(payload)
        != {
            "schema_version",
            "kind",
            "status",
            "bundle_id",
            "manifest_sha256",
            "off_host_copy_id",
            "confirmed_at",
        }
        or payload.get("schema_version") != 1
        or payload.get("kind") != "control_bundle_offhost_confirmation"
        or payload.get("status") != "confirmed"
        or not isinstance(manifest, str)
        or _SHA256_RE.fullmatch(manifest) is None
        or not isinstance(bundle_id, str)
        or _BUNDLE_ID_RE.fullmatch(bundle_id) is None
        or not isinstance(copy_id, str)
        or not 0 < len(copy_id) <= 512
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in copy_id)
        or not isinstance(confirmed_at, str)
        or moment.tzinfo is None
    ):
        raise ControlHostReclamationError("off-host confirmation receipt is invalid")
    return manifest


def confirm_offhost_bundle(
    *,
    bundle_id: str,
    manifest_sha256: str,
    off_host_copy_id: str,
    bundle_root: Path,
    confirmation_root: Path,
    now: datetime | None = None,
    trusted_confirmation_owner_uid: int = 0,
) -> dict[str, object]:
    """Record an operator-confirmed, hash-equal copy outside both Linux hosts."""
    if _BUNDLE_ID_RE.fullmatch(bundle_id) is None:
        raise ValueError("bundle_id is invalid")
    if _SHA256_RE.fullmatch(manifest_sha256) is None:
        raise ValueError("manifest_sha256 must be lowercase SHA-256")
    if (
        not 0 < len(off_host_copy_id) <= 512
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in off_host_copy_id)
    ):
        raise ValueError("off_host_copy_id is invalid")
    bundle = bundle_root / bundle_id
    if bundle.parent != bundle_root or bundle not in _safe_children(bundle_root):
        raise ControlHostReclamationError("confirmed bundle is unavailable")
    observed = _bundle_manifest_sha256(bundle)
    if observed != manifest_sha256:
        raise ControlHostReclamationError("off-host manifest differs from Control bundle")
    from codex_dispatcher.disaster_recovery import load_disaster_recovery_bundle

    loaded = load_disaster_recovery_bundle(bundle)
    if loaded.manifest_sha256 != manifest_sha256:
        raise ControlHostReclamationError("confirmed bundle failed integral validation")
    _protected_directory(
        confirmation_root, owner_uid=trusted_confirmation_owner_uid
    )
    path = confirmation_root / f"{manifest_sha256}.json"
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            raise ControlHostReclamationError("off-host confirmation identity conflicts")
        existing = _read_json(path, maximum=64 * 1024)
        _validate_bundle_confirmation(existing)
        if (
            existing.get("bundle_id") != bundle_id
            or existing.get("manifest_sha256") != manifest_sha256
            or existing.get("off_host_copy_id") != off_host_copy_id
        ):
            raise ControlHostReclamationError("off-host confirmation identity conflicts")
        return {
            **existing,
            "ok": True,
            "receipt_path": str(path),
            "state_writes": 0,
        }
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("confirmation timestamp must be timezone-aware")
    receipt: dict[str, object] = {
        "schema_version": 1,
        "kind": "control_bundle_offhost_confirmation",
        "status": "confirmed",
        "bundle_id": bundle_id,
        "manifest_sha256": manifest_sha256,
        "off_host_copy_id": off_host_copy_id,
        "confirmed_at": moment.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
    }
    _write_json_atomic(path, receipt, owner_uid=trusted_confirmation_owner_uid)
    return {**receipt, "ok": True, "receipt_path": str(path), "state_writes": 1}


def _path_asset(kind: str, path: Path, identity: str) -> ControlPathAsset:
    digest, allocated = _hash_path(path)
    asset = ControlPathAsset(kind, str(path), identity, digest, allocated)
    _validate_path_asset(asset)
    return asset


def _validate_path_asset(asset: ControlPathAsset) -> None:
    if (
        asset.kind
        not in {
            "release_tree",
            "disaster_recovery_root",
            "disaster_recovery_input",
            "disaster_recovery_bundle",
            "release_archive",
        }
        or not isinstance(asset.path, str)
        or not Path(asset.path).is_absolute()
        or not isinstance(asset.identity, str)
        or not asset.identity
        or not isinstance(asset.content_sha256, str)
        or _SHA256_RE.fullmatch(asset.content_sha256) is None
        or type(asset.allocated_bytes) is not int
        or asset.allocated_bytes < 0
    ):
        raise ControlHostReclamationError("Control reclamation target is invalid")


def _validate_plan(plan: ControlHostReclamationPlan) -> None:
    numeric = (
        plan.release_count,
        plan.recovery_root_count,
        plan.bundle_count,
        plan.expected_total_bytes,
    )
    if (
        _COMMIT_RE.fullmatch(str(plan.current_release_commit)) is None
        or _COMMIT_RE.fullmatch(str(plan.rollback_release_commit)) is None
        or plan.current_release_commit == plan.rollback_release_commit
        or any(type(value) is not int or value < 0 for value in numeric)
        or not isinstance(plan.plan_sha256, str)
        or _SHA256_RE.fullmatch(plan.plan_sha256) is None
        or tuple(sorted(plan.protected_paths)) != plan.protected_paths
        or len(set(plan.protected_paths)) != len(plan.protected_paths)
        or tuple(sorted(plan.offhost_confirmation_sha256s))
        != plan.offhost_confirmation_sha256s
        or len(set(plan.offhost_confirmation_sha256s))
        != len(plan.offhost_confirmation_sha256s)
        or tuple(sorted(plan.unconfirmed_bundle_paths))
        != plan.unconfirmed_bundle_paths
        or len(set(plan.unconfirmed_bundle_paths))
        != len(plan.unconfirmed_bundle_paths)
        or tuple(sorted(plan.targets, key=lambda item: (item.kind, item.path)))
        != plan.targets
        or len({target.path for target in plan.targets}) != len(plan.targets)
        or plan.expected_total_bytes
        != sum(target.allocated_bytes for target in plan.targets)
    ):
        raise ControlHostReclamationError("Control reclamation plan is inconsistent")
    for target in plan.targets:
        _validate_path_asset(target)
    protected = set(plan.protected_paths)
    if protected.intersection(target.path for target in plan.targets):
        raise ControlHostReclamationError("Control target is also protected")


def apply_control_host_reclamation(
    *,
    approved_plan: ControlHostReclamationPlan,
    observed_plan: ControlHostReclamationPlan,
    receipt_directory: Path,
    allowed_roots: dict[str, Path],
    now: datetime | None = None,
    trusted_receipt_owner_uid: int = 0,
) -> dict[str, object]:
    """Reinspect, delete exact direct children, and publish a permanent receipt."""
    _validate_plan(approved_plan)
    _validate_plan(observed_plan)
    _protected_directory(receipt_directory, owner_uid=trusted_receipt_owner_uid)
    if observed_plan != approved_plan:
        raise ControlHostReclamationError("Control assets changed after plan approval")
    expected_kinds = {target.kind for target in approved_plan.targets}
    if not expected_kinds.issubset(allowed_roots) or set(allowed_roots) - {
        "release_tree",
        "disaster_recovery_root",
        "disaster_recovery_input",
        "disaster_recovery_bundle",
        "release_archive",
    }:
        raise ControlHostReclamationError("Control deletion roots are incomplete")
    for root in allowed_roots.values():
        _protected_directory(root)
    receipt_path = receipt_directory / f"{approved_plan.plan_sha256}.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        payload = _read_json(receipt_path, maximum=1024 * 1024)
        if (
            payload.get("kind") == "control_host_reclamation_receipt"
            and payload.get("plan_sha256") == approved_plan.plan_sha256
            and payload.get("status") == "reclaimed"
        ):
            return payload
        raise ControlHostReclamationError("Control reclamation receipt already exists")
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("Control reclamation timestamp must be timezone-aware")
    completed: list[str] = []
    reclaimed_bytes = 0
    receipt: dict[str, object] = {
        "schema_version": 1,
        "kind": "control_host_reclamation_receipt",
        "status": "prepared",
        "plan_sha256": approved_plan.plan_sha256,
        "current_release_commit": approved_plan.current_release_commit,
        "rollback_release_commit": approved_plan.rollback_release_commit,
        "targets": [target.to_mapping() for target in approved_plan.targets],
        "completed_paths": completed,
        "expected_total_bytes": approved_plan.expected_total_bytes,
        "reclaimed_bytes": reclaimed_bytes,
        "started_at": moment.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
    }
    _write_json_atomic(
        receipt_path, receipt, owner_uid=trusted_receipt_owner_uid
    )
    try:
        for target in approved_plan.targets:
            reclaimed_bytes += _delete_exact_control_asset(
                target, root=allowed_roots[target.kind]
            )
            completed.append(target.path)
            receipt.update(
                status="in_progress",
                completed_paths=list(completed),
                reclaimed_bytes=reclaimed_bytes,
            )
            _write_json_atomic(
                receipt_path, receipt, owner_uid=trusted_receipt_owner_uid
            )
    except BaseException:
        receipt.update(
            status="failed",
            completed_paths=list(completed),
            reclaimed_bytes=reclaimed_bytes,
            rollback_boundary=(
                "deleted artifacts are not recreated automatically; retain current and "
                "rollback releases, the off-host bundle copies, release archives, and this receipt"
            ),
        )
        _write_json_atomic(
            receipt_path, receipt, owner_uid=trusted_receipt_owner_uid
        )
        raise
    receipt.update(
        status="reclaimed",
        completed_paths=list(completed),
        reclaimed_bytes=reclaimed_bytes,
        completed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    _write_json_atomic(
        receipt_path, receipt, owner_uid=trusted_receipt_owner_uid
    )
    return receipt


def _delete_exact_control_asset(target: ControlPathAsset, *, root: Path) -> int:
    _validate_path_asset(target)
    path = Path(target.path)
    if path.parent != root or path.name != target.identity:
        raise ControlHostReclamationError("Control deletion target escapes its fixed root")
    digest, allocated = _hash_path(path)
    if digest != target.content_sha256 or allocated != target.allocated_bytes:
        raise ControlHostReclamationError("Control deletion target changed after reinspection")
    if path.is_dir() and not shutil.rmtree.avoids_symlink_attacks:
        raise ControlHostReclamationError("safe fd-relative recursive deletion is unavailable")
    parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        current = path.stat(follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise ControlHostReclamationError("Control deletion target raced reinspection")
        if stat.S_ISREG(before.st_mode):
            os.unlink(path.name, dir_fd=parent_fd)
        elif stat.S_ISDIR(before.st_mode):
            shutil.rmtree(path.name, dir_fd=parent_fd)
        else:
            raise ControlHostReclamationError("Control deletion target type changed")
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ControlHostReclamationError("Control deletion target still exists")
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return allocated


def _hash_path(path: Path) -> tuple[str, int]:
    metadata = path.stat(follow_symlinks=False)
    if path.is_symlink() or metadata.st_mode & 0o022:
        raise ControlHostReclamationError("Control reclamation target is unsafe")
    if stat.S_ISREG(metadata.st_mode):
        return _sha256_file(path), metadata.st_blocks * 512
    if not stat.S_ISDIR(metadata.st_mode):
        raise ControlHostReclamationError("Control reclamation target has unknown type")
    rows: list[dict[str, object]] = []
    allocated = metadata.st_blocks * 512
    root_device = metadata.st_dev
    for candidate in sorted(path.rglob("*")):
        item = candidate.stat(follow_symlinks=False)
        relative = candidate.relative_to(path).as_posix()
        if candidate.is_symlink():
            link_target = os.readlink(candidate)
            if Path(link_target).is_absolute() or ".." in Path(link_target).parts:
                raise ControlHostReclamationError(
                    "Control reclamation tree contains an unsafe symlink"
                )
            allocated += item.st_blocks * 512
            rows.append(
                {
                    "path": relative,
                    "type": "symlink",
                    "mode": stat.S_IMODE(item.st_mode),
                    "size": item.st_size,
                    "link_target": link_target,
                }
            )
            continue
        if (
            item.st_dev != root_device
            or item.st_mode & 0o022
            or not (stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode))
        ):
            raise ControlHostReclamationError("Control reclamation tree is unsafe")
        allocated += item.st_blocks * 512
        rows.append(
            {
                "path": relative,
                "type": "directory" if stat.S_ISDIR(item.st_mode) else "file",
                "mode": stat.S_IMODE(item.st_mode),
                "size": item.st_size,
                "sha256": None if stat.S_ISDIR(item.st_mode) else _sha256_file(candidate),
            }
        )
    return _canonical_sha256(rows), allocated


def _safe_children(path: Path) -> tuple[Path, ...]:
    metadata = path.stat(follow_symlinks=False)
    if path.is_symlink() or not path.is_dir() or metadata.st_mode & 0o022:
        raise ControlHostReclamationError("Control reclamation inventory root is unsafe")
    children = tuple(sorted(path.iterdir()))
    if any(child.is_symlink() for child in children):
        raise ControlHostReclamationError("Control reclamation inventory contains a symlink")
    return children


def _commit_from_link(current_link: Path, releases_root: Path) -> str:
    try:
        value = os.readlink(current_link)
    except OSError as exc:
        raise ControlHostReclamationError("Control current link is unavailable") from exc
    commit = value.rstrip("/").split("/")[-1]
    if _COMMIT_RE.fullmatch(commit) is None:
        raise ControlHostReclamationError("Control current link is invalid")
    if current_link.resolve() != (releases_root / commit).resolve():
        raise ControlHostReclamationError("Control current link escapes releases root")
    return commit


def _commit_from_release_path(value: object) -> str:
    if not isinstance(value, str):
        raise ControlHostReclamationError("rollback Control release is absent")
    commit = value.rstrip("/").split("/")[-1]
    if _COMMIT_RE.fullmatch(commit) is None:
        raise ControlHostReclamationError("rollback Control release is invalid")
    return commit


def _read_json(path: Path, *, maximum: int) -> dict[str, object]:
    metadata = path.stat(follow_symlinks=False)
    raw = path.read_bytes()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or not 0 < len(raw) <= maximum
    ):
        raise ControlHostReclamationError("Control reclamation JSON is unsafe")
    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(payload, dict):
        raise ControlHostReclamationError("Control reclamation JSON must be an object")
    return payload


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(raw.encode()).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON field")
        payload[key] = value
    return payload


def _protected_directory(path: Path, *, owner_uid: int | None = None) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_dir()
        or metadata.st_mode & 0o022
        or (owner_uid is not None and metadata.st_uid != owner_uid)
    ):
        raise ControlHostReclamationError("Control maintenance directory is unsafe")


def _encoded_json(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _write_json_atomic(
    path: Path,
    payload: dict[str, object],
    *,
    mode: int = 0o600,
    group_id: int | None = None,
    owner_uid: int = 0,
) -> None:
    if path.is_symlink():
        raise ControlHostReclamationError("Control maintenance output path is unsafe")
    if path.exists():
        existing = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != owner_uid
            or existing.st_mode & 0o022
            or existing.st_nlink != 1
        ):
            raise ControlHostReclamationError("Control maintenance output path is unsafe")
    raw = _encoded_json(payload)
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        if group_id is not None:
            os.fchown(descriptor, 0, group_id)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        staging.unlink(missing_ok=True)


def _load_plan(directory: Path, plan_sha256: str) -> ControlHostReclamationPlan:
    if _SHA256_RE.fullmatch(plan_sha256) is None:
        raise ValueError("plan_sha256 must be lowercase SHA-256")
    _protected_directory(directory, owner_uid=0)
    path = directory / f"{plan_sha256}.json"
    plan = ControlHostReclamationPlan.from_mapping(
        _read_json(path, maximum=4 * 1024 * 1024)
    )
    if plan.plan_sha256 != plan_sha256:
        raise ControlHostReclamationError("Control plan filename conflicts with content")
    return plan


def _write_plan(directory: Path, plan: ControlHostReclamationPlan) -> Path:
    _protected_directory(directory, owner_uid=0)
    path = directory / f"{plan.plan_sha256}.json"
    raw = _encoded_json(plan.to_mapping())
    if path.is_symlink():
        raise ControlHostReclamationError("Control reclamation plan path is unsafe")
    if path.exists():
        existing = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != 0
            or existing.st_mode & 0o022
            or existing.st_nlink != 1
        ):
            raise ControlHostReclamationError("Control reclamation plan path is unsafe")
        if path.read_bytes() != raw:
            raise ControlHostReclamationError("Control reclamation plan identity conflicts")
        return path
    _write_json_atomic(path, plan.to_mapping())
    return path


def _live_plan() -> ControlHostReclamationPlan:
    current = _commit_from_link(CURRENT_LINK, RELEASES_ROOT)
    return plan_control_host_reclamation(
        releases_root=RELEASES_ROOT,
        current_link=CURRENT_LINK,
        current_release_receipt=RELEASE_RECEIPTS / f"{current}.json",
        disaster_recovery_roots=DR_ROOTS,
        disaster_recovery_inputs=DR_INPUTS,
        disaster_recovery_bundles=DR_BUNDLES,
        bundle_confirmation_root=BUNDLE_CONFIRMATION_DIRECTORY,
        release_archives_root=RELEASE_ARCHIVES,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="control-host-reclamation")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--write-plan", action="store_true", required=True)
    commands.add_parser("auto-plan")
    recheck = commands.add_parser("recheck")
    recheck.add_argument("--plan-sha256", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--plan-sha256", required=True)
    apply.add_argument("--apply", action="store_true", required=True)
    confirm = commands.add_parser("bundle-confirm")
    confirm.add_argument("--bundle-id", required=True)
    confirm.add_argument("--manifest-sha256", required=True)
    confirm.add_argument("--off-host-copy-id", required=True)
    confirm.add_argument("--apply", action="store_true", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.geteuid() != 0:
        print(json.dumps({"ok": False, "error": "root is required"}, sort_keys=True))
        return 1
    try:
        if args.command == "bundle-confirm":
            payload = confirm_offhost_bundle(
                bundle_id=args.bundle_id,
                manifest_sha256=args.manifest_sha256,
                off_host_copy_id=args.off_host_copy_id,
                bundle_root=DR_BUNDLES,
                confirmation_root=BUNDLE_CONFIRMATION_DIRECTORY,
            )
        else:
            observed = _live_plan()
            if args.command == "plan":
                path = _write_plan(PLAN_DIRECTORY, observed)
                payload = observed.to_mapping()
                payload.update(ok=True, plan_path=str(path), state_writes=1)
            elif args.command == "auto-plan":
                filesystem = os.statvfs(DR_ROOTS.parent)
                status = build_control_reclamation_status(
                    host_available_bytes=filesystem.f_bavail * filesystem.f_frsize,
                    current_release_commit=observed.current_release_commit,
                    release_count=observed.release_count,
                    recovery_root_count=observed.recovery_root_count,
                    target_count=len(observed.targets),
                    bundle_target_count=sum(
                        target.kind == "disaster_recovery_bundle"
                        for target in observed.targets
                    ),
                    unconfirmed_bundle_count=len(observed.unconfirmed_bundle_paths),
                    expected_total_bytes=observed.expected_total_bytes,
                    plan_sha256=observed.plan_sha256,
                )
                path = (
                    _write_plan(PLAN_DIRECTORY, observed)
                    if status.trigger_reasons
                    else None
                )
                _protected_directory(STATUS_DIRECTORY, owner_uid=0)
                group_id = grp.getgrnam("codex-dispatcher").gr_gid
                if STATUS_DIRECTORY.stat(follow_symlinks=False).st_gid != group_id:
                    raise ControlHostReclamationError(
                        "Control reclamation status group is invalid"
                    )
                _write_json_atomic(
                    STATUS_PATH,
                    status.to_mapping(),
                    mode=0o640,
                    group_id=group_id,
                )
                payload = status.to_mapping()
                payload.update(
                    ok=True,
                    authorizes_apply=False,
                    status_path=str(STATUS_PATH),
                    plan_path=str(path) if path is not None else None,
                    state_writes=2 if path is not None else 1,
                )
            else:
                approved = _load_plan(PLAN_DIRECTORY, args.plan_sha256)
                if approved != observed:
                    raise ControlHostReclamationError(
                        "Control assets changed after stored plan"
                    )
                if args.command == "recheck":
                    payload = observed.to_mapping()
                    payload.update(
                        ok=True,
                        reinspection_matches=True,
                        state_writes=0,
                        requires_separate_apply=True,
                    )
                else:
                    roots = {
                        "release_tree": RELEASES_ROOT,
                        "disaster_recovery_root": DR_ROOTS,
                        "disaster_recovery_input": DR_INPUTS,
                        "disaster_recovery_bundle": DR_BUNDLES,
                    }
                    if RELEASE_ARCHIVES.exists():
                        roots["release_archive"] = RELEASE_ARCHIVES
                    payload = apply_control_host_reclamation(
                        approved_plan=approved,
                        observed_plan=observed,
                        receipt_directory=RECEIPT_DIRECTORY,
                        allowed_roots=roots,
                    )
                    payload.update(ok=True, state_writes=len(observed.targets) + 2)
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
