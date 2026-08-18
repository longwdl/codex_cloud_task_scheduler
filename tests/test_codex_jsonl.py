from __future__ import annotations

import json
import unittest

from codex_dispatcher.codex_jsonl import (
    CodexJsonlError,
    CodexTerminalStatus,
    parse_codex_jsonl,
)


SESSION = "123e4567-e89b-12d3-a456-426614174000"


def jsonl(*events: object) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


class CodexJsonlTests(unittest.TestCase):
    def test_parses_one_session_terminal_event_and_redacted_agent_message(self) -> None:
        output = jsonl(
            {"type": "thread.started", "thread_id": SESSION},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done token=secret-value"},
            },
            {"type": "turn.completed"},
        )
        summary = parse_codex_jsonl(output, expected_session_id=SESSION)
        self.assertEqual(SESSION, summary.session_id)
        self.assertEqual(CodexTerminalStatus.COMPLETED, summary.status)
        self.assertEqual("done token=[REDACTED]", summary.final_message)
        self.assertEqual(4, summary.event_count)

    def test_failed_turn_redacts_provider_errors(self) -> None:
        output = jsonl(
            {"type": "thread.started", "thread_id": SESSION},
            {"type": "error", "message": "Bearer ghp_abcdefghijklmnopqrstuvwxyz123456"},
            {"type": "turn.failed"},
        )
        summary = parse_codex_jsonl(output)
        self.assertEqual(CodexTerminalStatus.FAILED, summary.status)
        self.assertEqual(("Bearer [REDACTED]",), summary.errors)

    def test_rejects_session_conflict_duplicate_and_incomplete_streams(self) -> None:
        other = "223e4567-e89b-12d3-a456-426614174000"
        cases = (
            jsonl({"type": "thread.started", "thread_id": other}, {"type": "turn.completed"}),
            jsonl(
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "turn.completed"},
            ),
            jsonl({"type": "thread.started", "thread_id": SESSION}),
            jsonl({"type": "turn.completed"}),
            jsonl(
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "turn.completed"},
            ),
            jsonl(
                {"type": "turn.completed"},
                {"type": "thread.started", "thread_id": SESSION},
            ),
            jsonl(
                {"type": "thread.started", "thread_id": SESSION},
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "done"},
                },
                {"type": "turn.completed"},
                {"type": "turn.started"},
            ),
            '{"type":"thread.started","type":"turn.completed"}\n',
            jsonl(
                {"type": "thread.started", "thread_id": SESSION},
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "bad\u0001text"},
                },
                {"type": "turn.completed"},
            ),
        )
        for output in cases:
            with self.subTest(output=output):
                with self.assertRaises(CodexJsonlError):
                    parse_codex_jsonl(output, expected_session_id=SESSION)


if __name__ == "__main__":
    unittest.main()
