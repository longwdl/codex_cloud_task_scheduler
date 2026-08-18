"""Strict, secret-free TOML configuration parsing."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from codex_dispatcher.task_spec import TaskSpecError, is_hard_denied_path, normalize_repo_path


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    database_path: Path
    workspace_root: Path
    poll_interval_seconds: int
    global_max_active: int


@dataclass(frozen=True, slots=True)
class ToolPins:
    git_version: str
    gh_version: str
    codex_version: str
    ssh_version: str | None = None


@dataclass(frozen=True, slots=True)
class RepositoryConfig:
    slug: str
    base_branch: str
    cloud_environment_id: str
    max_active: int
    allowed_paths: tuple[str, ...]
    denied_paths: tuple[str, ...]
    maintainers: tuple[str, ...]
    required_checks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SshRuntimeConfig:
    git_path: Path
    gh_path: Path
    ssh_path: Path
    host: str
    user: str
    port: int
    known_hosts_path: Path
    identity_file: Path
    lock_path: Path
    mirror_root: Path
    source_temporary_root: Path
    quarantine_root: Path
    publisher_temporary_root: Path
    runner_root: str
    connect_timeout_seconds: int
    operation_timeout_seconds: int
    assh_proxy_path: Path | None = None
    assh_home: Path | None = None


@dataclass(frozen=True, slots=True)
class Config:
    scheduler: SchedulerConfig
    tools: ToolPins
    repositories: tuple[RepositoryConfig, ...]
    ssh_runtime: SshRuntimeConfig | None = None


def _expect_table(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a TOML table")
    return value


def _check_keys(table: dict[str, Any], expected: frozenset[str], path: str) -> None:
    unknown = set(table) - expected
    missing = expected - set(table)
    if unknown:
        raise ValueError(f"{path} has unknown field(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"{path} is missing required field(s): {', '.join(sorted(missing))}")


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return value


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{path} must be a positive integer")
    return value


def _absolute_path(value: Any, path: str) -> Path:
    raw = _string(value, path)
    candidate = Path(raw)
    if (
        not candidate.is_absolute()
        or str(candidate) != raw
        or ".." in candidate.parts
        or "\x00" in raw
    ):
        raise ValueError(f"{path} must be a normalized absolute path")
    return candidate


def _string_list(value: Any, path: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "an array" if allow_empty else "a non-empty array"
        raise ValueError(f"{path} must be {qualifier} of strings")
    items = tuple(_string(item, f"{path}[{index}]") for index, item in enumerate(value))
    if len(set(items)) != len(items):
        raise ValueError(f"{path} must not contain duplicate values")
    return items


def _repo_paths(
    value: Any, path: str, *, allow_empty: bool, allow_hard_denied: bool
) -> tuple[str, ...]:
    items = _string_list(value, path, allow_empty=allow_empty)
    try:
        normalized = tuple(normalize_repo_path(item) for item in items)
    except TaskSpecError as exc:
        raise ValueError(f"{path} contains an invalid repository path: {exc}") from exc
    if not allow_hard_denied and any(is_hard_denied_path(item) for item in normalized):
        raise ValueError(f"{path} must not contain a hard-denied path")
    return normalized


def load_config(path: Path) -> Config:
    """Load an allowlist configuration, rejecting unknown keys and secret fields."""
    try:
        with path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {path}: {exc}") from exc

    required_root = frozenset({"scheduler", "tools", "repositories"})
    unknown_root = set(raw) - required_root - {"ssh_runtime"}
    missing_root = required_root - set(raw)
    if unknown_root:
        raise ValueError(f"root has unknown field(s): {', '.join(sorted(unknown_root))}")
    if missing_root:
        raise ValueError(f"root is missing required field(s): {', '.join(sorted(missing_root))}")
    scheduler = _expect_table(raw["scheduler"], "scheduler")
    _check_keys(
        scheduler,
        frozenset(
            {"database_path", "workspace_root", "poll_interval_seconds", "global_max_active"}
        ),
        "scheduler",
    )
    tools = _expect_table(raw["tools"], "tools")
    required_tools = frozenset({"git_version", "gh_version", "codex_version"})
    unknown_tools = set(tools) - required_tools - {"ssh_version"}
    missing_tools = required_tools - set(tools)
    if unknown_tools:
        raise ValueError(f"tools has unknown field(s): {', '.join(sorted(unknown_tools))}")
    if missing_tools:
        raise ValueError(f"tools is missing required field(s): {', '.join(sorted(missing_tools))}")
    repositories = raw["repositories"]
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("repositories must be a non-empty array of tables")

    parsed_repositories: list[RepositoryConfig] = []
    repository_keys = frozenset(
        {
            "slug", "base_branch", "cloud_environment_id", "max_active", "allowed_paths",
            "denied_paths", "maintainers", "required_checks",
        }
    )
    for index, raw_repository in enumerate(repositories):
        item_path = f"repositories[{index}]"
        repository = _expect_table(raw_repository, item_path)
        _check_keys(repository, repository_keys, item_path)
        slug = _string(repository["slug"], f"{item_path}.slug")
        if slug.count("/") != 1 or any(
            not component or component.strip() != component or any(c.isspace() for c in component)
            for component in slug.split("/")
        ):
            raise ValueError(f"{item_path}.slug must be in owner/repository form")
        parsed_repositories.append(
            RepositoryConfig(
                slug=slug,
                base_branch=_string(repository["base_branch"], f"{item_path}.base_branch"),
                cloud_environment_id=_string(
                    repository["cloud_environment_id"], f"{item_path}.cloud_environment_id"
                ),
                max_active=_positive_int(repository["max_active"], f"{item_path}.max_active"),
                allowed_paths=_repo_paths(
                    repository["allowed_paths"],
                    f"{item_path}.allowed_paths",
                    allow_empty=False,
                    allow_hard_denied=False,
                ),
                denied_paths=_repo_paths(
                    repository["denied_paths"],
                    f"{item_path}.denied_paths",
                    allow_empty=True,
                    allow_hard_denied=True,
                ),
                maintainers=_string_list(repository["maintainers"], f"{item_path}.maintainers"),
                required_checks=_string_list(
                    repository["required_checks"], f"{item_path}.required_checks"
                ),
            )
        )

    slugs = [repository.slug for repository in parsed_repositories]
    if len(set(slugs)) != len(slugs):
        raise ValueError("repositories contains duplicate slug values")
    ssh_runtime = (
        None
        if "ssh_runtime" not in raw
        else _parse_ssh_runtime(raw["ssh_runtime"])
    )
    if ssh_runtime is not None and "ssh_version" not in tools:
        raise ValueError("tools.ssh_version is required when ssh_runtime is configured")
    return Config(
        scheduler=SchedulerConfig(
            database_path=Path(
                _string(scheduler["database_path"], "scheduler.database_path")
            ),
            workspace_root=Path(
                _string(scheduler["workspace_root"], "scheduler.workspace_root")
            ),
            poll_interval_seconds=_positive_int(
                scheduler["poll_interval_seconds"], "scheduler.poll_interval_seconds"
            ),
            global_max_active=_positive_int(
                scheduler["global_max_active"], "scheduler.global_max_active"
            ),
        ),
        tools=ToolPins(
            git_version=_string(tools["git_version"], "tools.git_version"),
            gh_version=_string(tools["gh_version"], "tools.gh_version"),
            codex_version=_string(tools["codex_version"], "tools.codex_version"),
            ssh_version=(
                _string(tools["ssh_version"], "tools.ssh_version")
                if "ssh_version" in tools
                else None
            ),
        ),
        repositories=tuple(parsed_repositories),
        ssh_runtime=ssh_runtime,
    )


def _parse_ssh_runtime(value: Any) -> SshRuntimeConfig:
    table = _expect_table(value, "ssh_runtime")
    required = frozenset(
        {
            "git_path",
            "gh_path",
            "ssh_path",
            "host",
            "user",
            "port",
            "known_hosts_path",
            "identity_file",
            "lock_path",
            "mirror_root",
            "source_temporary_root",
            "quarantine_root",
            "publisher_temporary_root",
            "runner_root",
            "connect_timeout_seconds",
            "operation_timeout_seconds",
        }
    )
    optional = frozenset({"assh_proxy_path", "assh_home"})
    unknown = set(table) - required - optional
    missing = required - set(table)
    if unknown:
        raise ValueError(
            f"ssh_runtime has unknown field(s): {', '.join(sorted(unknown))}"
        )
    if missing:
        raise ValueError(
            f"ssh_runtime is missing required field(s): {', '.join(sorted(missing))}"
        )
    proxy_present = "assh_proxy_path" in table
    home_present = "assh_home" in table
    if proxy_present != home_present:
        raise ValueError(
            "ssh_runtime.assh_proxy_path and ssh_runtime.assh_home must be configured together"
        )
    runner_root = _string(table["runner_root"], "ssh_runtime.runner_root")
    runner_path = PurePosixPath(runner_root)
    if (
        not runner_path.is_absolute()
        or str(runner_path) != runner_root
        or ".." in runner_path.parts
        or "\\" in runner_root
        or "\x00" in runner_root
    ):
        raise ValueError("ssh_runtime.runner_root must be a normalized absolute POSIX path")
    return SshRuntimeConfig(
        git_path=_absolute_path(table["git_path"], "ssh_runtime.git_path"),
        gh_path=_absolute_path(table["gh_path"], "ssh_runtime.gh_path"),
        ssh_path=_absolute_path(table["ssh_path"], "ssh_runtime.ssh_path"),
        host=_string(table["host"], "ssh_runtime.host"),
        user=_string(table["user"], "ssh_runtime.user"),
        port=_positive_int(table["port"], "ssh_runtime.port"),
        known_hosts_path=_absolute_path(
            table["known_hosts_path"], "ssh_runtime.known_hosts_path"
        ),
        identity_file=_absolute_path(
            table["identity_file"], "ssh_runtime.identity_file"
        ),
        lock_path=_absolute_path(table["lock_path"], "ssh_runtime.lock_path"),
        mirror_root=_absolute_path(table["mirror_root"], "ssh_runtime.mirror_root"),
        source_temporary_root=_absolute_path(
            table["source_temporary_root"], "ssh_runtime.source_temporary_root"
        ),
        quarantine_root=_absolute_path(
            table["quarantine_root"], "ssh_runtime.quarantine_root"
        ),
        publisher_temporary_root=_absolute_path(
            table["publisher_temporary_root"],
            "ssh_runtime.publisher_temporary_root",
        ),
        runner_root=runner_root,
        connect_timeout_seconds=_positive_int(
            table["connect_timeout_seconds"],
            "ssh_runtime.connect_timeout_seconds",
        ),
        operation_timeout_seconds=_positive_int(
            table["operation_timeout_seconds"],
            "ssh_runtime.operation_timeout_seconds",
        ),
        assh_proxy_path=(
            _absolute_path(table["assh_proxy_path"], "ssh_runtime.assh_proxy_path")
            if proxy_present
            else None
        ),
        assh_home=(
            _absolute_path(table["assh_home"], "ssh_runtime.assh_home")
            if home_present
            else None
        ),
    )
