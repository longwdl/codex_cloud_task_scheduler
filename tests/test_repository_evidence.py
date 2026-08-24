from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codex_dispatcher.repository_admission import (
    REPOSITORY_ADMISSION_MATRIX_SHA256,
    REPOSITORY_ADMISSION_MATRIX_VERSION,
    RepositoryClass,
    RepositoryPolicyIdentity,
    RepositoryRecoveryProfile,
    RepositoryTargetReadbackProfile,
    build_repository_policy_identity,
    repository_policy_sha256,
)
from codex_dispatcher.repository_evidence import (
    evaluate_repository_recovery,
    evaluate_repository_target_readback,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.trackers.base import PullRequest, PullRequestState
from codex_dispatcher.work_items import WorkItem
from tests.test_ssh_dispatch_planning import BASE_SHA, claimed_task


CREATED_AT = "2026-08-24T00:00:00+00:00"


def fixture_policy(issue_number: int = 42) -> RepositoryPolicyIdentity:
    return build_repository_policy_identity(
        repository="owner/repo",
        issue_number=issue_number,
        issue_node_id=f"I_kwDOFixture{issue_number}",
        repository_class=RepositoryClass.FIXTURE,
        recovery_profiles=frozenset({RepositoryRecoveryProfile.FIXTURE_LIVE_V1}),
        target_readback_profiles=frozenset(
            {RepositoryTargetReadbackProfile.FIXTURE_EXACT_V1}
        ),
    )


def higher_value_policy(issue_number: int = 42) -> RepositoryPolicyIdentity:
    digest = repository_policy_sha256(
        repository="owner/repo",
        issue_number=issue_number,
        issue_node_id=f"I_kwDOFixture{issue_number}",
        repository_class=RepositoryClass.HIGHER_VALUE,
        recovery_profile=RepositoryRecoveryProfile.HIGHER_VALUE_LIVE_V1,
        target_readback_profile=(
            RepositoryTargetReadbackProfile.HIGHER_VALUE_EXACT_V1
        ),
    )
    return RepositoryPolicyIdentity(
        repository="owner/repo",
        issue_number=issue_number,
        issue_node_id=f"I_kwDOFixture{issue_number}",
        repository_class=RepositoryClass.HIGHER_VALUE,
        recovery_profile=RepositoryRecoveryProfile.HIGHER_VALUE_LIVE_V1,
        target_readback_profile=(
            RepositoryTargetReadbackProfile.HIGHER_VALUE_EXACT_V1
        ),
        admission_matrix_version=REPOSITORY_ADMISSION_MATRIX_VERSION,
        admission_matrix_sha256=REPOSITORY_ADMISSION_MATRIX_SHA256,
        policy_sha256=digest,
    )


def work_item(policy: RepositoryPolicyIdentity) -> WorkItem:
    return WorkItem.new(
        repository=policy.repository,
        issue_number=policy.issue_number,
        issue_node_id=policy.issue_node_id,
        base_branch="main",
        base_sha=BASE_SHA,
        repository_policy_sha256=policy.policy_sha256,
        at=CREATED_AT,
    )


def empty_recovery_ledgers() -> dict[str, object]:
    return {
        "schema_version": 2,
        "slack_deliveries": [],
        "actions_completion_gate": None,
        "runner_terminal_storage": {"archive": None, "absence": None},
        "discard_request": None,
        "disposition": None,
        "terminal_github_closures": [],
    }


class RepositoryEvidenceTests(unittest.TestCase):
    def test_fixture_recovery_and_exact_target_are_deterministic(self) -> None:
        policy = fixture_policy()
        item = work_item(policy)
        task = replace(claimed_task(), branch_name=item.task_branch)

        receipt = evaluate_repository_recovery(
            action="resume_preparation",
            policy=policy,
            task=task,
            work_item=item,
            turn=None,
            pull_request=None,
            planning_code=None,
            created_at=CREATED_AT,
        )
        repeated = evaluate_repository_recovery(
            action="resume_preparation",
            policy=policy,
            task=task,
            work_item=item,
            turn=None,
            pull_request=None,
            planning_code=None,
            created_at="2026-08-24T00:01:00+00:00",
        )
        verdict = evaluate_repository_target_readback(
            action="resume_preparation",
            policy=policy,
            task=task,
            work_item=item,
            turn=None,
            pull_request=None,
            ledger_evidence=empty_recovery_ledgers(),
            created_at=CREATED_AT,
        )

        self.assertEqual("allowed", receipt.decision)
        self.assertEqual(receipt.receipt_sha256, repeated.receipt_sha256)
        self.assertEqual("passed", verdict.status)

    def test_higher_value_profile_blocks_destructive_terminal_cleanup(self) -> None:
        policy = higher_value_policy()
        item = work_item(policy)
        receipt = evaluate_repository_recovery(
            action="delete_terminal_branch",
            policy=policy,
            task=replace(claimed_task(), branch_name=item.task_branch),
            work_item=item,
            turn=None,
            pull_request=None,
            planning_code=None,
            created_at=CREATED_AT,
        )

        self.assertEqual("blocked", receipt.decision)
        self.assertEqual("repository_recovery_action_not_allowed", receipt.code)

    def test_higher_value_profile_allows_exact_disposition_archive_only(self) -> None:
        policy = higher_value_policy()
        item = work_item(policy)
        task = replace(claimed_task(), branch_name=item.task_branch)

        for action in (
            "record_work_item_disposition",
            "archive_disposed_work_item",
            "reconcile_work_item_archive",
        ):
            with self.subTest(action=action):
                receipt = evaluate_repository_recovery(
                    action=action,
                    policy=policy,
                    task=task,
                    work_item=item,
                    turn=None,
                    pull_request=None,
                    planning_code=None,
                    created_at=CREATED_AT,
                )
                self.assertEqual("allowed", receipt.decision)
                self.assertIsNone(receipt.code)

    def test_readback_blocks_a_pull_request_target_mismatch(self) -> None:
        policy = fixture_policy()
        item = replace(work_item(policy), pr_number=7, last_published_sha="d" * 40)
        task = replace(claimed_task(), branch_name=item.task_branch)
        wrong = PullRequest(
            number=7,
            url="https://github.com/owner/repo/pull/7",
            branch_name=item.task_branch,
            title="Fixture",
            is_draft=True,
            base_branch="main",
            state=PullRequestState.OPEN,
            is_cross_repository=False,
            head_sha="e" * 40,
        )

        verdict = evaluate_repository_target_readback(
            action="resume_publication",
            policy=policy,
            task=task,
            work_item=item,
            turn=None,
            pull_request=wrong,
            ledger_evidence=empty_recovery_ledgers(),
            created_at=CREATED_AT,
        )

        self.assertEqual("blocked", verdict.status)
        self.assertEqual("target_pull_request_conflict", verdict.code)

    def test_readback_blocks_untyped_cross_work_item_ledger_evidence(self) -> None:
        policy = fixture_policy()
        item = work_item(policy)
        evidence = empty_recovery_ledgers()
        evidence["slack_deliveries"] = [
            {
                "deduplication_key": "slack:wrong:root",
                "work_item_id": "wi_" + "0" * 24,
                "payload_sha256": "0" * 64,
                "state": "delivered",
            }
        ]

        verdict = evaluate_repository_target_readback(
            action="resume_preparation",
            policy=policy,
            task=replace(claimed_task(), branch_name=item.task_branch),
            work_item=item,
            turn=None,
            pull_request=None,
            ledger_evidence=evidence,
            created_at=CREATED_AT,
        )

        self.assertEqual("blocked", verdict.status)
        self.assertEqual("target_slack_ledger_invalid", verdict.code)

    def test_policy_receipt_and_verdict_ledgers_are_immutable_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with StateStore(Path(temp_dir) / "state.db") as store:
                store.migrate()
                policy = fixture_policy()
                item = work_item(policy)
                task = replace(claimed_task(), branch_name=item.task_branch)
                store.prepare_repository_claim_policy(policy, created_at=CREATED_AT)
                store.create_work_item(item)
                self.assertEqual(policy, store.get_work_item_repository_policy(item.work_item_id))

                receipt = evaluate_repository_recovery(
                    action="resume_preparation",
                    policy=policy,
                    task=task,
                    work_item=item,
                    turn=None,
                    pull_request=None,
                    planning_code=None,
                    created_at=CREATED_AT,
                )
                verdict = evaluate_repository_target_readback(
                    action="resume_preparation",
                    policy=policy,
                    task=task,
                    work_item=item,
                    turn=None,
                    pull_request=None,
                    ledger_evidence=empty_recovery_ledgers(),
                    created_at=CREATED_AT,
                )
                self.assertEqual(receipt, store.record_repository_recovery_receipt(receipt))
                self.assertEqual(verdict, store.record_repository_target_readback_verdict(verdict))
                self.assertEqual(1, len(store.list_repository_recovery_receipts()))
                self.assertEqual(1, len(store.list_repository_target_readback_verdicts()))

                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    store._connection.execute(
                        "UPDATE repository_claim_policies SET issue_node_id = 'changed'"
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "cannot be deleted"):
                    store._connection.execute("DELETE FROM repository_claim_policies")
