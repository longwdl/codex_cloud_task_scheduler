from __future__ import annotations

import unittest

from codex_dispatcher.prompt_builder import build_prompt_snapshot, build_turn_prompt_snapshot
from codex_dispatcher.task_spec import parse_task_spec
from codex_dispatcher.trackers.base import TrackerComment
from tests.test_task_spec import BODY


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


if __name__ == "__main__":
    unittest.main()
