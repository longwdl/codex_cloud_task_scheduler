from __future__ import annotations

from dataclasses import replace
import unittest

from codex_dispatcher.handoffs import build_session_handoff_snapshot
from codex_dispatcher.prompt_builder import (
    ApprovedContextItem,
    build_canonical_input_snapshot,
    build_generation_delta_prompt_snapshot,
    build_generation_full_prompt_snapshot,
    build_prompt_snapshot,
    build_turn_prompt_snapshot,
)
from codex_dispatcher.task_spec import parse_task_spec
from codex_dispatcher.trackers.base import TrackerComment
from codex_dispatcher.work_items import (
    SessionGeneration,
    SessionGenerationRole,
    WorkItem,
)
from tests.test_task_spec import BODY


GENERATION_ID = "sg_" + "b" * 32
POLICY_DIGEST = "c" * 64
HEAD_SHA = "d" * 40
WORK_ITEM_ID = "wi_" + "a" * 24
PROMPT_ITEM = WorkItem.new(
    repository="owner/repo",
    issue_number=42,
    issue_node_id="I_prompt_fixture",
    base_branch="main",
    base_sha=HEAD_SHA,
    at="2026-08-22T00:00:00Z",
)
GENERATION_WORK_ITEM_ID = PROMPT_ITEM.work_item_id


def generation_arguments() -> dict[str, object]:
    return {
        "work_item_id": GENERATION_WORK_ITEM_ID,
        "session_generation_id": GENERATION_ID,
        "session_generation": 2,
        "agent_policy_digest": POLICY_DIGEST,
        "turn_number": 3,
        "issue_revision": "2026-08-22T01:02:03Z",
        "repository": "owner/repo",
        "branch": PROMPT_ITEM.task_branch,
        "input_head_sha": HEAD_SHA,
    }


def generation_handoff(inputs):
    source = SessionGeneration.new(
        work_item_id=GENERATION_WORK_ITEM_ID,
        generation_number=1,
        role=SessionGenerationRole.IMPLEMENTATION,
        start_head_sha=HEAD_SHA,
        policy_sha256="e" * 64,
        session_generation_id="sg_" + "a" * 32,
        at="2026-08-22T00:00:00Z",
    )
    return build_session_handoff_snapshot(
        work_item=PROMPT_ITEM,
        from_generation=source,
        to_session_generation_id=GENERATION_ID,
        to_generation_number=2,
        to_agent_policy_sha256=POLICY_DIGEST,
        rotation_reason="context_pressure",
        issue_revision="2026-08-22T01:02:03Z",
        issue_content_sha256=inputs.issue_content_sha256,
        task_spec_sha256=inputs.task_spec_sha256,
        approved_context_sha256=inputs.approved_context_sha256,
        acceptance_criteria=inputs.task_spec.acceptance_criteria,
        required_checks=("tests",),
        published_checkpoints=(),
        source_turn_id=None,
        source_result_status=None,
        source_result_summary=None,
        source_agent_result=None,
        created_at="2026-08-22T01:02:03Z",
    )


