"""Exact, read-only Control Host release and recovery-artifact reclamation plans."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from typing import Sequence

from codex_dispatcher.runner_asset_reclamation import inspect_release_assets


_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SCHEMA_VERSION = 1


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
    protected_paths: tuple[str, ...]
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
            "protected_paths": list(self.protected_paths),
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


def plan_control_host_reclamation(
    *,
    releases_root: Path,
    current_link: Path,
    current_release_receipt: Path,
    disaster_recovery_roots: Path,
    disaster_recovery_inputs: Path,
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
                ControlPathAsset(
                    "release_tree",
                    release.path,
                    release.commit,
                    release.tree_sha256,
                    release.allocated_bytes,
                )
            )

    protected_dr = _latest_successful_dr_root(disaster_recovery_roots, current)
    if protected_dr is not None:
        protected_paths.add(str(protected_dr))
    for child in _safe_children(disaster_recovery_roots):
        if child == protected_dr:
            continue
        targets.append(_path_asset("disaster_recovery_root", child, child.name))

    for child in _safe_children(disaster_recovery_inputs):
        if current in child.name:
            protected_paths.add(str(child))
            continue
        targets.append(_path_asset("disaster_recovery_input", child, child.name))

    if release_archives_root is not None and release_archives_root.exists():
        for child in _safe_children(release_archives_root):
            if current in child.name or rollback in child.name:
                protected_paths.add(str(child))
                continue
            targets.append(_path_asset("release_archive", child, child.name))

    ordered = tuple(sorted(targets, key=lambda target: (target.kind, target.path)))
    provisional = ControlHostReclamationPlan(
        current_release_commit=current,
        rollback_release_commit=rollback,
        protected_paths=tuple(sorted(protected_paths)),
        targets=ordered,
        expected_total_bytes=sum(target.allocated_bytes for target in ordered),
        plan_sha256="0" * 64,
    )
    digest = _canonical_sha256(provisional.to_mapping(include_digest=False))
    return ControlHostReclamationPlan(
        current_release_commit=provisional.current_release_commit,
        rollback_release_commit=provisional.rollback_release_commit,
        protected_paths=provisional.protected_paths,
        targets=provisional.targets,
        expected_total_bytes=provisional.expected_total_bytes,
        plan_sha256=digest,
    )


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


def _path_asset(kind: str, path: Path, identity: str) -> ControlPathAsset:
    digest, allocated = _hash_path(path)
    return ControlPathAsset(kind, str(path), identity, digest, allocated)


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
        if (
            candidate.is_symlink()
            or item.st_dev != root_device
            or item.st_mode & 0o022
            or not (stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode))
        ):
            raise ControlHostReclamationError("Control reclamation tree is unsafe")
        relative = candidate.relative_to(path).as_posix()
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


def _write_plan(directory: Path, plan: ControlHostReclamationPlan) -> Path:
    metadata = directory.stat(follow_symlinks=False)
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
    ):
        raise ControlHostReclamationError("Control reclamation plan directory is unsafe")
    path = directory / f"{plan.plan_sha256}.json"
    raw = json.dumps(plan.to_mapping(), sort_keys=True, separators=(",", ":")).encode() + b"\n"
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
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
        directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        staging.unlink(missing_ok=True)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="control-host-reclamation-plan")
    parser.add_argument("--write-plan", action="store_true", required=True)
    args = parser.parse_args(argv)
    del args
    try:
        current = _commit_from_link(
            Path("/opt/codex-dispatcher/current"),
            Path("/opt/codex-dispatcher/releases"),
        )
        plan = plan_control_host_reclamation(
            releases_root=Path("/opt/codex-dispatcher/releases"),
            current_link=Path("/opt/codex-dispatcher/current"),
            current_release_receipt=(
                Path("/opt/codex-dispatcher/release-receipts") / f"{current}.json"
            ),
            disaster_recovery_roots=Path(
                "/var/lib/codex-dispatcher/disaster-recovery-drills"
            ),
            disaster_recovery_inputs=Path(
                "/var/lib/codex-dispatcher/disaster-recovery-inputs"
            ),
            release_archives_root=Path(
                "/var/lib/codex-dispatcher/release-archives"
            ),
        )
        path = _write_plan(
            Path("/var/lib/codex-dispatcher/control-reclamation-plans"), plan
        )
        payload = plan.to_mapping()
        payload.update(ok=True, plan_path=str(path), state_writes=1)
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
