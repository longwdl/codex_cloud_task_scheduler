"""Administrator-only Runner recovery snapshots and exact asset reclamation CLI."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import grp
import json
import os
from pathlib import Path
import stat
import sys
from typing import Sequence
from collections.abc import Iterator

from codex_dispatcher.command_runner import run_command
from codex_dispatcher.disaster_recovery import collect_runner_recovery_snapshot
from codex_dispatcher.runner_asset_reclamation import (
    DockerImageAsset,
    RunnerAssetReclamationError,
    RunnerAssetReclamationPlan,
    apply_runner_asset_reclamation,
    collect_runner_asset_snapshot,
    delete_exact_release,
    docker_image_assets_from_json,
    plan_runner_asset_reclamation,
)
from codex_dispatcher.runner_reclamation_status import (
    build_runner_reclamation_status,
)


RUNNER_CONFIG = Path("/srv/codex-runner/etc/config.json")
RELEASES_ROOT = Path("/srv/codex-runner/releases")
CURRENT_LINK = Path("/srv/codex-runner/current")
WORK_ITEMS_ROOT = Path("/srv/codex-runner/work-items")
PROVENANCE_PATH = Path("/srv/codex-runner/etc/image-provenance.json")
ROLLBACK_REFERENCES_PATH = Path(
    "/srv/codex-runner/etc/reclamation-rollback-references.json"
)
PLAN_DIRECTORY = Path("/srv/codex-runner/reclamation-plans")
RECEIPT_DIRECTORY = Path("/srv/codex-runner/reclamation-receipts")
STATUS_DIRECTORY = Path("/srv/codex-runner/reclamation-status")
STATUS_PATH = STATUS_DIRECTORY / "latest.json"
ACTIVE_LOCK_PATH = Path("/srv/codex-runner/run/active.lock")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-runner-maintenance")
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("recovery-snapshot")
    snapshot.add_argument("--current-release-commit", required=True)
    plan = commands.add_parser("reclamation-plan")
    plan.add_argument("--write-plan", action="store_true", required=True)
    commands.add_parser("reclamation-auto-plan")
    recheck = commands.add_parser("reclamation-recheck")
    recheck.add_argument("--plan-sha256", required=True)
    apply = commands.add_parser("reclamation-apply")
    apply.add_argument("--plan-sha256", required=True)
    apply.add_argument("--apply", action="store_true", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.geteuid() != 0:
        print("Runner maintenance must run as root", file=sys.stderr)
        return 1
    try:
        with _runner_active_lock():
            if args.command == "recovery-snapshot":
                current = os.readlink(CURRENT_LINK).rstrip("/").split("/")[-1]
                if current != args.current_release_commit:
                    raise RunnerAssetReclamationError(
                        "requested recovery commit differs from Runner current"
                    )
                owner_uid = WORK_ITEMS_ROOT.stat(follow_symlinks=False).st_uid
                if owner_uid != ACTIVE_LOCK_PATH.stat(
                    follow_symlinks=False
                ).st_uid:
                    raise RunnerAssetReclamationError(
                        "Runner evidence and active lock owners differ"
                    )
                payload = collect_runner_recovery_snapshot(
                    work_items_root=WORK_ITEMS_ROOT,
                    current_release_commit=args.current_release_commit,
                    trusted_owner_uid=owner_uid,
                )
            else:
                payload = _run_reclamation(args)
            _emit(payload)
            return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        _emit({"ok": False, "error": str(exc)})
        return 1


def _run_reclamation(args: argparse.Namespace) -> dict[str, object]:
    rollback_commits, rollback_images = _rollback_references()
    work_items_owner_uid = WORK_ITEMS_ROOT.stat(follow_symlinks=False).st_uid
    active_lock_owner_uid = ACTIVE_LOCK_PATH.stat(
        follow_symlinks=False
    ).st_uid
    if work_items_owner_uid != active_lock_owner_uid:
        raise RunnerAssetReclamationError(
            "Runner evidence and active lock owners differ"
        )
    inspector = _DockerInspector(RUNNER_CONFIG, PROVENANCE_PATH)
    snapshot = collect_runner_asset_snapshot(
        config_path=RUNNER_CONFIG,
        releases_root=RELEASES_ROOT,
        current_link=CURRENT_LINK,
        rollback_release_commits=rollback_commits,
        rollback_image_refs=rollback_images,
        images=inspector.inspect(),
        trusted_work_items_owner_uid=work_items_owner_uid,
    )
    plan = plan_runner_asset_reclamation(snapshot)
    if args.command == "reclamation-auto-plan":
        filesystem = os.statvfs(RELEASES_ROOT)
        available = filesystem.f_bavail * filesystem.f_frsize
        status = build_runner_reclamation_status(
            host_available_bytes=available,
            release_count=len(snapshot.releases),
            release_target_count=len(plan.release_targets),
            image_target_count=len(plan.image_targets),
            expected_total_bytes=plan.expected_total_bytes,
            plan_sha256=plan.plan_sha256,
        )
        plan_path: Path | None = None
        inventory_path: Path | None = None
        if status.trigger_reasons:
            plan_path, inventory_path = _store_plan(plan, snapshot.to_mapping())
        _protected_directory(STATUS_DIRECTORY)
        status_gid = grp.getgrnam("codex-runner").gr_gid
        if STATUS_DIRECTORY.stat(follow_symlinks=False).st_gid != status_gid:
            raise RunnerAssetReclamationError("reclamation status group is invalid")
        _write_json_atomic(
            STATUS_PATH,
            status.to_mapping(),
            mode=0o640,
            group_id=status_gid,
        )
        payload = status.to_mapping()
        payload.update(
            ok=True,
            status_path=str(STATUS_PATH),
            plan_path=str(plan_path) if plan_path is not None else None,
            reference_inventory_path=(
                str(inventory_path) if inventory_path is not None else None
            ),
            authorizes_apply=False,
            state_writes=3 if status.trigger_reasons else 1,
        )
        return payload
    if args.command == "reclamation-plan":
        path, inventory_path = _store_plan(plan, snapshot.to_mapping())
        inventory = snapshot.to_mapping()
        payload = plan.to_mapping()
        payload["plan_path"] = str(path)
        payload["reference_inventory_path"] = str(inventory_path)
        payload["reference_inventory"] = inventory
        payload["state_writes"] = 1
        return payload
    approved = _load_plan(args.plan_sha256)
    if approved != plan:
        raise RunnerAssetReclamationError("Runner assets changed after stored plan")
    if args.command == "reclamation-recheck":
        payload = plan.to_mapping()
        payload.update(
            reinspection_matches=True,
            reference_inventory=snapshot.to_mapping(),
            state_writes=0,
            requires_separate_apply=True,
        )
        return payload
    return apply_runner_asset_reclamation(
        approved_plan=approved,
        reinspected_snapshot=snapshot,
        receipt_directory=RECEIPT_DIRECTORY,
        delete_release=delete_exact_release,
        delete_image=inspector.delete,
    )


class _DockerInspector:
    def __init__(self, config_path: Path, provenance_path: Path) -> None:
        self._config = _read_json(config_path)
        runtime = self._config.get("docker_runtime")
        if not isinstance(runtime, dict):
            raise RunnerAssetReclamationError("Runner Docker runtime is unavailable")
        docker_path = runtime.get("docker_path")
        self._docker_host = runtime.get("docker_host")
        docker_config = runtime.get("cli_config_directory")
        docker_host_path = None
        if isinstance(self._docker_host, str) and self._docker_host.startswith("unix:///"):
            docker_host_path = Path(self._docker_host.removeprefix("unix://"))
        if not all(
            isinstance(value, str) and Path(value).is_absolute()
            for value in (docker_path, docker_config)
        ) or docker_host_path is None or ".." in docker_host_path.parts:
            raise RunnerAssetReclamationError("Runner Docker command boundary is invalid")
        assert isinstance(docker_path, str) and isinstance(docker_config, str)
        self._docker_path = docker_path
        self._docker_config = docker_config
        _protected_executable(Path(docker_path))
        docker_uid = _protected_docker_socket(docker_host_path)
        _protected_runtime_directory(Path(docker_config), docker_uid)
        self._environment = {
            "HOME": "/var/lib/codex-runner/home",
            "XDG_RUNTIME_DIR": str(Path(self._docker_host.removeprefix("unix://")).parent),
            "DOCKER_HOST": self._docker_host,
            "DOCKER_CONFIG": self._docker_config,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
        provenance_payload = _read_json(provenance_path)
        rows = provenance_payload.get("images")
        if (
            provenance_payload.get("schema_version") != 1
            or provenance_payload.get("kind") != "runner_image_provenance"
            or not isinstance(rows, list)
        ):
            raise RunnerAssetReclamationError("Runner image provenance is invalid")
        self._provenance: dict[str, tuple[str, str]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise RunnerAssetReclamationError("Runner image provenance row is invalid")
            identity = row.get("identity")
            commit = row.get("source_commit")
            kind = row.get("provenance_kind")
            if not all(isinstance(value, str) for value in (identity, commit, kind)):
                raise RunnerAssetReclamationError("Runner image provenance row is incomplete")
            assert (
                isinstance(identity, str)
                and isinstance(commit, str)
                and isinstance(kind, str)
            )
            if (
                not (
                    identity.startswith("sha256:")
                    or "@sha256:" in identity
                )
                or len(commit) != 40
                or any(character not in "0123456789abcdef" for character in commit)
                or kind not in {"publication_run", "tree_equivalent"}
            ):
                raise RunnerAssetReclamationError("Runner image provenance row is invalid")
            if identity in self._provenance:
                raise RunnerAssetReclamationError("Runner image provenance is duplicated")
            self._provenance[identity] = (commit, kind)

    def inspect(self) -> tuple[DockerImageAsset, ...]:
        listed = self._run("image", "ls", "-a", "--no-trunc", "--format", "{{.ID}}")
        image_ids = tuple(
            sorted({line.strip() for line in listed.splitlines() if line.strip()})
        )
        inspections: list[dict[str, object]] = []
        for image_id in image_ids:
            payload = json.loads(
                self._run("image", "inspect", image_id, "--format", "{{json .}}")
            )
            if not isinstance(payload, dict):
                raise RunnerAssetReclamationError("Docker image inspection is invalid")
            inspections.append(payload)
        usage = json.loads(
            self._run("system", "df", "-v", "--format", "{{json .}}")
        )
        if not isinstance(usage, dict):
            raise RunnerAssetReclamationError("Docker disk usage is invalid")
        return docker_image_assets_from_json(
            inspections=inspections,
            disk_usage=usage,
            provenance=self._provenance,
        )

    def delete(self, target: DockerImageAsset) -> int:
        self._run("image", "rm", target.image_id)
        result = run_command(
            (self._docker_path, "image", "inspect", target.image_id),
            timeout_seconds=30,
            max_output_bytes=4096,
            env=self._environment,
        )
        if result.timed_out or result.returncode == 0:
            raise RunnerAssetReclamationError("Docker image still exists after exact delete")
        return target.unique_size_bytes

    def _run(self, *arguments: str) -> str:
        result = run_command(
            (self._docker_path, *arguments),
            timeout_seconds=60,
            max_output_bytes=1024 * 1024,
            env=self._environment,
        )
        if (
            result.returncode != 0
            or result.timed_out
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise RunnerAssetReclamationError("Docker maintenance command failed")
        return result.stdout.strip()


def _rollback_references() -> tuple[tuple[str, ...], tuple[str, ...]]:
    payload = _read_json(ROLLBACK_REFERENCES_PATH)
    commits = payload.get("protected_release_commits")
    images = payload.get("protected_image_refs")
    current = payload.get("current_release_commit")
    immediate = payload.get("immediate_rollback_release_commit")
    current_image = payload.get("current_image_ref")
    if (
        set(payload)
        != {
            "schema_version",
            "kind",
            "current_release_commit",
            "immediate_rollback_release_commit",
            "current_image_ref",
            "protected_release_commits",
            "protected_image_refs",
        }
        or payload.get("schema_version") != 2
        or payload.get("kind") != "runner_reclamation_rollback_references"
        or not isinstance(commits, list)
        or not isinstance(images, list)
        or not all(isinstance(value, str) for value in commits)
        or not all(isinstance(value, str) for value in images)
        or not isinstance(current, str)
        or not isinstance(immediate, str)
        or not isinstance(current_image, str)
        or current not in commits
        or immediate not in commits
        or current_image not in images
        or current == immediate
        or len(commits) != 2
        or len(images) != 1
        or os.readlink(CURRENT_LINK).rstrip("/").split("/")[-1] != current
    ):
        raise RunnerAssetReclamationError("Runner rollback references are stale or invalid")
    return (
        tuple(value for value in commits if value != current),
        tuple(images),
    )


def _store_plan(
    plan: RunnerAssetReclamationPlan,
    inventory: dict[str, object],
) -> tuple[Path, Path]:
    _protected_directory(PLAN_DIRECTORY)
    path = PLAN_DIRECTORY / f"{plan.plan_sha256}.json"
    if path.exists() or path.is_symlink():
        existing = RunnerAssetReclamationPlan.from_mapping(_read_json(path))
        if existing != plan:
            raise RunnerAssetReclamationError("stored plan identity conflicts")
    else:
        _write_json_atomic(path, plan.to_mapping())
    inventory_path = PLAN_DIRECTORY / f"inventory-{plan.snapshot_sha256}.json"
    if inventory_path.exists() or inventory_path.is_symlink():
        if _read_json(inventory_path) != inventory:
            raise RunnerAssetReclamationError(
                "stored reference inventory identity conflicts"
            )
    else:
        _write_json_atomic(inventory_path, inventory)
    return path, inventory_path


def _load_plan(plan_sha256: str) -> RunnerAssetReclamationPlan:
    if (
        len(plan_sha256) != 64
        or any(character not in "0123456789abcdef" for character in plan_sha256)
    ):
        raise ValueError("plan_sha256 must be lowercase SHA-256")
    path = PLAN_DIRECTORY / f"{plan_sha256}.json"
    plan = RunnerAssetReclamationPlan.from_mapping(_read_json(path))
    if plan.plan_sha256 != plan_sha256:
        raise RunnerAssetReclamationError("stored plan filename conflicts")
    return plan


def _read_json(path: Path) -> dict[str, object]:
    try:
        metadata = path.stat(follow_symlinks=False)
        raw = path.read_bytes()
    except OSError as exc:
        raise RunnerAssetReclamationError("protected maintenance input is unavailable") from exc
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or not 0 < len(raw) <= 1024 * 1024
    ):
        raise RunnerAssetReclamationError("protected maintenance input is unsafe")
    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(payload, dict):
        raise RunnerAssetReclamationError("protected maintenance input must be an object")
    return payload


def _protected_directory(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if path.is_symlink() or not path.is_dir() or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise RunnerAssetReclamationError("maintenance output directory is unsafe")


def _protected_executable(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & 0o022
        or not metadata.st_mode & 0o111
    ):
        raise RunnerAssetReclamationError("Docker executable is unsafe")


def _protected_docker_socket(path: Path) -> int:
    metadata = path.stat(follow_symlinks=False)
    if (
        path.is_symlink()
        or not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_mode & 0o007
    ):
        raise RunnerAssetReclamationError("Docker socket is unsafe")
    return metadata.st_uid


def _protected_runtime_directory(path: Path, expected_uid: int) -> None:
    metadata = path.stat(follow_symlinks=False)
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or metadata.st_mode & 0o077
    ):
        raise RunnerAssetReclamationError("Docker CLI directory is unsafe")


@contextmanager
def _runner_active_lock() -> Iterator[None]:
    config = _read_json(RUNNER_CONFIG)
    value = config.get("active_lock_path")
    if value != str(ACTIVE_LOCK_PATH):
        raise RunnerAssetReclamationError("Runner active lock path is invalid")
    path = Path(value)
    metadata = path.stat(follow_symlinks=False)
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
    ):
        raise RunnerAssetReclamationError("Runner active lock is unsafe")
    with path.open("rb", buffering=0) as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunnerAssetReclamationError("Runner has an active operation") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON field")
        payload[key] = value
    return payload


def _write_json_atomic(
    path: Path,
    payload: dict[str, object],
    *,
    mode: int = 0o600,
    group_id: int | None = None,
) -> None:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
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


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    raise SystemExit(main())
