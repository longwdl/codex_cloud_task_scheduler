"""Trusted dependency assembly for one explicitly enabled SSH dispatcher sweep."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from codex_dispatcher.config import Config, SshRuntimeConfig, load_config
from codex_dispatcher.contract import ContractCheck, run_control_host_contract_checks
from codex_dispatcher.control_sweep import ControlSweepResult, SshControlSweep
from codex_dispatcher.dispatcher_lock import DispatcherProcessLock
from codex_dispatcher.git_bundle_verifier import GitBundleQuarantineVerifier
from codex_dispatcher.git_publisher import GitTaskBranchPublisher
from codex_dispatcher.github_delivery import GitHubDeliveryCoordinator
from codex_dispatcher.source_bundle import GitSourceBundleBuilder
from codex_dispatcher.ssh_dispatch_service import OfflineSshDispatchService
from codex_dispatcher.ssh_preflight import SshPreflightPlan, build_ssh_preflight_plan
from codex_dispatcher.ssh_runner_transport import SshRunnerTransport
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.trackers.github_cli import GitHubCliTracker
from codex_dispatcher.trusted_mirror import GitHubMirrorRefresher, TrustedMirrorSource
from codex_dispatcher.turn_orchestration import OfflineTurnOrchestrator


class SshRuntimeError(RuntimeError):
    """Raised before a live sweep when the trusted runtime boundary is incomplete."""


@dataclass(frozen=True, slots=True)
class SshPreflightInspection:
    """Auditable read-only evidence for one potential live SSH sweep."""

    plan: SshPreflightPlan
    checks: tuple[ContractCheck, ...]
    database_preexisting: bool


def load_protected_ssh_config(path: Path) -> Config:
    """Load one bounded, owned runtime config that cannot be replaced by other users."""
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise SshRuntimeError("SSH runtime config path must be normalized and absolute")
    _protected_directory(
        path.parent,
        "SSH runtime config directory",
        allow_root=True,
    )
    _protected_regular_file(
        path,
        "SSH runtime config",
        maximum_size=256 * 1024,
        allow_root=True,
    )
    return load_config(path)


def validate_runtime_state_path(path: Path) -> None:
    """Require a protected SQLite parent and protected existing database sidecars."""
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise SshRuntimeError("SSH runtime database_path must be normalized and absolute")
    _protected_directory(path.parent, "SSH runtime database directory")
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists() or candidate.is_symlink():
            _protected_regular_file(
                candidate,
                "SSH runtime database file",
                maximum_size=None,
                allow_root=False,
            )


def build_ssh_control_sweep(
    *,
    config: Config,
    store: StateStore,
    github_token: str,
) -> SshControlSweep:
    """Assemble live ports without invoking GitHub, Git fetch, or SSH."""
    if not isinstance(config, Config):
        raise TypeError("config must be a Config")
    if not isinstance(store, StateStore):
        raise TypeError("store must be a StateStore")
    runtime = _require_runtime(config)
    git_path = _protected_executable(runtime.git_path, "ssh_runtime.git_path")
    gh_path = _protected_executable(runtime.gh_path, "ssh_runtime.gh_path")
    ssh_path = _protected_executable(runtime.ssh_path, "ssh_runtime.ssh_path")

    return _assemble_ssh_control_sweep(
        config=config,
        store=store,
        github_token=github_token,
        runtime=runtime,
        git_path=git_path,
        gh_path=gh_path,
        ssh_path=ssh_path,
    )


def _assemble_ssh_control_sweep(
    *,
    config: Config,
    store: StateStore,
    github_token: str,
    runtime: SshRuntimeConfig,
    git_path: Path,
    gh_path: Path,
    ssh_path: Path,
) -> SshControlSweep:
    """Assemble ports using the exact executable paths that were verified."""

    tracker = GitHubCliTracker(gh_path=gh_path, token=github_token)
    source = TrustedMirrorSource(
        refresher=GitHubMirrorRefresher(
            git_path=git_path,
            mirror_root=runtime.mirror_root,
            github_token=github_token,
        ),
        builder=GitSourceBundleBuilder(
            git_path=git_path,
            mirror_root=runtime.mirror_root,
            temporary_root=runtime.source_temporary_root,
        ),
    )
    transport = SshRunnerTransport(
        ssh_path=ssh_path,
        host=runtime.host,
        user=runtime.user,
        port=runtime.port,
        known_hosts_path=runtime.known_hosts_path,
        identity_file=runtime.identity_file,
        assh_proxy_path=runtime.assh_proxy_path,
        assh_home=runtime.assh_home,
        connect_timeout_seconds=runtime.connect_timeout_seconds,
        operation_timeout_seconds=runtime.operation_timeout_seconds,
    )
    orchestrator = OfflineTurnOrchestrator(
        store=store,
        transport=transport,
        bundle_verifier=GitBundleQuarantineVerifier(
            git_path=git_path,
            quarantine_root=runtime.quarantine_root,
        ),
    )
    dispatch = OfflineSshDispatchService(
        config=config,
        store=store,
        orchestrator=orchestrator,
    )
    return SshControlSweep(
        config=config,
        store=store,
        tracker=tracker,
        dispatch=dispatch,
        source=source,
        process_lock=DispatcherProcessLock(runtime.lock_path),
        publisher=GitTaskBranchPublisher(
            git_path=git_path,
            mirror_root=runtime.mirror_root,
            temporary_root=runtime.publisher_temporary_root,
            github_token=github_token,
        ),
        delivery=GitHubDeliveryCoordinator(store=store, tracker=tracker),
        runner_root=runtime.runner_root,
    )


def run_ssh_control_sweep(
    *,
    config: Config,
    store: StateStore,
    github_token: str,
) -> ControlSweepResult:
    """Verify pinned Control Host tools, then execute exactly one locked sweep."""
    if not isinstance(config, Config):
        raise TypeError("config must be a Config")
    if not isinstance(store, StateStore):
        raise TypeError("store must be a StateStore")
    runtime = _require_runtime(config)
    git_path = _protected_executable(runtime.git_path, "ssh_runtime.git_path")
    gh_path = _protected_executable(runtime.gh_path, "ssh_runtime.gh_path")
    ssh_path = _protected_executable(runtime.ssh_path, "ssh_runtime.ssh_path")
    checks = run_control_host_contract_checks(
        pins=config.tools,
        git_path=git_path,
        gh_path=gh_path,
        ssh_path=ssh_path,
    )
    failed = tuple(check.name for check in checks if not check.ok)
    if failed:
        raise SshRuntimeError(
            f"Control Host tool contract failed: {', '.join(failed)}"
        )
    sweep = _assemble_ssh_control_sweep(
        config=config,
        store=store,
        github_token=github_token,
        runtime=runtime,
        git_path=git_path,
        gh_path=gh_path,
        ssh_path=ssh_path,
    )
    return sweep.run_once()


def run_ssh_preflight(
    *,
    config: Config,
    github_token: str,
) -> SshPreflightInspection:
    """Inspect tools, GitHub state, and a migrated temporary SQLite snapshot."""
    if not isinstance(config, Config):
        raise TypeError("config must be a Config")
    runtime = _require_runtime(config)
    git_path = _protected_executable(runtime.git_path, "ssh_runtime.git_path")
    gh_path = _protected_executable(runtime.gh_path, "ssh_runtime.gh_path")
    ssh_path = _protected_executable(runtime.ssh_path, "ssh_runtime.ssh_path")
    checks = run_control_host_contract_checks(
        pins=config.tools,
        git_path=git_path,
        gh_path=gh_path,
        ssh_path=ssh_path,
    )
    failed = tuple(check.name for check in checks if not check.ok)
    if failed:
        raise SshRuntimeError(
            f"Control Host tool contract failed: {', '.join(failed)}"
        )
    tracker = GitHubCliTracker(gh_path=gh_path, token=github_token)
    with _temporary_state_snapshot(config.scheduler.database_path) as snapshot:
        store, database_preexisting = snapshot
        plan = build_ssh_preflight_plan(config, store, tracker)
    return SshPreflightInspection(plan, checks, database_preexisting)


@contextmanager
def _temporary_state_snapshot(
    database_path: Path,
) -> Iterator[tuple[StateStore, bool]]:
    """Migrate a disposable snapshot without modifying the configured database."""
    database_preexisting = database_path.is_file()
    with tempfile.TemporaryDirectory(prefix="codex-dispatcher-preflight-") as root:
        snapshot_path = Path(root) / "state.db"
        if database_preexisting:
            with StateStore(database_path, read_only=True) as source:
                if source.integrity_check() != "ok":
                    raise SshRuntimeError("state database integrity check failed")
                source.backup(snapshot_path)
        with StateStore(snapshot_path) as store:
            store.migrate()
            if store.integrity_check() != "ok":
                raise SshRuntimeError("temporary state snapshot integrity check failed")
            yield store, database_preexisting


def _require_runtime(config: Config) -> SshRuntimeConfig:
    runtime = config.ssh_runtime
    if runtime is None:
        raise SshRuntimeError("ssh_runtime configuration is required")
    if config.scheduler.global_max_active != 1 or any(
        repository.max_active != 1 for repository in config.repositories
    ):
        raise SshRuntimeError("SSH runtime requires global and repository max_active to equal 1")
    if config.tools.ssh_version is None:
        raise SshRuntimeError("SSH runtime requires an exact ssh_version pin")
    database_path = config.scheduler.database_path
    if not database_path.is_absolute() or ".." in database_path.parts:
        raise SshRuntimeError("SSH runtime database_path must be normalized and absolute")
    return runtime


def _protected_executable(path: Path, field: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise SshRuntimeError(f"{field} must be a protected executable")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise SshRuntimeError(f"{field} must be a protected executable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & 0o022
        or not metadata.st_mode & 0o111
    ):
        raise SshRuntimeError(f"{field} must be a protected executable")
    return resolved


def _protected_directory(
    path: Path,
    field: str,
    *,
    allow_root: bool = False,
) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SshRuntimeError(f"{field} must be an owned protected directory") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid not in ({0, os.geteuid()} if allow_root else {os.geteuid()})
        or metadata.st_mode & 0o022
    ):
        raise SshRuntimeError(f"{field} must be an owned protected directory")


def _protected_regular_file(
    path: Path,
    field: str,
    *,
    maximum_size: int | None,
    allow_root: bool,
) -> None:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise SshRuntimeError(f"{field} must be an owned protected regular file")
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SshRuntimeError(f"{field} must be an owned protected regular file") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid not in ({0, os.geteuid()} if allow_root else {os.geteuid()})
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or (maximum_size is not None and not 0 < metadata.st_size <= maximum_size)
    ):
        raise SshRuntimeError(f"{field} must be an owned protected regular file")
