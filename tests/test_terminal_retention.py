from __future__ import annotations

import unittest

from codex_dispatcher.terminal_retention import terminal_branch_request_sha256


class TerminalRetentionTests(unittest.TestCase):
    def test_request_digest_is_stable_and_binds_exact_identity(self) -> None:
        arguments = {
            "work_item_id": "wi_" + "1" * 24,
            "repository": "owner/repo",
            "branch_name": "codex/issue-12-abcdef123456",
            "expected_head_sha": "a" * 40,
        }
        first = terminal_branch_request_sha256(**arguments)
        self.assertEqual(first, terminal_branch_request_sha256(**arguments))
        self.assertNotEqual(
            first,
            terminal_branch_request_sha256(
                **dict(arguments, expected_head_sha="b" * 40)
            ),
        )


if __name__ == "__main__":
    unittest.main()