class PromptBuilderTests(unittest.TestCase):
    def test_includes_only_approved_context_in_stable_id_order(self) -> None:
        snapshot = build_prompt_snapshot(
            run_id="run-1", repository="owner/repo", branch="codex/one", base_sha="a" * 40,
            issue_title="Implement parser", task_spec=parse_task_spec(BODY), maintainers={"alice"},
            comments=[
                {"id": 20, "author": "alice", "body": "/codex-context second"},
                {"id": 10, "author": "mallory", "body": "/codex-context ignored"},
                {"id": 11, "author": "alice", "body": "ordinary ignored"},
                {"id": 12, "author": "alice", "body": "/codex-contextual ignored"},
                {"id": 2, "author": "alice", "body": "/codex-context first"},
            ],
        )
        self.assertEqual(("2", "20"), snapshot.included_comment_ids)
        self.assertIn("Run ID: run-1", snapshot.content)
        self.assertNotIn("ignored", snapshot.content)
        self.assertIn("Do not deploy", snapshot.content)
        self.assertLess(snapshot.content.index("Comment 2"), snapshot.content.index("Comment 20"))

    def test_hash_and_content_are_deterministic(self) -> None:
        kwargs = dict(
            run_id="r",
            repository="o/r",
            branch="b",
            base_sha="s",
            issue_title="i",
            task_spec=parse_task_spec(BODY),
        )
        first = build_prompt_snapshot(**kwargs)
        second = build_prompt_snapshot(**kwargs)
        self.assertEqual(first, second)
        self.assertEqual(64, len(first.sha256))

    def test_turn_prompt_is_stable_and_forbids_push(self) -> None:
        arguments = {
            "work_item_id": "wi_" + "a" * 24,
            "turn_number": 2,
            "issue_revision": "revision-2",
            "repository": "owner/repo",
            "branch": "codex/issue-42-aaaaaaaaaaaa",
            "input_head_sha": "b" * 40,
            "issue_title": "Continue the same task",
            "task_spec": parse_task_spec(BODY),
            "comments": (),
            "maintainers": ("alice",),
        }
        first = build_turn_prompt_snapshot(**arguments)
        second = build_turn_prompt_snapshot(**arguments)
        self.assertEqual(first, second)
        self.assertIn("Work Item ID: wi_", first.content)
        self.assertIn("Turn: 2", first.content)
        self.assertIn("Do not push, merge, deploy", first.content)
        self.assertIn("status=needs_input exactly when", first.content)
        self.assertIn("status=completed or status=blocked", first.content)
        self.assertIn("use an empty array when no files changed", first.content)
        self.assertNotIn("Cloud", first.content)

    def test_tracker_comment_dto_is_filtered_by_the_same_maintainer_rule(self) -> None:
        comment = TrackerComment(
            "IC_fixture",
            "alice",
            "/codex-context\nUse the existing parser",
            "2026-08-13T01:00:00Z",
            "2026-08-13T01:01:00Z",
        )
        snapshot = build_prompt_snapshot(
            run_id="run-1",
            repository="owner/repo",
            branch="codex/issue-1-fixture",
            base_sha="a" * 40,
            issue_title="Fixture",
            task_spec=parse_task_spec(BODY),
            comments=(comment,),
            maintainers=("alice",),
        )
        self.assertEqual(("IC_fixture",), snapshot.included_comment_ids)

    def test_canonical_inputs_are_ordered_and_revision_independent(self) -> None:
        spec = parse_task_spec(BODY)
        comments = (
            {"id": "IC_20", "author": "alice", "body": "/codex-context second"},
            {"id": "IC_10", "author": "alice", "body": "/codex-context first"},
            {"id": "IC_00", "author": "mallory", "body": "/codex-context ignored"},
        )
        first = build_canonical_input_snapshot(
            issue_title="Implement parser",
            task_spec=spec,
            comments=comments,
            maintainers=("alice",),
        )
        reordered = build_canonical_input_snapshot(
            issue_title="Implement parser",
            task_spec=spec,
            comments=tuple(reversed(comments)),
            maintainers=("alice",),
        )

        self.assertEqual(first, reordered)
        self.assertEqual(("IC_10", "IC_20"), first.approved_comment_ids)
        self.assertEqual(
            ("/codex-context first", "/codex-context second"),
            tuple(item.body for item in first.approved_items),
        )
        # Issue revision and labels are deliberately not inputs to this pure snapshot.
        self.assertFalse(hasattr(first, "issue_revision"))
        self.assertFalse(hasattr(first, "labels"))

    def test_canonical_digests_detect_semantic_input_edits(self) -> None:
        spec = parse_task_spec(BODY)
        comment = {"id": "IC_1", "author": "alice", "body": "/codex-context original"}
        baseline = build_canonical_input_snapshot(
            issue_title="Implement parser",
            task_spec=spec,
            comments=(comment,),
            maintainers=("alice",),
        )
        title_edit = build_canonical_input_snapshot(
            issue_title="Implement safer parser",
            task_spec=spec,
            comments=(comment,),
            maintainers=("alice",),
        )
        spec_edit = build_canonical_input_snapshot(
            issue_title="Implement parser",
            task_spec=replace(spec, objective=spec.objective + " now"),
            comments=(comment,),
            maintainers=("alice",),
        )
        comment_edit = build_canonical_input_snapshot(
            issue_title="Implement parser",
            task_spec=spec,
            comments=(replace_comment_body(comment, "/codex-context edited"),),
            maintainers=("alice",),
        )

        self.assertEqual(baseline.task_spec_sha256, title_edit.task_spec_sha256)
        self.assertNotEqual(baseline.issue_content_sha256, title_edit.issue_content_sha256)
        self.assertNotEqual(baseline.task_spec_sha256, spec_edit.task_spec_sha256)
        self.assertNotEqual(baseline.issue_content_sha256, spec_edit.issue_content_sha256)
        self.assertEqual(
            baseline.issue_content_sha256,
            comment_edit.issue_content_sha256,
        )
        self.assertNotEqual(
            baseline.approved_context_sha256,
            comment_edit.approved_context_sha256,
        )

    def test_generation_full_prompt_contains_complete_canonical_context(self) -> None:
        inputs = build_canonical_input_snapshot(
            issue_title="Implement parser",
            task_spec=parse_task_spec(BODY),
            comments=(
                {"id": "IC_2", "author": "alice", "body": "/codex-context second"},
                {"id": "IC_1", "author": "alice", "body": "/codex-context first"},
                {"id": "IC_3", "author": "mallory", "body": "/codex-context ignored"},
            ),
            maintainers=("alice",),
        )
        prompt = build_generation_full_prompt_snapshot(
            **generation_arguments(),
            inputs=inputs,
            handoff=generation_handoff(inputs),
        )

        self.assertEqual(("IC_1", "IC_2"), prompt.included_comment_ids)
        self.assertIn(f"Session Generation ID: {GENERATION_ID}", prompt.content)
        self.assertIn("Session Generation: 2", prompt.content)
        self.assertIn(f"Agent policy digest: {POLICY_DIGEST}", prompt.content)
        self.assertIn("## Issue title\nImplement parser", prompt.content)
        self.assertIn("## Task snapshot\n### 目标", prompt.content)
        self.assertIn("/codex-context first", prompt.content)
        self.assertIn("/codex-context second", prompt.content)
        self.assertNotIn("ignored", prompt.content)
        self.assertIn(inputs.issue_content_sha256, prompt.content)
        self.assertIn("Do not push, merge, deploy", prompt.content)
        self.assertIn("Fresh-session bootstrap", prompt.content)
        self.assertIn("UNTRUSTED handoff advisory", prompt.content)

    def test_generation_delta_contains_only_explicit_new_context(self) -> None:
        inputs = build_canonical_input_snapshot(
            issue_title="Full title must not repeat",
            task_spec=parse_task_spec(BODY),
            comments=(
                {"id": "IC_old", "author": "alice", "body": "/codex-context old body"},
                {"id": "IC_new", "author": "alice", "body": "/codex-context new body"},
            ),
            maintainers=("alice",),
        )
        new_item = next(
            item for item in inputs.approved_items if item.comment_id == "IC_new"
        )
        prompt = build_generation_delta_prompt_snapshot(
            **generation_arguments(),
            inputs=inputs,
            prior_status="needs_input",
            prior_summary="Waiting for the parser format decision",
            new_approved_items=(new_item,),
        )

        self.assertEqual(("IC_new",), prompt.included_comment_ids)
        self.assertIn("Status: needs_input", prompt.content)
        self.assertIn("Waiting for the parser format decision", prompt.content)
        self.assertIn("/codex-context new body", prompt.content)
        self.assertNotIn("/codex-context old body", prompt.content)
        self.assertNotIn("Full title must not repeat", prompt.content)
        self.assertNotIn("## Task snapshot", prompt.content)
        self.assertNotIn("### 目标", prompt.content)
        self.assertIn(inputs.task_spec_sha256, prompt.content)
        self.assertIn(inputs.issue_content_sha256, prompt.content)
        self.assertIn(inputs.approved_context_sha256, prompt.content)
        self.assertIn("Do not push, merge, deploy", prompt.content)

        with self.assertRaisesRegex(ValueError, "exact items"):
            build_generation_delta_prompt_snapshot(
                **generation_arguments(),
                inputs=inputs,
                prior_status="completed",
                prior_summary="Done",
                new_approved_items=(
                    ApprovedContextItem("IC_new", "/codex-context altered"),
                ),
            )

    def test_fresh_audit_role_adds_independent_completion_contract(self) -> None:
        inputs = build_canonical_input_snapshot(
            issue_title="Implement parser",
            task_spec=parse_task_spec(BODY),
        )
        prompt = build_generation_full_prompt_snapshot(
            **generation_arguments(),
            session_role=SessionGenerationRole.AUDIT,
            inputs=inputs,
            handoff=generation_handoff(inputs),
        )

        self.assertIn("Session Role: audit", prompt.content)
        self.assertIn("## Fresh final audit", prompt.content)
        self.assertIn("Do not inherit the implementation session's confidence", prompt.content)
        self.assertIn(
            "Because Audit is read-only, return changed_paths=[]", prompt.content
        )
        self.assertIn(
            "not paths observed in the completion candidate's existing diff",
            prompt.content,
        )

    def test_existing_v1_turn_prompt_remains_byte_for_byte_unchanged(self) -> None:
        snapshot = build_turn_prompt_snapshot(
            work_item_id=WORK_ITEM_ID,
            turn_number=2,
            issue_revision="revision-2",
            repository="owner/repo",
            branch="codex/issue-42-aaaaaaaaaaaa",
            input_head_sha="b" * 40,
            issue_title="Continue the same task",
            task_spec=parse_task_spec(BODY),
            comments=(),
            maintainers=("alice",),
        )

        self.assertEqual(
            "164f0da2552b89f2222c5193191397e52542059f309225852aaa15d7d2055102",
            snapshot.sha256,
        )


def replace_comment_body(comment: dict[str, object], body: str) -> dict[str, object]:
    return {**comment, "body": body}


if __name__ == "__main__":
    unittest.main()
