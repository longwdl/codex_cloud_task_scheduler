from __future__ import annotations

import unittest

from codex_dispatcher.acceptance_evaluator import AcceptanceStatus, evaluate_acceptance
from codex_dispatcher.ci_evidence import (
    ActionsEvidenceSnapshot,
    ActionsRunEvidence,
    RequiredCheckEvidence,
)
from codex_dispatcher.task_spec import parse_acceptance_criteria


REPOSITORY = "owner/repo"
BRANCH = "codex/issue-42-aaaaaaaaaaaa"
HEAD = "a" * 40


def evidence(*, conclusion: str = "success") -> ActionsEvidenceSnapshot:
    run = ActionsRunEvidence(
        name="tests",
        workflow_id=200,
        run_id=100,
        run_attempt=1,
        repository=REPOSITORY,
        head_repository=REPOSITORY,
        head_branch=BRANCH,
        head_sha=HEAD,
        event="pull_request",
        status="completed",
        conclusion=conclusion,
        created_at="2026-08-22T08:00:00Z",
        updated_at="2026-08-22T08:01:00Z",
        html_url=f"https://github.com/{REPOSITORY}/actions/runs/100",
    )
    return ActionsEvidenceSnapshot(
        repository=REPOSITORY,
        task_branch=BRANCH,
        head_sha=HEAD,
        remote_ref_sha=HEAD,
        observed_at="2026-08-22T08:02:00Z",
        required_checks=(
            RequiredCheckEvidence("tests", run.check_status, run),
        ),
    )


class AcceptanceEvaluatorTests(unittest.TestCase):
    def test_all_supported_predicates_pass_from_trusted_evidence(self) -> None:
        result = evaluate_acceptance(
            criteria=parse_acceptance_criteria(
                """- [AC-1] required-check: tests
- [AC-2] changed-paths-within-allowed
- [AC-3] task-head-published
"""
            ),
            configured_required_checks=("tests",),
            allowed_paths=("src", "tests"),
            publication_evidence_complete=True,
            verified_changed_paths=("src/module.py", "tests/test_module.py"),
            head_was_published=True,
            actions_evidence=evidence(),
        )

        self.assertEqual(AcceptanceStatus.PASSED, result.status)
        self.assertTrue(
            all(item.status is AcceptanceStatus.PASSED for item in result.criteria)
        )
        self.assertEqual(
            ("github-actions-run:100:attempt:1",),
            result.criteria[0].evidence_refs,
        )

    def test_manual_or_missing_provider_evidence_stays_unverified(self) -> None:
        result = evaluate_acceptance(
            criteria=parse_acceptance_criteria(
                """- [AC-1] required-check: tests
- [ ] 人工验证输出
"""
            ),
            configured_required_checks=("tests",),
            allowed_paths=("src",),
            publication_evidence_complete=True,
            verified_changed_paths=(),
            head_was_published=False,
            actions_evidence=None,
        )

        self.assertEqual(AcceptanceStatus.UNVERIFIED, result.status)
        self.assertEqual(
            ["actions_evidence_unavailable", "manual_evidence_required"],
            [item.reason for item in result.criteria],
        )

    def test_failed_check_overrides_unverified_and_unconfigured_name(self) -> None:
        failed = evaluate_acceptance(
            criteria=parse_acceptance_criteria(
                """- [AC-1] required-check: tests
- [ ] 人工验证输出
"""
            ),
            configured_required_checks=("tests",),
            allowed_paths=("src",),
            publication_evidence_complete=True,
            verified_changed_paths=(),
            head_was_published=True,
            actions_evidence=evidence(conclusion="failure"),
        )
        unconfigured = evaluate_acceptance(
            criteria=parse_acceptance_criteria("- [AC-1] required-check: attacker-name"),
            configured_required_checks=("tests",),
            allowed_paths=("src",),
            publication_evidence_complete=True,
            verified_changed_paths=(),
            head_was_published=True,
            actions_evidence=evidence(),
        )

        self.assertEqual(AcceptanceStatus.FAILED, failed.status)
        self.assertEqual(AcceptanceStatus.UNVERIFIED, unconfigured.status)
        self.assertEqual(
            "required_check_not_configured", unconfigured.criteria[0].reason
        )

    def test_incomplete_publication_ledger_never_proves_path_predicate(self) -> None:
        result = evaluate_acceptance(
            criteria=parse_acceptance_criteria(
                "- [AC-1] changed-paths-within-allowed"
            ),
            configured_required_checks=("tests",),
            allowed_paths=("src",),
            publication_evidence_complete=False,
            verified_changed_paths=("src/module.py",),
            head_was_published=True,
            actions_evidence=evidence(),
        )

        self.assertEqual(AcceptanceStatus.UNVERIFIED, result.status)
        self.assertEqual("publication_evidence_incomplete", result.criteria[0].reason)


if __name__ == "__main__":
    unittest.main()
