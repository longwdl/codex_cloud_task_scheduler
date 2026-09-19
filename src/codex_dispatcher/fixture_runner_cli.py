"""Operator-only, fail-closed Fixture Runner fault and evidence utility.

This module deliberately has no scheduler, GitHub, Slack, or database access.
It is invoked on the Runner as ``codex-runner`` and only accepts the two
dedicated Fixture repositories.  Its JSON output is metadata-only.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import stat
import time
from pathlib import Path
from typing import Sequence

from codex_dispatcher.command_runner import run_command
from codex_dispatcher.executors.codex_docker import (
    DOCKER_NETWORK,
    DOCKER_LABEL_POLICY_DIGEST,
    DOCKER_LABEL_SESSION_GENERATION,
    DOCKER_LABEL_SESSION_GENERATION_ID,
    DOCKER_LABEL_TURN,
    DOCKER_LABEL_WORK_ITEM,
)
from codex_dispatcher.runner_main import (
    DEFAULT_RUNNER_CONFIG,
    RunnerConfiguration,
    load_runner_configuration,
)
from codex_dispatcher.runner_docker import _read_generation_record, _read_session_binding, _read_tool_binding
from codex_dispatcher.runner_protocol import NEXT_PROTOCOL_VERSION, RunnerOperation, parse_runner_request
from codex_dispatcher.runner_workspace import RunnerWorkspace
from codex_dispatcher.runner_disk import FusedWorkItemDisk
from codex_dispatcher.work_items import (
    validate_git_sha,
    validate_session_generation_id,
    validate_sha256,
    validate_turn_id,
    validate_work_item_id,
)


FIXTURE_REPOSITORIES = frozenset(
    {"longwdl/codex-dispatcher-fixture", "longwdl/codex-dispatcher-fixture-2"}
)
FAULT_ENV = "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS"
OPERATOR_ROOT = Path("/srv/codex-runner/fixture-operator-evidence")
_MAX_JSON_BYTES = 64 * 1024
_MAX_PATHS = 32
_MAX_GIT_BYTES = 2 * 1024 * 1024


class FixtureRunnerError(RuntimeError):
    """A condition that must be reported without exposing runtime contents."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m codex_dispatcher.fixture_runner_cli")
    parser.add_argument("--config", type=Path, default=DEFAULT_RUNNER_CONFIG)
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan-stop", "stop", "status"):
        item = commands.add_parser(name)
        _identity_arguments(item, turn_required=True)
        if name == "stop":
            item.add_argument("--apply", action="store_true", required=True)
    diagnose = commands.add_parser("diagnose")
    _diagnostic_arguments(diagnose)
    return parser


def _identity_arguments(parser: argparse.ArgumentParser, *, turn_required: bool) -> None:
    parser.add_argument("--repository", required=True, choices=tuple(sorted(FIXTURE_REPOSITORIES)))
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--work-item-id", required=True)
    if turn_required:
        parser.add_argument("--turn-id", required=True)
        parser.add_argument("--session-generation-id", required=True)
        parser.add_argument("--generation", type=int, required=True)
        parser.add_argument("--policy-digest", required=True)
    parser.add_argument("--expected-head", required=True)


