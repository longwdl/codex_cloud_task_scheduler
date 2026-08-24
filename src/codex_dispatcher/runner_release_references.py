"""Transactional release ownership of Runner reclamation references."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Sequence


REFERENCES_PATH = Path(
    "/srv/codex-runner/etc/reclamation-rollback-references.json"
)
RECEIPT_DIRECTORY = Path(
    "/srv/codex-runner/reclamation-reference-receipts"
)
RUNNER_CONFIG = Path("/srv/codex-runner/etc/config.json")
CURRENT_LINK = Path("/srv/codex-runner/current")

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_IMAGE_REF_RE = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class RunnerReleaseReferenceError(RuntimeError):
    """Raised before a release can safely change the reference ledger."""


def apply_release_references(
    *,
    references_path: Path,
    receipt_directory: Path,
    runner_config: Path,
    current_link: Path,
    release_commit: str,
    previous_release_commit: str,
    trusted_owner_uid: int = 0,
    now: datetime | None = None,
) -> dict[str, object]:
    """Prepare and commit the two-phase ledger transaction."""
    prepare_release_references(
        references_path=references_path,
        receipt_directory=receipt_directory,
        runner_config=runner_config,
        current_link=current_link,
        release_commit=release_commit,
        previous_release_commit=previous_release_commit,
        trusted_owner_uid=trusted_owner_uid,
        now=now,
    )
    return commit_prepared_release_references(
        references_path=references_path,
        receipt_directory=receipt_directory,
        current_link=current_link,
        release_commit=release_commit,
        trusted_owner_uid=trusted_owner_uid,
    )


def prepare_release_references(
    *,
    references_path: Path,
    receipt_directory: Path,
    runner_config: Path,
    current_link: Path,
    release_commit: str,
    previous_release_commit: str,
    trusted_owner_uid: int = 0,
    now: datetime | None = None,
) -> dict[str, object]:
    """Durably record exact before/after intent before changing the live ledger."""
    _validate_commit(release_commit, "release_commit")
    _validate_commit(previous_release_commit, "previous_release_commit")
    if release_commit == previous_release_commit:
        raise RunnerReleaseReferenceError("release and rollback commits must differ")
    if _current_commit(current_link) != release_commit:
        raise RunnerReleaseReferenceError("Runner current does not match release commit")
    _protected_directory(receipt_directory, trusted_owner_uid)
    before = _read_protected_json(references_path, trusted_owner_uid)
    _validate_existing_references(before)
    image = _configured_image(runner_config, trusted_owner_uid)
    after: dict[str, object] = {
        "schema_version": 2,
        "kind": "runner_reclamation_rollback_references",
        "current_release_commit": release_commit,
        "immediate_rollback_release_commit": previous_release_commit,
        "current_image_ref": image,
        "protected_release_commits": [release_commit, previous_release_commit],
        "protected_image_refs": [image],
    }
    receipt_path = receipt_directory / f"{release_commit}.apply.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        existing = _read_protected_json(receipt_path, trusted_owner_uid)
        _validate_apply_receipt(existing, release_commit)
        existing_after = existing.get("after_references")
        existing_before_sha = existing.get("before_sha256")
        existing_after_sha = existing.get("after_sha256")
        if (
            existing.get("previous_release_commit") != previous_release_commit
            or existing.get("current_image_ref") != image
            or existing_after != after
            or not isinstance(existing_before_sha, str)
            or not isinstance(existing_after_sha, str)
            or _payload_sha256(before) not in {existing_before_sha, existing_after_sha}
        ):
            raise RunnerReleaseReferenceError("reference apply receipt conflicts")
        return _result(
            receipt_path,
            existing_before_sha,
            existing_after_sha,
            str(existing["status"]),
            state_writes=0,
        )
    before_sha = _payload_sha256(before)
    after_sha = _payload_sha256(after)
    moment = _utc_timestamp(now)
    receipt: dict[str, object] = {
        "schema_version": 1,
        "kind": "runner_reclamation_reference_change_receipt",
        "operation": "apply",
        "status": "prepared",
        "release_commit": release_commit,
        "previous_release_commit": previous_release_commit,
        "current_image_ref": image,
        "before_sha256": before_sha,
        "after_sha256": after_sha,
        "before_references": before,
        "after_references": after,
        "recorded_at": moment,
    }
    _write_json_exclusive(receipt_path, receipt)
    return _result(receipt_path, before_sha, after_sha, "prepared", state_writes=1)


def commit_prepared_release_references(
    *,
    references_path: Path,
    receipt_directory: Path,
    current_link: Path,
    release_commit: str,
    trusted_owner_uid: int = 0,
) -> dict[str, object]:
    """Commit a prepared intent; an interrupted commit remains rollback-recoverable."""
    _validate_commit(release_commit, "release_commit")
    _protected_directory(receipt_directory, trusted_owner_uid)
    if _current_commit(current_link) != release_commit:
        raise RunnerReleaseReferenceError("Runner current does not match release commit")
    receipt_path = receipt_directory / f"{release_commit}.apply.json"
    receipt = _read_protected_json(receipt_path, trusted_owner_uid)
    _validate_apply_receipt(receipt, release_commit)
    rollback_path = receipt_directory / f"{release_commit}.rollback.json"
    if rollback_path.exists() or rollback_path.is_symlink():
        raise RunnerReleaseReferenceError("reference transaction was already rolled back")
    before = receipt["before_references"]
    after = receipt["after_references"]
    before_sha = receipt["before_sha256"]
    after_sha = receipt["after_sha256"]
    assert isinstance(before, dict) and isinstance(after, dict)
    assert isinstance(before_sha, str) and isinstance(after_sha, str)
    observed = _read_protected_json(references_path, trusted_owner_uid)
    observed_sha = _payload_sha256(observed)
    state_writes = 0
    if receipt["status"] == "applied":
        if observed != after or observed_sha != after_sha:
            raise RunnerReleaseReferenceError("applied reference ledger changed")
        return _result(
            receipt_path, before_sha, after_sha, "applied", state_writes=0
        )
    if observed == before and observed_sha == before_sha:
        _write_json_atomic(references_path, after)
        state_writes += 1
    elif observed != after or observed_sha != after_sha:
        raise RunnerReleaseReferenceError("prepared reference ledger changed")
    receipt = dict(receipt)
    receipt["status"] = "applied"
    _write_json_atomic(receipt_path, receipt)
    state_writes += 1
    return _result(
        receipt_path, before_sha, after_sha, "applied", state_writes=state_writes
    )


def rollback_release_references(
    *,
    references_path: Path,
    receipt_directory: Path,
    current_link: Path,
    release_commit: str,
    trusted_owner_uid: int = 0,
    now: datetime | None = None,
) -> dict[str, object]:
    """Restore the exact pre-release ledger and write a separate immutable receipt."""
    _validate_commit(release_commit, "release_commit")
    _protected_directory(receipt_directory, trusted_owner_uid)
    apply_path = receipt_directory / f"{release_commit}.apply.json"
    apply_receipt = _read_protected_json(apply_path, trusted_owner_uid)
    _validate_apply_receipt(apply_receipt, release_commit)
    previous = apply_receipt["previous_release_commit"]
    assert isinstance(previous, str)
    if _current_commit(current_link) != previous:
        raise RunnerReleaseReferenceError(
            "Runner current does not match immediate rollback commit"
        )
    before = apply_receipt["before_references"]
    after = apply_receipt["after_references"]
    assert isinstance(before, dict) and isinstance(after, dict)
    before_sha = apply_receipt["before_sha256"]
    after_sha = apply_receipt["after_sha256"]
    assert isinstance(before_sha, str) and isinstance(after_sha, str)
    rollback_path = receipt_directory / f"{release_commit}.rollback.json"
    rollback_receipt: dict[str, object] = {
        "schema_version": 1,
        "kind": "runner_reclamation_reference_change_receipt",
        "operation": "rollback",
        "status": "rolled_back",
        "release_commit": release_commit,
        "restored_release_commit": previous,
        "before_sha256": after_sha,
        "after_sha256": before_sha,
        "apply_receipt": str(apply_path),
        "recorded_at": _utc_timestamp(now),
    }
    if rollback_path.exists() or rollback_path.is_symlink():
        existing = _read_protected_json(rollback_path, trusted_owner_uid)
        _validate_rollback_receipt(existing, release_commit)
        comparable_existing = dict(existing)
        comparable_expected = dict(rollback_receipt)
        comparable_existing.pop("recorded_at")
        comparable_expected.pop("recorded_at")
        if comparable_existing != comparable_expected or _payload_sha256(
            _read_protected_json(references_path, trusted_owner_uid)
        ) != before_sha:
            raise RunnerReleaseReferenceError("reference rollback receipt conflicts")
        return _result(
            rollback_path, after_sha, before_sha, "rolled_back", state_writes=0
        )
    observed = _read_protected_json(references_path, trusted_owner_uid)
    observed_sha = _payload_sha256(observed)
    state_writes = 0
    if observed == after and observed_sha == after_sha:
        _write_json_atomic(references_path, before)
        state_writes += 1
    elif observed != before or observed_sha != before_sha:
        raise RunnerReleaseReferenceError("reference ledger changed after release")
    _write_json_exclusive(rollback_path, rollback_receipt)
    state_writes += 1
    return _result(
        rollback_path,
        after_sha,
        before_sha,
        "rolled_back",
        state_writes=state_writes,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runner-release-references")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--release-commit", required=True)
    prepare.add_argument("--previous-release-commit", required=True)
    commit = commands.add_parser("commit")
    commit.add_argument("--release-commit", required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--release-commit", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if os.geteuid() != 0:
        print("Runner release reference update requires root", file=sys.stderr)
        return 1
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            payload = prepare_release_references(
                references_path=REFERENCES_PATH,
                receipt_directory=RECEIPT_DIRECTORY,
                runner_config=RUNNER_CONFIG,
                current_link=CURRENT_LINK,
                release_commit=args.release_commit,
                previous_release_commit=args.previous_release_commit,
            )
        elif args.command == "commit":
            payload = commit_prepared_release_references(
                references_path=REFERENCES_PATH,
                receipt_directory=RECEIPT_DIRECTORY,
                current_link=CURRENT_LINK,
                release_commit=args.release_commit,
            )
        else:
            payload = rollback_release_references(
                references_path=REFERENCES_PATH,
                receipt_directory=RECEIPT_DIRECTORY,
                current_link=CURRENT_LINK,
                release_commit=args.release_commit,
            )
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        RunnerReleaseReferenceError,
    ) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def _configured_image(path: Path, owner_uid: int) -> str:
    payload = _read_protected_json(path, owner_uid)
    runtime = payload.get("docker_runtime")
    image = runtime.get("image") if isinstance(runtime, dict) else None
    if not isinstance(image, str) or _IMAGE_REF_RE.fullmatch(image) is None:
        raise RunnerReleaseReferenceError("configured image is not an exact digest")
    return image


def _validate_existing_references(payload: dict[str, object]) -> None:
    version = payload.get("schema_version")
    common = {
        "schema_version",
        "kind",
        "current_release_commit",
        "protected_release_commits",
        "protected_image_refs",
    }
    expected = (
        common
        if version == 1
        else common | {"immediate_rollback_release_commit", "current_image_ref"}
    )
    commits = payload.get("protected_release_commits")
    images = payload.get("protected_image_refs")
    if (
        version not in {1, 2}
        or set(payload) != expected
        or payload.get("kind") != "runner_reclamation_rollback_references"
        or not isinstance(commits, list)
        or not isinstance(images, list)
        or not commits
        or not images
        or not all(isinstance(value, str) and _COMMIT_RE.fullmatch(value) for value in commits)
        or not all(isinstance(value, str) and _IMAGE_REF_RE.fullmatch(value) for value in images)
        or not isinstance(payload.get("current_release_commit"), str)
        or _COMMIT_RE.fullmatch(str(payload.get("current_release_commit"))) is None
    ):
        raise RunnerReleaseReferenceError("existing reference ledger is invalid")
    if len(commits) != len(set(commits)) or len(images) != len(set(images)):
        raise RunnerReleaseReferenceError("existing reference ledger contains duplicates")
    if version == 2:
        rollback = payload.get("immediate_rollback_release_commit")
        current_image = payload.get("current_image_ref")
        if (
            not isinstance(rollback, str)
            or _COMMIT_RE.fullmatch(rollback) is None
            or rollback not in commits
            or not isinstance(current_image, str)
            or _IMAGE_REF_RE.fullmatch(current_image) is None
            or current_image not in images
        ):
            raise RunnerReleaseReferenceError("existing reference ledger v2 is invalid")


def _validate_apply_receipt(payload: dict[str, object], release_commit: str) -> None:
    expected = {
        "schema_version",
        "kind",
        "operation",
        "status",
        "release_commit",
        "previous_release_commit",
        "current_image_ref",
        "before_sha256",
        "after_sha256",
        "before_references",
        "after_references",
        "recorded_at",
    }
    before = payload.get("before_references")
    after = payload.get("after_references")
    if (
        set(payload) != expected
        or payload.get("schema_version") != 1
        or payload.get("kind") != "runner_reclamation_reference_change_receipt"
        or payload.get("operation") != "apply"
        or payload.get("status") not in {"prepared", "applied"}
        or payload.get("release_commit") != release_commit
        or not isinstance(payload.get("previous_release_commit"), str)
        or _COMMIT_RE.fullmatch(str(payload.get("previous_release_commit"))) is None
        or not isinstance(payload.get("current_image_ref"), str)
        or _IMAGE_REF_RE.fullmatch(str(payload.get("current_image_ref"))) is None
        or not isinstance(payload.get("recorded_at"), str)
        or not isinstance(before, dict)
        or not isinstance(after, dict)
        or payload.get("before_sha256") != _payload_sha256(before)
        or payload.get("after_sha256") != _payload_sha256(after)
    ):
        raise RunnerReleaseReferenceError("reference apply receipt is invalid")
    _validate_existing_references(before)
    _validate_existing_references(after)


def _validate_rollback_receipt(payload: dict[str, object], release_commit: str) -> None:
    expected = {
        "schema_version",
        "kind",
        "operation",
        "status",
        "release_commit",
        "restored_release_commit",
        "before_sha256",
        "after_sha256",
        "apply_receipt",
        "recorded_at",
    }
    if (
        set(payload) != expected
        or payload.get("schema_version") != 1
        or payload.get("kind") != "runner_reclamation_reference_change_receipt"
        or payload.get("operation") != "rollback"
        or payload.get("status") != "rolled_back"
        or payload.get("release_commit") != release_commit
        or not isinstance(payload.get("restored_release_commit"), str)
        or _COMMIT_RE.fullmatch(str(payload.get("restored_release_commit"))) is None
        or not isinstance(payload.get("before_sha256"), str)
        or _SHA256_RE.fullmatch(str(payload.get("before_sha256"))) is None
        or not isinstance(payload.get("after_sha256"), str)
        or _SHA256_RE.fullmatch(str(payload.get("after_sha256"))) is None
        or not isinstance(payload.get("apply_receipt"), str)
        or not isinstance(payload.get("recorded_at"), str)
    ):
        raise RunnerReleaseReferenceError("reference rollback receipt is invalid")


def _read_protected_json(path: Path, owner_uid: int) -> dict[str, object]:
    metadata = path.stat(follow_symlinks=False)
    raw = path.read_bytes()
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or not 0 < len(raw) <= 1024 * 1024
    ):
        raise RunnerReleaseReferenceError("protected reference input is unsafe")
    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(payload, dict):
        raise RunnerReleaseReferenceError("protected reference input must be an object")
    return payload


def _protected_directory(path: Path, owner_uid: int) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_mode & 0o022
    ):
        raise RunnerReleaseReferenceError("reference receipt directory is unsafe")


def _current_commit(path: Path) -> str:
    target = os.readlink(path).rstrip("/").split("/")[-1]
    _validate_commit(target, "Runner current commit")
    return target


def _validate_commit(value: str, field: str) -> None:
    if not isinstance(value, str) or _COMMIT_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be lowercase 40-hex")


def _utc_timestamp(value: datetime | None) -> str:
    moment = datetime.now(timezone.utc) if value is None else value
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("reference receipt timestamp must be timezone-aware")
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _payload_sha256(payload: dict[str, object]) -> str:
    return sha256(_canonical_bytes(payload)).hexdigest()


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    raw = _canonical_bytes(payload) + b"\n"
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
        _fsync_directory(path.parent)
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass


def _write_json_exclusive(path: Path, payload: dict[str, object]) -> None:
    raw = _canonical_bytes(payload) + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(path.parent)
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON field")
        payload[key] = value
    return payload


def _result(
    path: Path,
    before_sha256: str,
    after_sha256: str,
    status: str,
    *,
    state_writes: int,
) -> dict[str, object]:
    if _SHA256_RE.fullmatch(before_sha256) is None or _SHA256_RE.fullmatch(after_sha256) is None:
        raise AssertionError("internal reference digest is invalid")
    return {
        "ok": True,
        "status": status,
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "receipt_path": str(path),
        "state_writes": state_writes,
    }


if __name__ == "__main__":
    raise SystemExit(main())
