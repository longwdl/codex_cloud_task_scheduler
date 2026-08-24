"""Fail-closed contract for the fixed manual higher-value canary repository."""

from __future__ import annotations

from pathlib import Path

from codex_dispatcher.config import Config
from codex_dispatcher.repository_admission import (
    HIGHER_VALUE_CANARY_REPOSITORY,
    HigherValueCanaryTarget,
    RepositoryClass,
    RepositoryRecoveryProfile,
    RepositoryTargetReadbackProfile,
    evaluate_repository_admission,
)


HIGHER_VALUE_CANARY_MUTABLE_PATH = "canary/target.txt"
HIGHER_VALUE_CANARY_REQUIRED_CHECK = "higher-value-canary"
HIGHER_VALUE_CANARY_MAINTAINER = "longwdl"
HIGHER_VALUE_CANARY_ISOLATION_COMPONENT = "higher-value-canary"


class HigherValueCanaryRejected(RuntimeError):
    """Raised before a manual canary can read or mutate external state."""


def validate_higher_value_canary_config(
    config: Config,
    target: HigherValueCanaryTarget,
) -> None:
    """Require an isolated, exact-target configuration for the manual CLI."""
    if not isinstance(config, Config):
        raise TypeError("config must be a Config")
    if not isinstance(target, HigherValueCanaryTarget):
        raise TypeError("target must be a HigherValueCanaryTarget")
    if config.scheduler.global_max_active != 1:
        raise HigherValueCanaryRejected(
            "higher-value canary global_max_active must equal one"
        )
    if len(config.repositories) != 1:
        raise HigherValueCanaryRejected(
            "higher-value canary config must contain exactly one repository"
        )
    repository = config.repositories[0]
    if (
        repository.slug != HIGHER_VALUE_CANARY_REPOSITORY
        or target.repository != repository.slug
        or repository.repository_class is not RepositoryClass.HIGHER_VALUE
        or repository.base_branch != "main"
        or repository.max_active != 1
        or repository.allowed_paths != (HIGHER_VALUE_CANARY_MUTABLE_PATH,)
        or repository.denied_paths != ()
        or repository.maintainers != (HIGHER_VALUE_CANARY_MAINTAINER,)
        or repository.required_checks != (HIGHER_VALUE_CANARY_REQUIRED_CHECK,)
    ):
        raise HigherValueCanaryRejected(
            "higher-value canary repository policy is not exact"
        )
    admission = config.repository_admission
    if (
        admission is None
        or admission.recovery_profiles
        != frozenset({RepositoryRecoveryProfile.HIGHER_VALUE_LIVE_V1})
        or admission.target_readback_profiles
        != frozenset({RepositoryTargetReadbackProfile.HIGHER_VALUE_EXACT_V1})
    ):
        raise HigherValueCanaryRejected(
            "higher-value canary runtime profiles are not exact"
        )
    ordinary = evaluate_repository_admission(
        repository.repository_class,
        admission.recovery_profiles,
        admission.target_readback_profiles,
    )
    if ordinary.admitted or ordinary.code != "repository_class_not_admitted":
        raise HigherValueCanaryRejected(
            "ordinary higher-value admission must remain hard false"
        )
    if config.ssh_runtime is None or config.session_runtime is None:
        raise HigherValueCanaryRejected(
            "higher-value canary requires SSH and protocol-v2 session runtimes"
        )
    isolated_paths = (
        config.scheduler.database_path,
        config.scheduler.workspace_root,
        config.ssh_runtime.lock_path,
        config.ssh_runtime.mirror_root,
        config.ssh_runtime.source_temporary_root,
        config.ssh_runtime.quarantine_root,
        config.ssh_runtime.publisher_temporary_root,
    )
    if any(not _is_canary_isolated(path) for path in isolated_paths):
        raise HigherValueCanaryRejected(
            "higher-value canary Control paths are not isolated"
        )


def _is_canary_isolated(path: Path) -> bool:
    return any(
        component == HIGHER_VALUE_CANARY_ISOLATION_COMPONENT
        or component.startswith(f"{HIGHER_VALUE_CANARY_ISOLATION_COMPONENT}.")
        for component in path.parts
    )
