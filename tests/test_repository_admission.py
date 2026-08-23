from __future__ import annotations

import unittest

from codex_dispatcher.repository_admission import (
    REPOSITORY_ADMISSION_MATRIX,
    RepositoryClass,
    RepositoryRecoveryProfile,
    RepositoryTargetReadbackProfile,
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
