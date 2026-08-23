from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
import unittest

from codex_dispatcher.ci_evidence import (
    ActionsEvidenceSnapshot,
    ActionsRunEvidence,
    RequiredCheckEvidence,
)
from codex_dispatcher.handoffs import (
    PublishedCheckpoint,
    SessionHandoffSnapshot,
    build_session_handoff_snapshot,
)
from codex_dispatcher.prompt_builder import build_canonical_input_snapshot
from codex_dispatcher.runner_protocol import parse_agent_result
from codex_dispatcher.task_spec import parse_acceptance_criteria, parse_task_spec
from codex_dispatcher.work_items import (
    SessionGeneration,
    SessionGenerationRole,
    WorkItem,
)
from tests.test_task_spec import BODY


class SessionHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.item = WorkItem.new(
            repository="owner/repo",
            issue_number=42,
            issue_node_id="I_handoff_fixture",
            base_branch="main",
            base_sha="a" * 40,
            at="2026-08-22T00:00:00Z",
        )
        self.source = SessionGeneration.new(
            work_item_id=self.item.work_item_id,
            generation_number=1,
            role=SessionGenerationRole.IMPLEMENTATION,
            start_head_sha=self.item.base_sha,
            policy_sha256="b" * 64,
            session_generation_id="sg_" + "1" * 32,
            at="2026-08-22T00:00:00Z",
        )
        self.item = replace(self.item, last_published_sha="d" * 40)
        self.source = replace(self.source, last_published_sha="d" * 40)
        self.inputs = build_canonical_input_snapshot(
            issue_title="Implement fixture",
            task_spec=parse_task_spec(BODY),
        )
        self.result = parse_agent_result(
            json.dumps(
                {
                    "schema_version": 2,
                    "status": "completed",
                    "summary": "Unverified claim from the old session",
                    "acceptance": [],
                    "remaining_work": [],
                    "needs_input": [],
                    "tests": [{"name": "unit", "status": "passed"}],
                    "changed_paths": ["src/reported.py"],
                    "blocker_code": None,
                    "next_step": "Inspect the integration fixture",
                }
            )
        )

    def build(self) -> SessionHandoffSnapshot:
        return build_session_handoff_snapshot(
            work_item=self.item,
            from_generation=self.source,
            to_session_generation_id="sg_" + "2" * 32,
            to_generation_number=2,
            to_agent_policy_sha256="c" * 64,
            rotation_reason="context_pressure",
            issue_revision="2026-08-22T01:00:00Z",
            issue_content_sha256=self.inputs.issue_content_sha256,
            task_spec_sha256=self.inputs.task_spec_sha256,
            approved_context_sha256=self.inputs.approved_context_sha256,
            acceptance_criteria=self.inputs.task_spec.acceptance_criteria,
            required_checks=("unit-tests", "integration-tests"),
            published_checkpoints=(
                PublishedCheckpoint(
                    head_sha="d" * 40,
                    previous_sha="a" * 40,
                    bundle_sha256="e" * 64,
                    changed_paths=("src/verified.py", "tests/test_verified.py"),
                ),
            ),
            source_turn_id="turn_" + "3" * 32,
            source_result_status="completed",
            source_result_summary=self.result.summary,
            source_agent_result=self.result,
            created_at="2026-08-22T01:00:00Z",
            handoff_id="handoff_" + "4" * 32,
        )

    def test_trusted_facts_never_promote_agent_claims(self) -> None:
        snapshot = self.build()

        trusted = snapshot.trusted_facts
        advisory = snapshot.untrusted_advisory
        self.assertEqual("unverified", trusted["acceptance"]["status"])
        self.assertEqual(
            ["src/verified.py", "tests/test_verified.py"],
            trusted["git"]["verified_changed_paths"],
        )
        self.assertNotIn(self.result.summary, snapshot.trusted_facts_json)
        self.assertNotIn("src/reported.py", snapshot.trusted_facts_json)
        self.assertEqual("untrusted_advisory", advisory["classification"])
        self.assertEqual(self.result.summary, advisory["previous_result"]["summary"])
        self.assertEqual(self.result.next_step, advisory["recommended_next_action"])
        self.assertEqual(snapshot, self.build())

    def test_canonical_content_and_digest_tampering_fail_closed(self) -> None:
        snapshot = self.build()
        with self.assertRaisesRegex(ValueError, "canonical JSON"):
            replace(snapshot, trusted_facts_json="{\"schema_version\": 1}")
        with self.assertRaisesRegex(ValueError, "handoff_sha256"):
            replace(snapshot, handoff_sha256="f" * 64)

    def test_structured_acceptance_embeds_exact_actions_evidence(self) -> None:
        run = ActionsRunEvidence(
            name="unit-tests",
            workflow_id=200,
            run_id=100,
            run_attempt=1,
            repository=self.item.repository,
            head_repository=self.item.repository,
            head_branch=self.item.task_branch,
            head_sha="d" * 40,
            event="pull_request",
            status="completed",
            conclusion="success",
            created_at="2026-08-22T00:30:00Z",
            updated_at="2026-08-22T00:31:00Z",
            html_url="https://github.com/owner/repo/actions/runs/100",
        )
        actions = ActionsEvidenceSnapshot(
            repository=self.item.repository,
            task_branch=self.item.task_branch,
            head_sha="d" * 40,
            remote_ref_sha="d" * 40,
            observed_at="2026-08-22T00:32:00Z",
            required_checks=(
                RequiredCheckEvidence("unit-tests", run.check_status, run),
            ),
        )
        criteria = """- [AC-1] required-check: unit-tests
- [AC-2] changed-paths-within-allowed
- [AC-3] task-head-published
""".strip()

        snapshot = build_session_handoff_snapshot(
            work_item=self.item,
            from_generation=self.source,
            to_session_generation_id="sg_" + "2" * 32,
            to_generation_number=2,
            to_agent_policy_sha256="c" * 64,
            rotation_reason="context_pressure",
            issue_revision="2026-08-22T01:00:00Z",
            issue_content_sha256=self.inputs.issue_content_sha256,
            task_spec_sha256=self.inputs.task_spec_sha256,
            approved_context_sha256=self.inputs.approved_context_sha256,
            acceptance_criteria=criteria,
            required_checks=("unit-tests",),
            published_checkpoints=(
                PublishedCheckpoint(
                    head_sha="d" * 40,
                    previous_sha="a" * 40,
                    bundle_sha256="e" * 64,
                    changed_paths=("src/verified.py",),
                ),
            ),
            source_turn_id=None,
            source_result_status=None,
            source_result_summary=None,
            source_agent_result=None,
            created_at="2026-08-22T01:00:00Z",
            acceptance_items=parse_acceptance_criteria(criteria),
            allowed_paths=("src",),
            actions_evidence=actions,
        )

        trusted = snapshot.trusted_facts
        self.assertEqual(2, trusted["schema_version"])
        self.assertEqual("passed", trusted["acceptance"]["status"])
        self.assertEqual(
            [{"name": "unit-tests", "status": "passed"}],
            trusted["required_checks"],
        )
        self.assertEqual("github_actions", trusted["ci_evidence"]["provider"])
        self.assertEqual(100, trusted["ci_evidence"]["runs"][0]["run_id"])
        self.assertEqual([], trusted["remaining_work"])

    def test_schema_one_handoffs_remain_readable_after_schema_two_writer(self) -> None:
        current = self.build()
        trusted = current.trusted_facts
        trusted.pop("ci_evidence")
        trusted["acceptance"] = {
            "criteria_sha256": sha256(
                self.inputs.task_spec.acceptance_criteria.encode("utf-8")
            ).hexdigest(),
            "evidence": [],
            "status": "unverified",
        }
        trusted["provenance"] = "dispatcher_git_state"
        trusted["schema_version"] = 1
        advisory = current.untrusted_advisory
        trusted_json = json.dumps(
            trusted, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        digest_payload = json.dumps(
            {"trusted_facts": trusted, "untrusted_advisory": advisory},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

        restored = replace(
            current,
            trusted_facts_json=trusted_json,
            handoff_sha256=sha256(digest_payload.encode("utf-8")).hexdigest(),
        )

        self.assertEqual(1, restored.trusted_facts["schema_version"])


if __name__ == "__main__":
    unittest.main()