def _diagnostic_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True, choices=tuple(sorted(FIXTURE_REPOSITORIES)))
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--work-item-id", required=True)
    parser.add_argument("--expected-head", required=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _require_runner_user()
        config = load_runner_configuration(args.config)
        if args.command in {"plan-stop", "stop", "status"}:
            result = _stop_plan(config, args)
            if args.command == "stop" and result["reason"] == "stop_ready":
                result = _apply_stop(config, args, result)
            elif args.command == "stop" and result["reason"] != "already_stopped":
                raise FixtureRunnerError(str(result["reason"]))
        else:
            result = _diagnose(config, args)
        result["command"] = args.command
        result["ok"] = result["reason"] != "stop_intent_ambiguous"
        _emit(result)
        return 0 if result["ok"] else 1
    except FixtureRunnerError as exc:
        _emit({"ok": False, "reason": str(exc)})
        return 1
    except (OSError, ValueError, RuntimeError, TypeError):
        _emit({"ok": False, "reason": "operator_inspection_failed"})
        return 1


def _stop_plan(config: RunnerConfiguration, args: argparse.Namespace) -> dict[str, object]:
    identity = _validate_identity(args)
    if args.command != "status":
        _require_fault_enablement(identity["repository"])
    prior = _stop_receipt_state(identity)
    if prior is not None:
        return prior
    snapshot = _load_snapshot(config, identity, require_turn=True)
    container = _exact_container(config, identity)
    return {
        "command": args.command,
        "reason": "stop_ready" if container["running"] else "container_not_running",
        "identity": identity,
        "container_id": container["id"],
        "container_running": container["running"],
        "workspace_head": snapshot["head"],
        "receipt_state": snapshot["receipt_state"],
        "state_writes": 0,
    }


def _apply_stop(
    config: RunnerConfiguration, args: argparse.Namespace, plan: dict[str, object]
) -> dict[str, object]:
    if plan["reason"] == "already_stopped":
        return plan
    if plan["reason"] != "stop_ready":
        raise FixtureRunnerError("stop_not_authorized_by_current_plan")
    identity = plan["identity"]
    assert isinstance(identity, dict)
    receipt_dir = _receipt_directory(identity, "stops")
    _prepare_operator_root(receipt_dir)
    request_sha = _canonical_sha(identity)
    key = _stop_request_sha(identity)
    receipt = receipt_dir / f"{key}.json"
    intent = receipt_dir / f"{key}.intent.json"
    existing = _read_existing_receipt(receipt, identity)
    if existing is not None:
        return existing
    _create_intent(intent, identity, str(plan["container_id"]))
    # Revalidate immediately before the only external effect.  An existing intent
    # intentionally remains ambiguous; this tool never retries a stop.
    _load_snapshot(config, identity, require_turn=True)
    fresh = _exact_container(config, identity)
    if fresh["id"] != plan["container_id"] or not fresh["running"]:
        raise FixtureRunnerError("stop_intent_ambiguous")
    runtime = config.docker_runtime
    if runtime is None:
        raise FixtureRunnerError("docker_runtime_unavailable")
    result = run_command(
        (str(runtime.docker_path), f"--host={runtime.docker_host}", "container", "stop", "--time", "10", str(fresh["id"])),
        timeout_seconds=15.0,
        max_output_bytes=4096,
        env={"DOCKER_CONFIG": str(runtime.cli_config_directory)},
    )
    if result.timed_out or result.error is not None or result.returncode != 0:
        raise FixtureRunnerError("stop_intent_ambiguous")
    if not _stop_effect_is_proven(config, identity, str(fresh["id"])):
        raise FixtureRunnerError("stop_intent_ambiguous")
    payload: dict[str, object] = {
        "version": 1, "state": "stopped", "request_sha256": request_sha,
        "container_id": fresh["id"], "identity": identity,
        "dedup_identity": _stop_dedup_identity(identity),
    }
    _write_exclusive_json(receipt, payload)
    return {"command": "stop", "reason": "stopped", "receipt": str(receipt), "state_writes": 2, **payload}


def _diagnose(config: RunnerConfiguration, args: argparse.Namespace) -> dict[str, object]:
    identity = _validate_diagnostic_identity(args)
    with _nonblocking_active_lock(config):
        archive = _archive_status(config, identity)
        if archive != "none":
            containers = _containers_for_work_item(config, str(identity["work_item_id"]))
            return {"command": "diagnose", "reason": "archive_receipt_observed", "identity": identity,
                    "archive_status": archive, "active_container_count": len(containers),
                    "cleanup_verified": archive in {"archived", "absent"} and not containers
                    and _storage_absent(config, identity, archive=archive),
                    "state_writes": 0}
        snapshot = _load_snapshot(config, identity, require_turn=False)
        containers = _containers_for_work_item(config, str(identity["work_item_id"]))
        if containers:
            return {"command": "diagnose", "reason": "active_container", "identity": identity,
                    "head": snapshot["head"], "active_container_count": len(containers),
                    "archive_status": archive, "state_writes": 0}
        status = _git_status(config, snapshot["repository"])
        return {
            "command": "diagnose", "reason": "active_container" if containers else ("clean" if not status else "dirty"),
            "identity": identity, "head": snapshot["head"],
            "tracked_paths": _bounded_paths(status, tracked=True),
            "untracked_paths": _bounded_paths(status, tracked=False),
            "path_count": len(status), "paths_truncated": len(status) > _MAX_PATHS,
            "changes": status[:_MAX_PATHS],
            "receipt_state": snapshot["receipt_state"], "state_writes": 0,
            "archive_status": archive, "active_container_count": len(containers),
            "active_container": any(bool(row["running"]) for row in containers),
        }


def _validate_identity(args: argparse.Namespace) -> dict[str, object]:
    if args.repository not in FIXTURE_REPOSITORIES or args.issue <= 0 or args.generation <= 0:
        raise FixtureRunnerError("identity_invalid")
    return {"repository": args.repository, "issue": args.issue,
            "work_item_id": validate_work_item_id(args.work_item_id),
            "turn_id": validate_turn_id(args.turn_id),
            "session_generation_id": validate_session_generation_id(args.session_generation_id),
            "generation": args.generation, "policy_digest": validate_sha256(args.policy_digest, "policy_digest"),
            "expected_head": validate_git_sha(args.expected_head, "expected_head")}


def _validate_diagnostic_identity(args: argparse.Namespace) -> dict[str, object]:
    if args.repository not in FIXTURE_REPOSITORIES or args.issue <= 0:
        raise FixtureRunnerError("identity_invalid")
    return {"repository": args.repository, "issue": args.issue,
            "work_item_id": validate_work_item_id(args.work_item_id),
            "expected_head": validate_git_sha(args.expected_head, "expected_head")}


def _require_fault_enablement(repository: object) -> None:
    if repository not in FIXTURE_REPOSITORIES or os.environ.get(FAULT_ENV) != repository:
        raise FixtureRunnerError("fixture_fault_enablement_missing")


def _load_snapshot(config: RunnerConfiguration, identity: dict[str, object], *, require_turn: bool) -> dict[str, object]:
    root = config.work_items_root
    _safe_directory(root, "work_items_root")
    registry = root / ".registry" / f"{identity['work_item_id']}.json"
    _read_json(registry)
    reader = _workspace_reader(config)
    record = reader._read_registry(str(identity["work_item_id"]), required=True).to_mapping()
    if (record.get("work_item_id"), record.get("repository"), record.get("issue_number")) != (identity["work_item_id"], identity["repository"], identity["issue"]):
        raise FixtureRunnerError("workspace_registry_identity_mismatch")
    workspace = root / str(identity["repository"]).replace("/", "__") / f"issue-{identity['issue']}"
    repository = workspace / "repo"; state = workspace / "runner-state"
    for path, name in ((workspace, "workspace"), (repository, "repository"), (state, "state")):
        _safe_directory(path, name)
    if getattr(config, "work_item_disk", None) is not None:
        disk = FusedWorkItemDisk(config.work_item_disk, work_items_root=root)
        image = disk._image_path(str(identity["work_item_id"]))
        disk._validate_image(image)
        mount = disk._mount_record(workspace)
        if mount is None:
            raise FixtureRunnerError("workspace_not_mounted")
        disk._validate_mount_record(mount, image=image, mountpoint=workspace)
    workspace_record = _read_json(state / "workspace.json")
    if workspace_record != record:
        raise FixtureRunnerError("workspace_metadata_mismatch")
    branch = _git(config, repository, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if branch != record["task_branch"]:
        raise FixtureRunnerError("workspace_branch_mismatch")
    head = _git_head(config, repository)
    if head != identity["expected_head"]:
        raise FixtureRunnerError("workspace_head_mismatch")
    receipt_state = "none"
    if require_turn:
        if config.policy_bundle is None or config.policy_bundle.policy_digest != identity["policy_digest"]:
            raise FixtureRunnerError("configured_policy_digest_mismatch")
        generation = state / "generations" / str(identity["session_generation_id"])
        _safe_directory(generation, "generation")
        runtime = config.docker_runtime
        if runtime is None:
            raise FixtureRunnerError("docker_runtime_unavailable")
        try:
            for filename in ("generation.json", "codex-session.json", "codex-session-tools.json"):
                _safe_regular(generation / filename, "session_receipt")
            generation_receipt = _read_generation_record(generation / "generation.json")
        except (OSError, ValueError, RuntimeError) as exc:
            raise FixtureRunnerError("generation_receipt_invalid") from exc
        expected = (identity["work_item_id"], identity["session_generation_id"], identity["generation"], identity["policy_digest"], runtime.image, runtime.codex_sha256, runtime.code_mode_host_sha256)
        if generation_receipt != expected:
            raise FixtureRunnerError("generation_identity_mismatch")
        try:
            session = _read_session_binding(generation / "codex-session.json")
            tools = _read_tool_binding(generation / "codex-session-tools.json")
        except (OSError, ValueError, RuntimeError) as exc:
            raise FixtureRunnerError("session_receipt_invalid") from exc
        if session is None or tools is None or session != (expected[0], session[1], expected[4], expected[5]) or tools != (*session, expected[6]):
            raise FixtureRunnerError("session_receipt_invalid")
        try:
            turn_payload = _read_json(state / "turns" / f"{identity['turn_id']}.json")
        except FixtureRunnerError as exc:
            raise FixtureRunnerError("turn_receipt_invalid") from exc
        if set(turn_payload) != {"version", "request", "state", "reply"} or turn_payload.get("version") != 1:
            raise FixtureRunnerError("turn_receipt_invalid")
        if turn_payload.get("state") != "executing" or turn_payload.get("reply") is not None or not isinstance(turn_payload.get("request"), dict):
            raise FixtureRunnerError("turn_receipt_invalid")
        try:
            request = parse_runner_request(json.dumps(turn_payload["request"], sort_keys=True, separators=(",", ":")))
        except (TypeError, ValueError) as exc:
            raise FixtureRunnerError("turn_receipt_invalid") from exc
        if (request.input_head_sha != identity["expected_head"]
                or (request.operation is RunnerOperation.RESUME and request.session_id != session[1])
                or request.version != NEXT_PROTOCOL_VERSION or request.operation not in {RunnerOperation.START, RunnerOperation.RESUME}
                or request.work_item_id != identity["work_item_id"] or request.turn_id != identity["turn_id"]
                or request.session_generation_id != identity["session_generation_id"]
                or request.session_generation != identity["generation"]
                or request.agent_policy_digest != identity["policy_digest"]):
            raise FixtureRunnerError("turn_receipt_invalid")
        receipt_state = "bound"
    return {"repository": repository, "state": state, "head": head, "receipt_state": receipt_state}


def _exact_container(config: RunnerConfiguration, identity: dict[str, object]) -> dict[str, object]:
    matches = _containers_for_work_item(config, str(identity["work_item_id"]))
    expected = {DOCKER_LABEL_WORK_ITEM: identity["work_item_id"], DOCKER_LABEL_TURN: identity["turn_id"],
                DOCKER_LABEL_SESSION_GENERATION_ID: identity["session_generation_id"],
                DOCKER_LABEL_SESSION_GENERATION: str(identity["generation"]), DOCKER_LABEL_POLICY_DIGEST: identity["policy_digest"]}
    exact = [row for row in matches if row["name"] == f"/codex-{identity['turn_id']}" and all(row["labels"].get(key) == value for key, value in expected.items())]
    if len(exact) != 1 or len(matches) != 1:
        raise FixtureRunnerError("container_identity_ambiguous")
    return exact[0]


def _containers_for_work_item(config: RunnerConfiguration, work_item_id: str) -> list[dict[str, object]]:
    runtime = config.docker_runtime
    if runtime is None:
        raise FixtureRunnerError("docker_runtime_unavailable")
    result = run_command((str(runtime.docker_path), f"--host={runtime.docker_host}", "container", "ls", "-a", "--filter", f"label={DOCKER_LABEL_WORK_ITEM}={work_item_id}", "--format", "{{.ID}}"), timeout_seconds=10, max_output_bytes=4096, env={"DOCKER_CONFIG": str(runtime.cli_config_directory)})
    if result.timed_out or result.error is not None or result.returncode != 0 or result.stderr or result.stdout_truncated:
        raise FixtureRunnerError("docker_inspection_unavailable")
    rows = []
    ids = tuple(line for line in result.stdout.splitlines() if line)
    if len(ids) > 1:
        raise FixtureRunnerError("container_identity_ambiguous")
    for container_id in ids:
        if not 12 <= len(container_id) <= 64 or any(c not in "0123456789abcdef" for c in container_id):
            raise FixtureRunnerError("docker_container_id_invalid")
        inspected = run_command((str(runtime.docker_path), f"--host={runtime.docker_host}", "container", "inspect", container_id, "--format={{json .}}"), timeout_seconds=10, max_output_bytes=_MAX_JSON_BYTES, env={"DOCKER_CONFIG": str(runtime.cli_config_directory)})
        if inspected.timed_out or inspected.error is not None or inspected.returncode != 0 or inspected.stderr or inspected.stdout_truncated or inspected.stderr_truncated:
            raise FixtureRunnerError("docker_inspection_unavailable")
        payload = _json_bytes(inspected.stdout.encode())
        cfg, state, host, networks = payload.get("Config"), payload.get("State"), payload.get("HostConfig"), payload.get("NetworkSettings")
        labels = cfg.get("Labels") if isinstance(cfg, dict) else None
        full_id = payload.get("Id")
        if (not isinstance(labels, dict) or not isinstance(state, dict) or not isinstance(full_id, str)
                or len(full_id) != 64 or not full_id.startswith(container_id) or any(c not in "0123456789abcdef" for c in full_id)
                or not isinstance(payload.get("Name"), str) or cfg.get("Image") != runtime.image
                or not isinstance(host, dict) or host.get("NetworkMode") != DOCKER_NETWORK
                or not isinstance(networks, dict) or not isinstance(networks.get("Networks"), dict) or set(networks["Networks"]) != {DOCKER_NETWORK}
                or not isinstance(state.get("Running"), bool) or not isinstance(state.get("Paused"), bool)
                or not isinstance(state.get("Restarting"), bool) or not isinstance(state.get("Dead"), bool) or not isinstance(state.get("Pid"), int)
                or state["Paused"] or state["Restarting"] or state["Dead"] or (state["Running"] and state["Pid"] <= 0) or (not state["Running"] and state["Pid"] != 0)):
            raise FixtureRunnerError("docker_container_identity_invalid")
        rows.append({"id": full_id, "name": payload["Name"], "running": state["Running"], "labels": labels})
    return rows


def _stop_effect_is_proven(config: RunnerConfiguration, identity: dict[str, object], container_id: str) -> bool:
    """Docker's ``--rm`` may remove the exact stopped container immediately."""
    rows = _containers_for_work_item(config, str(identity["work_item_id"]))
    if not rows:
        return True
    try:
        observed = _exact_container(config, identity)
    except FixtureRunnerError:
        return False
    return observed["id"] == container_id and not bool(observed["running"])


def _nonblocking_active_lock(config: RunnerConfiguration):
    class _Lock:
        def __enter__(self):
            _safe_regular(config.active_lock_path, "active_lock")
            self.stream = config.active_lock_path.open("rb", buffering=0)
            try: fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self.stream.close(); raise FixtureRunnerError("runner_operation_busy") from exc
            return self
        def __exit__(self, *_):
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN); self.stream.close()
    return _Lock()


def _git_head(config: RunnerConfiguration, repository: Path) -> str:
    result = _git(config, repository, "rev-parse", "HEAD")
    try: return validate_git_sha(result.stdout.strip(), "workspace_head")
    except ValueError as exc: raise FixtureRunnerError("workspace_head_unavailable") from exc


def _git_status(config: RunnerConfiguration, repository: Path) -> list[dict[str, str]]:
    indexed = _git(config, repository, "ls-files", "--stage", "-z").stdout
    if any(entry and entry.split(" ", 1)[0] not in {"100644", "100755"} for entry in indexed.split("\0")):
        raise FixtureRunnerError("git_symlink_or_submodule_unsupported")
    result = _git(config, repository, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all")
    entries = [entry for entry in result.stdout.split("\0") if entry]
    rows = []
    for entry in entries:
        if len(entry) < 4 or entry[2] != " ": raise FixtureRunnerError("git_status_malformed")
        path = _safe_repo_path(entry[3:]); kind = "untracked" if entry.startswith("?? ") else "tracked"
        rows.append({"path": path, "kind": kind, "status": entry[:2]})
    return sorted(rows, key=lambda row: (row["kind"], row["path"]))


def _git(config: RunnerConfiguration, repository: Path, *arguments: str):
    git_dir = repository / ".git"
    _trusted_parents(git_dir / "config")
    if not git_dir.is_dir() or git_dir.is_symlink():
        raise FixtureRunnerError("git_directory_unsafe")
    environment = {
        "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1", "LC_ALL": "C",
    }
    prefix = (str(config.git_path), "--git-dir=" + str(git_dir), "--work-tree=" + str(repository),
              "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
              "-c", "diff.external=", "-c", "protocol.allow=never",
              "-c", "submodule.recurse=false", "-c", "status.renames=false")
    filters = run_command((*prefix, "config", "--includes", "--null", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"),
                          timeout_seconds=config.git_timeout_seconds, max_output_bytes=4096, env=environment)
    if (filters.timed_out or filters.error is not None or filters.returncode not in {0, 1}
            or filters.stderr or filters.stdout_truncated or filters.stderr_truncated or filters.stdout):
        raise FixtureRunnerError("git_filter_configuration_unsafe")
    result = run_command((*prefix, *arguments), timeout_seconds=config.git_timeout_seconds,
                         max_output_bytes=_MAX_GIT_BYTES, env=environment)
    if (result.timed_out or result.error is not None or result.returncode != 0
            or result.stderr or result.stdout_truncated or result.stderr_truncated):
        raise FixtureRunnerError("git_readonly_inspection_failed")
    return result


def _bounded_paths(rows: list[dict[str, str]], *, tracked: bool) -> list[str]:
    return [row["path"] for row in rows if (row["kind"] == "tracked") == tracked][:_MAX_PATHS]


def _safe_repo_path(value: str) -> str:
    if not value or "\x00" in value or value.startswith("/") or ".." in Path(value).parts or "\\" in value:
        raise FixtureRunnerError("repository_path_unsafe")
    return value


def _require_runner_user() -> None:
    try:
        account = pwd.getpwnam("codex-runner")
    except KeyError as exc:
        raise FixtureRunnerError("runner_account_unavailable") from exc
    if os.geteuid() == 0 or os.geteuid() != account.pw_uid:
        raise FixtureRunnerError("runner_account_required")


def _trusted_parents(path: Path) -> None:
    for parent in path.parents:
        metadata = parent.lstat()
        sticky_root = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, os.geteuid()}
                or (metadata.st_mode & 0o022 and not sticky_root)):
            raise FixtureRunnerError("untrusted_parent_directory")


def _safe_directory(path: Path, name: str) -> None:
    _trusted_parents(path)
    try: metadata = path.lstat()
    except OSError as exc: raise FixtureRunnerError(f"{name}_unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise FixtureRunnerError(f"{name}_unsafe")


def _safe_regular(path: Path, name: str) -> None:
    _trusted_parents(path)
    try: metadata = path.lstat()
    except OSError as exc: raise FixtureRunnerError(f"{name}_unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077 or metadata.st_nlink != 1:
        raise FixtureRunnerError(f"{name}_unsafe")


def _read_json(path: Path) -> dict[str, object]:
    _safe_regular(path, "receipt")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(_MAX_JSON_BYTES + 1)
    if not raw or len(raw) > _MAX_JSON_BYTES: raise FixtureRunnerError("receipt_invalid")
    return _json_bytes(raw)


def _json_bytes(raw: bytes) -> dict[str, object]:
    try: value = json.loads(raw.decode(), object_pairs_hook=lambda pairs: _unique_object(pairs))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc: raise FixtureRunnerError("receipt_invalid") from exc
    if not isinstance(value, dict): raise FixtureRunnerError("receipt_invalid")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result: raise ValueError("duplicate JSON")
        result[key] = value
    return result


def _receipt_directory(identity: dict[str, object], kind: str) -> Path:
    return OPERATOR_ROOT / kind / str(identity["work_item_id"])


def _prepare_operator_root(directory: Path) -> None:
    if not directory.is_absolute() or OPERATOR_ROOT not in directory.parents:
        raise FixtureRunnerError("operator_evidence_path_invalid")
    _safe_directory(OPERATOR_ROOT, "operator_evidence_root")
    current = OPERATOR_ROOT
    for part in directory.relative_to(OPERATOR_ROOT).parts:
        current = current / part
        current.mkdir(mode=0o700, exist_ok=True)
        _safe_directory(current, "operator_evidence_directory")
        descriptor = os.open(current.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _stop_dedup_identity(identity: dict[str, object]) -> dict[str, object]:
    return {key: identity[key] for key in ("work_item_id", "turn_id")}


def _stop_request_sha(identity: dict[str, object]) -> str:
    return _canonical_sha(_stop_dedup_identity(identity))


def _stop_receipt_state(identity: dict[str, object]) -> dict[str, object] | None:
    _trusted_parents(OPERATOR_ROOT)
    if not OPERATOR_ROOT.exists() and not OPERATOR_ROOT.is_symlink():
        return None
    _safe_directory(OPERATOR_ROOT, "operator_evidence_root")
    request_sha = _stop_request_sha(identity)
    directory = _receipt_directory(identity, "stops")
    receipt = directory / f"{request_sha}.json"
    intent = directory / f"{request_sha}.intent.json"
    if receipt.exists() or receipt.is_symlink():
        return _read_existing_receipt(receipt, identity)
    if intent.exists() or intent.is_symlink():
        payload = _read_json(intent)
        if (set(payload) != {"version", "state", "identity", "dedup_identity", "container_id", "created_at"}
                or payload.get("version") != 1 or payload.get("state") != "intent"
                or payload.get("identity") != identity
                or payload.get("dedup_identity") != _stop_dedup_identity(identity)):
            raise FixtureRunnerError("stop_intent_conflict")
        return {"command": "status", "reason": "stop_intent_ambiguous", "state_writes": 0}
    return None


def _workspace_reader(config: RunnerConfiguration) -> RunnerWorkspace:
    return RunnerWorkspace(git_path=config.git_path, work_items_root=config.work_items_root)


def _storage_absent(config: RunnerConfiguration, identity: dict[str, object], *, archive: str) -> bool:
    workspace = config.work_items_root / str(identity["repository"]).replace("/", "__") / f"issue-{identity['issue']}"
    if workspace.exists() or workspace.is_symlink():
        return False
    wi = str(identity["work_item_id"])
    staging_root = config.work_items_root / ".archive-staging"
    if staging_root.exists() or staging_root.is_symlink():
        _safe_directory(staging_root, "archive_staging")
        staged = staging_root / f"{wi}.workspace"
        if staged.exists() or staged.is_symlink():
            return False
    if archive == "absent":
        registry = config.work_items_root / ".registry" / f"{wi}.json"
        if registry.exists() or registry.is_symlink():
            return False
    runtime = getattr(config, "work_item_disk", None)
    if runtime is not None:
        _safe_directory(runtime.image_directory, "image_directory")
        for directory in (runtime.image_directory / ".archive", runtime.image_directory / ".staging"):
            if directory.exists() or directory.is_symlink():
                _safe_directory(directory, "image_staging")
        for path in (runtime.image_directory / f"{wi}.ext4",
                     runtime.image_directory / ".archive" / f"{wi}.ext4"):
            if path.exists() or path.is_symlink():
                return False
        staging = runtime.image_directory / ".staging"
        if staging.exists() or staging.is_symlink():
            _safe_directory(staging, "image_staging")
            if any(path.name.startswith(wi) for path in staging.iterdir()):
                return False
    return True


def _archive_status(config: RunnerConfiguration, identity: dict[str, object]) -> str:
    root = config.work_items_root
    _safe_directory(root, "work_items_root")
    wi = str(identity["work_item_id"])
    paths = [root / directory / f"{wi}.json" for directory in (".archives", ".absences")]
    present = []
    for path in paths:
        if path.parent.exists() or path.parent.is_symlink():
            _safe_directory(path.parent, "archive_directory")
        exists = path.exists() or path.is_symlink()
        if exists:
            _read_json(path)
        present.append(exists)
    if all(present):
        raise FixtureRunnerError("archive_absence_conflict")
    reader = _workspace_reader(config)
    if present[0]:
        record = reader._read_archive_record(wi, required=True)
        metadata_path = root / ".registry" / f"{wi}.json"
        _read_json(metadata_path)
        metadata = reader._read_registry(wi, required=True)
        if (record.expected_head_sha != identity["expected_head"]
                or metadata.repository != identity["repository"] or metadata.issue_number != identity["issue"]
                or record.metadata_sha256 != reader._metadata_sha256(metadata)):
            raise FixtureRunnerError("archive_identity_mismatch")
        return record.state
    if present[1]:
        record = reader._read_absence_record(wi, required=True)
        if (record.expected_head_sha != identity["expected_head"] or record.repository != identity["repository"]
                or record.issue_number != identity["issue"]):
            raise FixtureRunnerError("absence_identity_mismatch")
        return "absent"
    return "none"


def _create_intent(path: Path, identity: dict[str, object], container_id: str) -> None:
    if path.exists() or path.is_symlink(): raise FixtureRunnerError("stop_intent_ambiguous")
    _write_exclusive_json(path, {"version": 1, "state": "intent", "identity": identity,
                                "dedup_identity": _stop_dedup_identity(identity), "container_id": container_id, "created_at": int(time.time())})


def _read_existing_receipt(path: Path, identity: dict[str, object]) -> dict[str, object] | None:
    if not path.exists() and not path.is_symlink(): return None
    payload = _read_json(path)
    container_id = payload.get("container_id")
    if (set(payload) != {"version", "state", "request_sha256", "container_id", "identity", "dedup_identity"}
            or payload.get("version") != 1 or payload.get("state") != "stopped"
            or payload.get("request_sha256") != _canonical_sha(identity)
            or payload.get("dedup_identity") != _stop_dedup_identity(identity)
            or payload.get("identity") != identity or not isinstance(container_id, str)
            or len(container_id) != 64 or any(character not in "0123456789abcdef" for character in container_id)):
        raise FixtureRunnerError("stop_receipt_conflict")
    return {"command": "stop", "reason": "already_stopped", "receipt": str(path), "state_writes": 0, **payload}


def _write_exclusive_json(path: Path, payload: dict[str, object]) -> None:
    _write_exclusive_bytes(path, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n")


def _write_exclusive_bytes(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    except Exception:
        try: os.unlink(path)
        except OSError: pass
        raise


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
