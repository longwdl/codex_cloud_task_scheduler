from __future__ import annotations

import unittest

from codex_dispatcher.repository_admission import (
    HIGHER_VALUE_CANARY_REPOSITORY,
    REPOSITORY_ADMISSION_MATRIX,
    REPOSITORY_ADMISSION_MATRIX_SHA256,
    HigherValueCanaryTarget,
    RepositoryClass,
    RepositoryRecoveryProfile,
    RepositoryTargetReadbackProfile,
    build_higher_value_canary_policy_identity,
    build_repository_policy_identity,
    evaluate_repository_admission,
)


class RepositoryAdmissionTests(unittest.TestCase):
    def test_matrix_covers_every_class_once(self) -> None:
        self.assertEqual(
            set(RepositoryClass),
            {row.repository_class for row in REPOSITORY_ADMISSION_MATRIX},
        )
        self.assertEqual(
            len(RepositoryClass),
            len({row.repository_class for row in REPOSITORY_ADMISSION_MATRIX}),
        )

    def test_only_exact_fixture_profiles_are_currently_admitted(self) -> None:
        admitted = evaluate_repository_admission(
            RepositoryClass.FIXTURE,
            frozenset({RepositoryRecoveryProfile.FIXTURE_LIVE_V1}),
            frozenset({RepositoryTargetReadbackProfile.FIXTURE_EXACT_V1}),
        )
        wrong_readback = evaluate_repository_admission(
            RepositoryClass.FIXTURE,
            frozenset({RepositoryRecoveryProfile.FIXTURE_LIVE_V1}),
            frozenset({RepositoryTargetReadbackProfile.HIGHER_VALUE_EXACT_V1}),
        )
        higher_value = evaluate_repository_admission(
            RepositoryClass.HIGHER_VALUE,
            frozenset({RepositoryRecoveryProfile.HIGHER_VALUE_LIVE_V1}),
            frozenset({RepositoryTargetReadbackProfile.HIGHER_VALUE_EXACT_V1}),
        )

        self.assertTrue(admitted.admitted)
        self.assertIsNone(admitted.code)
        self.assertEqual(
            "repository_target_readback_profile_mismatch", wrong_readback.code
        )
        self.assertFalse(higher_value.admitted)
        self.assertEqual("repository_class_not_admitted", higher_value.code)

    def test_unclassified_and_missing_runtime_profiles_fail_closed(self) -> None:
        unclassified = evaluate_repository_admission(
            RepositoryClass.UNCLASSIFIED, frozenset(), frozenset()
        )
        missing_profiles = evaluate_repository_admission(
            RepositoryClass.FIXTURE, frozenset(), frozenset()
        )

        self.assertEqual("repository_class_unclassified", unclassified.code)
        self.assertEqual(
            "repository_recovery_profile_mismatch", missing_profiles.code
        )

    def test_preclaim_policy_freezes_exact_issue_and_matrix_identity(self) -> None:
        first = build_repository_policy_identity(
            repository="owner/repo",
            issue_number=42,
            issue_node_id="I_kwDOFixture42",
            repository_class=RepositoryClass.FIXTURE,
            recovery_profiles=frozenset(
                {RepositoryRecoveryProfile.FIXTURE_LIVE_V1}
            ),
            target_readback_profiles=frozenset(
                {RepositoryTargetReadbackProfile.FIXTURE_EXACT_V1}
            ),
        )
        second = build_repository_policy_identity(
            repository="owner/repo",
            issue_number=42,
            issue_node_id="I_kwDOFixture42-replaced",
            repository_class=RepositoryClass.FIXTURE,
            recovery_profiles=frozenset(
                {RepositoryRecoveryProfile.FIXTURE_LIVE_V1}
            ),
            target_readback_profiles=frozenset(
                {RepositoryTargetReadbackProfile.FIXTURE_EXACT_V1}
            ),
        )

        self.assertEqual(REPOSITORY_ADMISSION_MATRIX_SHA256, first.admission_matrix_sha256)
        self.assertNotEqual(first.policy_sha256, second.policy_sha256)
        self.assertEqual("fixture-live-v1", first.recovery_profile.value)
        self.assertEqual("fixture-exact-v1", first.target_readback_profile.value)

    def test_manual_canary_policy_is_exact_without_admitting_higher_value(self) -> None:
        target = HigherValueCanaryTarget(
            HIGHER_VALUE_CANARY_REPOSITORY,
            7,
            "I_kwDOHigherValue7",
            "a" * 40,
        )

        policy = build_higher_value_canary_policy_identity(
            target=target,
            repository=target.repository,
            issue_number=target.issue_number,
            issue_node_id=target.issue_node_id,
            repository_class=RepositoryClass.HIGHER_VALUE,
            recovery_profiles=frozenset(
                {RepositoryRecoveryProfile.HIGHER_VALUE_LIVE_V1}
            ),
            target_readback_profiles=frozenset(
                {RepositoryTargetReadbackProfile.HIGHER_VALUE_EXACT_V1}
            ),
        )
        ordinary = evaluate_repository_admission(
            RepositoryClass.HIGHER_VALUE,
            frozenset({RepositoryRecoveryProfile.HIGHER_VALUE_LIVE_V1}),
            frozenset({RepositoryTargetReadbackProfile.HIGHER_VALUE_EXACT_V1}),
        )

        self.assertIs(RepositoryClass.HIGHER_VALUE, policy.repository_class)
        self.assertFalse(ordinary.admitted)
        self.assertEqual("repository_class_not_admitted", ordinary.code)
        with self.assertRaisesRegex(ValueError, "Issue identity"):
            build_higher_value_canary_policy_identity(
                target=target,
                repository=target.repository,
                issue_number=8,
                issue_node_id=target.issue_node_id,
                repository_class=RepositoryClass.HIGHER_VALUE,
                recovery_profiles=frozenset(
                    {RepositoryRecoveryProfile.HIGHER_VALUE_LIVE_V1}
                ),
                target_readback_profiles=frozenset(
                    {RepositoryTargetReadbackProfile.HIGHER_VALUE_EXACT_V1}
                ),
            )
