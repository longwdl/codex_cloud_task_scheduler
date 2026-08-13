"""Strict, secret-free TOML configuration parsing."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
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
class Config:
    scheduler: SchedulerConfig
    tools: ToolPins
    repositories: tuple[RepositoryConfig, ...]


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

    _check_keys(raw, frozenset({"scheduler", "tools", "repositories"}), "root")
    scheduler = _expect_table(raw["scheduler"], "scheduler")
    _check_keys(
        scheduler,
        frozenset(
            {"database_path", "workspace_root", "poll_interval_seconds", "global_max_active"}
        ),
        "scheduler",
    )
    tools = _expect_table(raw["tools"], "tools")
    _check_keys(tools, frozenset({"git_version", "gh_version", "codex_version"}), "tools")
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
        ),
        repositories=tuple(parsed_repositories),
    )
