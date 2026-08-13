from __future__ import annotations

import unittest

from codex_dispatcher.prompt_builder import build_prompt_snapshot
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


if __name__ == "__main__":
    unittest.main()
