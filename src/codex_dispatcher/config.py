"""Strict, secret-free TOML configuration parsing."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from codex_dispatcher.repository_admission import (
    RepositoryClass,
    RepositoryRecoveryProfile,
    RepositoryTargetReadbackProfile,
)
from codex_dispatcher.slack_reporting import validate_slack_channel_id
from codex_dispatcher.task_spec import TaskSpecError, is_hard_denied_path, normalize_repo_path


SLACK_IDEMPOTENCY_CONTRACT_V1 = "client_msg_id-live-fixture-verified-v1"


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    database_path: Path
    workspace_root: Path
    poll_interval_seconds: int
    global_max_active: int
    terminal_full_scan_interval_seconds: int = 86400


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
    max_active: int
    allowed_paths: tuple[str, ...]
    denied_paths: tuple[str, ...]
    maintainers: tuple[str, ...]
    required_checks: tuple[str, ...]
    repository_class: RepositoryClass = RepositoryClass.UNCLASSIFIED


@dataclass(frozen=True, slots=True)
class RepositoryAdmissionConfig:
    recovery_profiles: frozenset[RepositoryRecoveryProfile]
    target_readback_profiles: frozenset[RepositoryTargetReadbackProfile]


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
    completed_retention_seconds: int | None = None
    terminal_branch_retention_seconds: int | None = None
    terminal_branch_retention_cutover_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SlackRuntimeConfig:
    issue_channel_id: str
    system_channel_id: str
    request_timeout_seconds: int
    idempotency_contract: str


@dataclass(frozen=True, slots=True)
class SessionRuntimeConfig:
    """Explicit protocol-v2 session policy required by the protected SSH runtime."""

    protocol_version: int
    agent_policy_digest: str
    max_turns_per_session: int
    rotate_after_input_tokens: int
    rotate_after_session_age_seconds: int
    rotate_before_final_audit: bool
    use_incremental_resume_prompts: bool
    max_session_generations: int
    max_total_turns: int
    max_no_progress_turns: int
    max_repair_cycles: int
    max_audit_cycles: int
    max_total_tokens: int
    max_work_item_age_seconds: int


@dataclass(frozen=True, slots=True)
class Config:
    scheduler: SchedulerConfig
    tools: ToolPins
    repositories: tuple[RepositoryConfig, ...]
    ssh_runtime: SshRuntimeConfig | None = None
    slack_runtime: SlackRuntimeConfig | None = None
    session_runtime: SessionRuntimeConfig | None = None
    repository_admission: RepositoryAdmissionConfig | None = None


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


def _retention_seconds(value: Any, path: str) -> int:
    parsed = _positive_int(value, path)
    if parsed > 10 * 366 * 24 * 60 * 60:
        raise ValueError(f"{path} must not exceed ten years")
    return parsed


def _utc_timestamp(value: Any, path: str) -> datetime:
    raw = _string(value, path)
    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)",
        raw,
    ) is None:
        raise ValueError(f"{path} must be an RFC 3339 UTC timestamp string")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{path} must be an RFC 3339 UTC timestamp string") from exc
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{path} must be an RFC 3339 UTC timestamp string")
    return parsed.astimezone(timezone.utc)


def _terminal_scan_interval(value: Any, path: str) -> int:
    parsed = _positive_int(value, path)
    if not 900 <= parsed <= 7 * 24 * 60 * 60:
        raise ValueError(f"{path} must be between 900 and 604800 seconds")
    return parsed


def _boolean(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{path} must be a boolean")
    return value


def _lowercase_sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{path} must be a lowercase SHA-256 digest")
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
    unknown_root = set(raw) - required_root - {
        "ssh_runtime",
        "slack_runtime",
        "session_runtime",
        "repository_admission",
    }
    missing_root = required_root - set(raw)
    if unknown_root:
        raise ValueError(f"root has unknown field(s): {', '.join(sorted(unknown_root))}")
    if missing_root:
        raise ValueError(f"root is missing required field(s): {', '.join(sorted(missing_root))}")
    scheduler = _expect_table(raw["scheduler"], "scheduler")
    required_scheduler = frozenset(
        {"database_path", "workspace_root", "poll_interval_seconds", "global_max_active"}
    )
    unknown_scheduler = set(scheduler) - required_scheduler - {
        "terminal_full_scan_interval_seconds"
    }
    missing_scheduler = required_scheduler - set(scheduler)
    if unknown_scheduler:
        raise ValueError(
            "scheduler has unknown field(s): " + ", ".join(sorted(unknown_scheduler))
        )
    if missing_scheduler:
        raise ValueError(
            "scheduler is missing required field(s): " + ", ".join(sorted(missing_scheduler))
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
    required_repository_keys = frozenset(
        {
            "slug", "base_branch", "max_active", "allowed_paths",
            "denied_paths", "maintainers", "required_checks",
        }
    )
    optional_repository_keys = frozenset({"repository_class"})
    for index, raw_repository in enumerate(repositories):
        item_path = f"repositories[{index}]"
        repository = _expect_table(raw_repository, item_path)
        unknown_repository_keys = (
            set(repository) - required_repository_keys - optional_repository_keys
        )
        missing_repository_keys = required_repository_keys - set(repository)
        if unknown_repository_keys:
            raise ValueError(
                f"{item_path} has unknown field(s): "
                + ", ".join(sorted(unknown_repository_keys))
            )
        if missing_repository_keys:
            raise ValueError(
                f"{item_path} is missing required field(s): "
                + ", ".join(sorted(missing_repository_keys))
            )
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
                repository_class=(
                    _repository_class(
                        repository["repository_class"],
                        f"{item_path}.repository_class",
                    )
                    if "repository_class" in repository
                    else RepositoryClass.UNCLASSIFIED
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
    slack_runtime = (
        None
        if "slack_runtime" not in raw
        else _parse_slack_runtime(raw["slack_runtime"])
    )
    session_runtime = (
        None
        if "session_runtime" not in raw
        else _parse_session_runtime(raw["session_runtime"])
    )
    repository_admission = (
        None
        if "repository_admission" not in raw
        else _parse_repository_admission(raw["repository_admission"])
    )
    if ssh_runtime is not None and "ssh_version" not in tools:
        raise ValueError("tools.ssh_version is required when ssh_runtime is configured")
    if slack_runtime is not None and ssh_runtime is None:
        raise ValueError("slack_runtime requires ssh_runtime")
    if session_runtime is not None and ssh_runtime is None:
        raise ValueError("session_runtime requires ssh_runtime")
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
            terminal_full_scan_interval_seconds=(
                _terminal_scan_interval(
                    scheduler["terminal_full_scan_interval_seconds"],
                    "scheduler.terminal_full_scan_interval_seconds",
                )
                if "terminal_full_scan_interval_seconds" in scheduler
                else 86400
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
        slack_runtime=slack_runtime,
        session_runtime=session_runtime,
        repository_admission=repository_admission,
    )


def _repository_class(value: Any, path: str) -> RepositoryClass:
    raw = _string(value, path)
    try:
        return RepositoryClass(raw)
    except ValueError as exc:
        raise ValueError(
            f"{path} must be one of: "
            + ", ".join(item.value for item in RepositoryClass)
        ) from exc


def _parse_repository_admission(value: Any) -> RepositoryAdmissionConfig:
    table = _expect_table(value, "repository_admission")
    _check_keys(
        table,
        frozenset({"recovery_profiles", "target_readback_profiles"}),
        "repository_admission",
    )
    recovery_raw = _string_list(
        table["recovery_profiles"], "repository_admission.recovery_profiles"
    )
    readback_raw = _string_list(
        table["target_readback_profiles"],
        "repository_admission.target_readback_profiles",
    )
    try:
        recovery_profiles = frozenset(
            RepositoryRecoveryProfile(item) for item in recovery_raw
        )
    except ValueError as exc:
        raise ValueError(
            "repository_admission.recovery_profiles contains an unsupported profile"
        ) from exc
    try:
        target_readback_profiles = frozenset(
            RepositoryTargetReadbackProfile(item) for item in readback_raw
        )
    except ValueError as exc:
        raise ValueError(
            "repository_admission.target_readback_profiles contains an unsupported profile"
        ) from exc
    return RepositoryAdmissionConfig(
        recovery_profiles=recovery_profiles,
        target_readback_profiles=target_readback_profiles,
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
    optional = frozenset(
        {
            "assh_proxy_path",
            "assh_home",
            "completed_retention_seconds",
            "terminal_branch_retention_seconds",
            "terminal_branch_retention_cutover_at",
        }
    )
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
    if (
        "terminal_branch_retention_cutover_at" in table
        and "terminal_branch_retention_seconds" not in table
    ):
        raise ValueError(
            "ssh_runtime.terminal_branch_retention_cutover_at requires "
            "ssh_runtime.terminal_branch_retention_seconds"
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
        completed_retention_seconds=(
            _retention_seconds(
                table["completed_retention_seconds"],
                "ssh_runtime.completed_retention_seconds",
            )
            if "completed_retention_seconds" in table
            else None
        ),
        terminal_branch_retention_seconds=(
            _retention_seconds(
                table["terminal_branch_retention_seconds"],
                "ssh_runtime.terminal_branch_retention_seconds",
            )
            if "terminal_branch_retention_seconds" in table
            else None
        ),
        terminal_branch_retention_cutover_at=(
            _utc_timestamp(
                table["terminal_branch_retention_cutover_at"],
                "ssh_runtime.terminal_branch_retention_cutover_at",
            )
            if "terminal_branch_retention_cutover_at" in table
            else None
        ),
    )


def _parse_slack_runtime(value: Any) -> SlackRuntimeConfig:
    table = _expect_table(value, "slack_runtime")
    _check_keys(
        table,
        frozenset(
            {
                "issue_channel_id",
                "system_channel_id",
                "request_timeout_seconds",
                "idempotency_contract",
            }
        ),
        "slack_runtime",
    )
    issue_channel_id = _string(
        table["issue_channel_id"], "slack_runtime.issue_channel_id"
    )
    system_channel_id = _string(
        table["system_channel_id"], "slack_runtime.system_channel_id"
    )
    for field, channel_id in (
        ("issue_channel_id", issue_channel_id),
        ("system_channel_id", system_channel_id),
    ):
        try:
            validate_slack_channel_id(channel_id)
        except ValueError as exc:
            raise ValueError(f"slack_runtime.{field} is invalid") from exc
    if issue_channel_id == system_channel_id:
        raise ValueError(
            "slack_runtime issue and system channels must be different"
        )
    timeout = _positive_int(
        table["request_timeout_seconds"],
        "slack_runtime.request_timeout_seconds",
    )
    if timeout > 60:
        raise ValueError(
            "slack_runtime.request_timeout_seconds must not exceed 60"
        )
    idempotency_contract = _string(
        table["idempotency_contract"],
        "slack_runtime.idempotency_contract",
    )
    if idempotency_contract != SLACK_IDEMPOTENCY_CONTRACT_V1:
        raise ValueError(
            "slack_runtime.idempotency_contract requires the exact live-fixture proof value"
        )
    return SlackRuntimeConfig(
        issue_channel_id=issue_channel_id,
        system_channel_id=system_channel_id,
        request_timeout_seconds=timeout,
        idempotency_contract=idempotency_contract,
    )


def _parse_session_runtime(value: Any) -> SessionRuntimeConfig:
    table = _expect_table(value, "session_runtime")
    fields = frozenset(
        {
            "protocol_version",
            "agent_policy_digest",
            "max_turns_per_session",
            "rotate_after_input_tokens",
            "rotate_after_session_age_seconds",
            "rotate_before_final_audit",
            "use_incremental_resume_prompts",
            "max_session_generations",
            "max_total_turns",
            "max_no_progress_turns",
            "max_repair_cycles",
            "max_audit_cycles",
            "max_total_tokens",
            "max_work_item_age_seconds",
        }
    )
    _check_keys(table, fields, "session_runtime")
    protocol_version = table["protocol_version"]
    if type(protocol_version) is not int or protocol_version != 2:
        raise ValueError("session_runtime.protocol_version must equal 2")
    rotate_before_final_audit = _boolean(
        table["rotate_before_final_audit"],
        "session_runtime.rotate_before_final_audit",
    )
    return SessionRuntimeConfig(
        protocol_version=protocol_version,
        agent_policy_digest=_lowercase_sha256(
            table["agent_policy_digest"],
            "session_runtime.agent_policy_digest",
        ),
        max_turns_per_session=_positive_int(
            table["max_turns_per_session"],
            "session_runtime.max_turns_per_session",
        ),
        rotate_after_input_tokens=_positive_int(
            table["rotate_after_input_tokens"],
            "session_runtime.rotate_after_input_tokens",
        ),
        rotate_after_session_age_seconds=_positive_int(
            table["rotate_after_session_age_seconds"],
            "session_runtime.rotate_after_session_age_seconds",
        ),
        rotate_before_final_audit=rotate_before_final_audit,
        use_incremental_resume_prompts=_boolean(
            table["use_incremental_resume_prompts"],
            "session_runtime.use_incremental_resume_prompts",
        ),
        max_session_generations=_positive_int(
            table["max_session_generations"],
            "session_runtime.max_session_generations",
        ),
        max_total_turns=_positive_int(
            table["max_total_turns"],
            "session_runtime.max_total_turns",
        ),
        max_no_progress_turns=_positive_int(
            table["max_no_progress_turns"],
            "session_runtime.max_no_progress_turns",
        ),
        max_repair_cycles=_positive_int(
            table["max_repair_cycles"],
            "session_runtime.max_repair_cycles",
        ),
        max_audit_cycles=_positive_int(
            table["max_audit_cycles"],
            "session_runtime.max_audit_cycles",
        ),
        max_total_tokens=_positive_int(
            table["max_total_tokens"],
            "session_runtime.max_total_tokens",
        ),
        max_work_item_age_seconds=_positive_int(
            table["max_work_item_age_seconds"],
            "session_runtime.max_work_item_age_seconds",
        ),
    )
