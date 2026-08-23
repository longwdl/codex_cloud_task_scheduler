"""Fail-closed repository-class admission for new dispatcher claims."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RepositoryClass(StrEnum):
    UNCLASSIFIED = "unclassified"
    FIXTURE = "fixture"
    HIGHER_VALUE = "higher-value"


class RepositoryRecoveryProfile(StrEnum):
    FIXTURE_LIVE_V1 = "fixture-live-v1"
    HIGHER_VALUE_LIVE_V1 = "higher-value-live-v1"


class RepositoryTargetReadbackProfile(StrEnum):
    FIXTURE_EXACT_V1 = "fixture-exact-v1"
    HIGHER_VALUE_EXACT_V1 = "higher-value-exact-v1"


@dataclass(frozen=True, slots=True)
class RepositoryAdmissionRequirement:
    repository_class: RepositoryClass
    recovery_profile: RepositoryRecoveryProfile | None
    target_readback_profile: RepositoryTargetReadbackProfile | None
    admitted_by_this_release: bool


@dataclass(frozen=True, slots=True)
class RepositoryAdmissionDecision:
    admitted: bool
    code: str | None
    requirement: RepositoryAdmissionRequirement


REPOSITORY_ADMISSION_MATRIX = (
    RepositoryAdmissionRequirement(
        RepositoryClass.UNCLASSIFIED,
        None,
        None,
        False,
    ),
    RepositoryAdmissionRequirement(
        RepositoryClass.FIXTURE,
        RepositoryRecoveryProfile.FIXTURE_LIVE_V1,
        RepositoryTargetReadbackProfile.FIXTURE_EXACT_V1,
        True,
    ),
    RepositoryAdmissionRequirement(
        RepositoryClass.HIGHER_VALUE,
        RepositoryRecoveryProfile.HIGHER_VALUE_LIVE_V1,
        RepositoryTargetReadbackProfile.HIGHER_VALUE_EXACT_V1,
        False,
    ),
)


def evaluate_repository_admission(
    repository_class: RepositoryClass,
    recovery_profiles: frozenset[RepositoryRecoveryProfile],
    target_readback_profiles: frozenset[RepositoryTargetReadbackProfile],
) -> RepositoryAdmissionDecision:
    """Evaluate only new-work admission; existing recovery remains unconditional."""
    if not isinstance(repository_class, RepositoryClass):
        raise TypeError("repository_class must be a RepositoryClass")
    if not isinstance(recovery_profiles, frozenset) or any(
        not isinstance(item, RepositoryRecoveryProfile) for item in recovery_profiles
    ):
        raise TypeError("recovery_profiles must contain only repository recovery profiles")
    if not isinstance(target_readback_profiles, frozenset) or any(
        not isinstance(item, RepositoryTargetReadbackProfile)
        for item in target_readback_profiles
    ):
        raise TypeError(
            "target_readback_profiles must contain only repository readback profiles"
        )
    requirement = next(
        row
        for row in REPOSITORY_ADMISSION_MATRIX
        if row.repository_class is repository_class
    )
    if repository_class is RepositoryClass.UNCLASSIFIED:
        return RepositoryAdmissionDecision(
            False, "repository_class_unclassified", requirement
        )
    if requirement.recovery_profile not in recovery_profiles:
        return RepositoryAdmissionDecision(
            False, "repository_recovery_profile_mismatch", requirement
        )
    if requirement.target_readback_profile not in target_readback_profiles:
        return RepositoryAdmissionDecision(
            False, "repository_target_readback_profile_mismatch", requirement
        )
    if not requirement.admitted_by_this_release:
        return RepositoryAdmissionDecision(
            False, "repository_class_not_admitted", requirement
        )
    return RepositoryAdmissionDecision(True, None, requirement)
