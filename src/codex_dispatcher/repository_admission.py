"""Fail-closed repository-class admission for new dispatcher claims."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256


REPOSITORY_ADMISSION_MATRIX_VERSION = 1
HIGHER_VALUE_CANARY_REPOSITORY = "longwdl/codex-dispatcher-fixture-2"


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


@dataclass(frozen=True, slots=True)
class HigherValueCanaryTarget:
    """Exact manual-only target that never changes ordinary admission."""

    repository: str
    issue_number: int
    issue_node_id: str
    expected_base_sha: str

    def __post_init__(self) -> None:
        if self.repository != HIGHER_VALUE_CANARY_REPOSITORY:
            raise ValueError("higher-value canary repository is not the fixed private target")
        if type(self.issue_number) is not int or self.issue_number <= 0:
            raise ValueError("higher-value canary issue_number is invalid")
        if (
            not isinstance(self.issue_node_id, str)
            or not self.issue_node_id
            or len(self.issue_node_id) > 256
        ):
            raise ValueError("higher-value canary issue_node_id is invalid")
        if (
            not isinstance(self.expected_base_sha, str)
            or re.fullmatch(r"[0-9a-f]{40,64}", self.expected_base_sha) is None
        ):
            raise ValueError("higher-value canary expected_base_sha is invalid")

    def matches_issue(
        self,
        *,
        repository: str,
        issue_number: int,
        issue_node_id: str,
    ) -> bool:
        return (
            repository == self.repository
            and issue_number == self.issue_number
            and issue_node_id == self.issue_node_id
        )


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


def _matrix_payload() -> dict[str, object]:
    return {
        "schema_version": REPOSITORY_ADMISSION_MATRIX_VERSION,
        "rows": [
            {
                "repository_class": row.repository_class.value,
                "recovery_profile": (
                    None if row.recovery_profile is None else row.recovery_profile.value
                ),
                "target_readback_profile": (
                    None
                    if row.target_readback_profile is None
                    else row.target_readback_profile.value
                ),
                "admitted_by_this_release": row.admitted_by_this_release,
            }
            for row in REPOSITORY_ADMISSION_MATRIX
        ],
    }


REPOSITORY_ADMISSION_MATRIX_SHA256 = sha256(
    json.dumps(
        _matrix_payload(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
).hexdigest()


@dataclass(frozen=True, slots=True)
class RepositoryPolicyIdentity:
    """Immutable pre-claim repository policy bound to one exact Issue identity."""

    repository: str
    issue_number: int
    issue_node_id: str
    repository_class: RepositoryClass
    recovery_profile: RepositoryRecoveryProfile
    target_readback_profile: RepositoryTargetReadbackProfile
    admission_matrix_version: int
    admission_matrix_sha256: str
    policy_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.repository, str)
            or re.fullmatch(
                r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", self.repository
            )
            is None
        ):
            raise ValueError("repository policy repository is invalid")
        if type(self.issue_number) is not int or self.issue_number <= 0:
            raise ValueError("repository policy issue_number is invalid")
        if (
            not isinstance(self.issue_node_id, str)
            or not self.issue_node_id
            or len(self.issue_node_id) > 256
        ):
            raise ValueError("repository policy issue_node_id is invalid")
        if not isinstance(self.repository_class, RepositoryClass):
            raise ValueError("repository policy class is invalid")
        if not isinstance(self.recovery_profile, RepositoryRecoveryProfile):
            raise ValueError("repository recovery profile is invalid")
        if not isinstance(
            self.target_readback_profile, RepositoryTargetReadbackProfile
        ):
            raise ValueError("repository target readback profile is invalid")
        requirement = next(
            row
            for row in REPOSITORY_ADMISSION_MATRIX
            if row.repository_class is self.repository_class
        )
        if (
            requirement.recovery_profile is not self.recovery_profile
            or requirement.target_readback_profile is not self.target_readback_profile
        ):
            raise ValueError("repository policy profiles conflict with its class")
        if (
            self.admission_matrix_version != REPOSITORY_ADMISSION_MATRIX_VERSION
            or self.admission_matrix_sha256 != REPOSITORY_ADMISSION_MATRIX_SHA256
        ):
            raise ValueError("repository admission matrix identity is invalid")
        expected = repository_policy_sha256(
            repository=self.repository,
            issue_number=self.issue_number,
            issue_node_id=self.issue_node_id,
            repository_class=self.repository_class,
            recovery_profile=self.recovery_profile,
            target_readback_profile=self.target_readback_profile,
        )
        if self.policy_sha256 != expected:
            raise ValueError("repository policy digest is invalid")

    def to_mapping(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "issue_number": self.issue_number,
            "issue_node_id": self.issue_node_id,
            "repository_class": self.repository_class.value,
            "recovery_profile": self.recovery_profile.value,
            "target_readback_profile": self.target_readback_profile.value,
            "admission_matrix_version": self.admission_matrix_version,
            "admission_matrix_sha256": self.admission_matrix_sha256,
            "policy_sha256": self.policy_sha256,
        }


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


def build_repository_policy_identity(
    *,
    repository: str,
    issue_number: int,
    issue_node_id: str,
    repository_class: RepositoryClass,
    recovery_profiles: frozenset[RepositoryRecoveryProfile],
    target_readback_profiles: frozenset[RepositoryTargetReadbackProfile],
) -> RepositoryPolicyIdentity:
    """Build one admitted exact policy before the corresponding remote claim."""
    decision = evaluate_repository_admission(
        repository_class, recovery_profiles, target_readback_profiles
    )
    if not decision.admitted:
        raise ValueError(decision.code or "repository policy is not admitted")
    requirement = decision.requirement
    assert requirement.recovery_profile is not None
    assert requirement.target_readback_profile is not None
    digest = repository_policy_sha256(
        repository=repository,
        issue_number=issue_number,
        issue_node_id=issue_node_id,
        repository_class=repository_class,
        recovery_profile=requirement.recovery_profile,
        target_readback_profile=requirement.target_readback_profile,
    )
    return RepositoryPolicyIdentity(
        repository,
        issue_number,
        issue_node_id,
        repository_class,
        requirement.recovery_profile,
        requirement.target_readback_profile,
        REPOSITORY_ADMISSION_MATRIX_VERSION,
        REPOSITORY_ADMISSION_MATRIX_SHA256,
        digest,
    )


def build_higher_value_canary_policy_identity(
    *,
    target: HigherValueCanaryTarget,
    repository: str,
    issue_number: int,
    issue_node_id: str,
    repository_class: RepositoryClass,
    recovery_profiles: frozenset[RepositoryRecoveryProfile],
    target_readback_profiles: frozenset[RepositoryTargetReadbackProfile],
) -> RepositoryPolicyIdentity:
    """Build one exact manual canary policy while the normal matrix stays false."""
    if not isinstance(target, HigherValueCanaryTarget):
        raise TypeError("target must be a HigherValueCanaryTarget")
    if not target.matches_issue(
        repository=repository,
        issue_number=issue_number,
        issue_node_id=issue_node_id,
    ):
        raise ValueError("higher-value canary Issue identity conflicts with its permit")
    if repository_class is not RepositoryClass.HIGHER_VALUE:
        raise ValueError("higher-value canary repository class is invalid")
    requirement = next(
        row
        for row in REPOSITORY_ADMISSION_MATRIX
        if row.repository_class is RepositoryClass.HIGHER_VALUE
    )
    if requirement.admitted_by_this_release:
        raise ValueError("higher-value canary override is invalid after ordinary admission")
    assert requirement.recovery_profile is not None
    assert requirement.target_readback_profile is not None
    if recovery_profiles != frozenset({requirement.recovery_profile}):
        raise ValueError("higher-value canary recovery profile is not exact")
    if target_readback_profiles != frozenset({requirement.target_readback_profile}):
        raise ValueError("higher-value canary target readback profile is not exact")
    digest = repository_policy_sha256(
        repository=repository,
        issue_number=issue_number,
        issue_node_id=issue_node_id,
        repository_class=repository_class,
        recovery_profile=requirement.recovery_profile,
        target_readback_profile=requirement.target_readback_profile,
    )
    return RepositoryPolicyIdentity(
        repository,
        issue_number,
        issue_node_id,
        repository_class,
        requirement.recovery_profile,
        requirement.target_readback_profile,
        REPOSITORY_ADMISSION_MATRIX_VERSION,
        REPOSITORY_ADMISSION_MATRIX_SHA256,
        digest,
    )


def repository_policy_sha256(
    *,
    repository: str,
    issue_number: int,
    issue_node_id: str,
    repository_class: RepositoryClass,
    recovery_profile: RepositoryRecoveryProfile,
    target_readback_profile: RepositoryTargetReadbackProfile,
) -> str:
    payload = {
        "schema_version": 1,
        "repository": repository,
        "issue_number": issue_number,
        "issue_node_id": issue_node_id,
        "repository_class": repository_class.value,
        "recovery_profile": recovery_profile.value,
        "target_readback_profile": target_readback_profile.value,
        "admission_matrix_version": REPOSITORY_ADMISSION_MATRIX_VERSION,
        "admission_matrix_sha256": REPOSITORY_ADMISSION_MATRIX_SHA256,
    }
    return sha256(
        json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
