from __future__ import annotations

import unittest

from codex_dispatcher.prompt_builder import build_prompt_snapshot, build_turn_prompt_snapshot
from codex_dispatcher.task_spec import parse_task_spec
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
        self.assertNotIn("Cloud", first.content)


if __name__ == "__main__":
    unittest.main()
