from __future__ import annotations

import json
import unittest

from codex_dispatcher.codex_jsonl import (
    CodexJsonlError,
    CodexTurnUsage,
    CodexTerminalStatus,
    is_context_failure,
    parse_codex_jsonl,
)


SESSION = "123e4567-e89b-12d3-a456-426614174000"


def jsonl(*events: object) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


class CodexJsonlTests(unittest.TestCase):
    def test_parses_one_session_terminal_event_and_redacted_agent_message(self) -> None:
        usage = {
            "input_tokens": 1,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 2,
            "reasoning_output_tokens": 3,
        }
        output = jsonl(
            {"type": "thread.started", "thread_id": SESSION},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done token=secret-value"},
            },
            {"type": "turn.completed", "usage": usage},
        )
        summary = parse_codex_jsonl(output, expected_session_id=SESSION)
        self.assertEqual(SESSION, summary.session_id)
        self.assertEqual(CodexTerminalStatus.COMPLETED, summary.status)
        self.assertEqual("done token=[REDACTED]", summary.final_message)
        self.assertEqual(
            CodexTurnUsage(
                input_tokens=1,
                cached_input_tokens=0,
                cache_write_input_tokens=0,
                output_tokens=2,
                reasoning_output_tokens=3,
            ),
            summary.usage,
        )
        self.assertEqual(4, summary.event_count)

    def test_failed_turn_has_no_usage(self) -> None:
        output = jsonl(
            {"type": "thread.started", "thread_id": SESSION},
            {"type": "turn.failed", "error": {"message": "provider failed"}},
        )
        summary = parse_codex_jsonl(output)
        self.assertIsNone(summary.usage)

        with self.assertRaises(CodexJsonlError) as raised:
            parse_codex_jsonl(
                jsonl(
                    {"type": "thread.started", "thread_id": SESSION},
                    {"type": "turn.failed", "usage": {}},
                )
            )
        self.assertEqual("usage_invalid", raised.exception.code)

    def test_rejects_invalid_turn_usage_in_terminal_event(self) -> None:
        malformed_usage_cases = (
            {
                "input_tokens": 1,
                "cached_input_tokens": 2,
                "cache_write_input_tokens": 3,
                "output_tokens": 4,
            },
            {
                "input_tokens": 1,
                "cached_input_tokens": 2,
                "cache_write_input_tokens": 3,
                "output_tokens": 4,
                "reasoning_output_tokens": 5,
                "extra": 6,
            },
            {
                "input_tokens": True,
                "cached_input_tokens": 2,
                "cache_write_input_tokens": 3,
                "output_tokens": 4,
                "reasoning_output_tokens": 5,
            },
            {
                "input_tokens": 1,
                "cached_input_tokens": -1,
                "cache_write_input_tokens": 3,
                "output_tokens": 4,
                "reasoning_output_tokens": 5,
            },
            {
                "input_tokens": "1",
                "cached_input_tokens": 2,
                "cache_write_input_tokens": 3,
                "output_tokens": 4,
                "reasoning_output_tokens": 5,
            },
            [],
        )
        for usage in malformed_usage_cases:
            with self.subTest(usage=usage):
                output = jsonl(
                    {"type": "thread.started", "thread_id": SESSION},
                    {"type": "turn.failed", "usage": usage},
                )
                with self.assertRaises(CodexJsonlError) as raised:
                    parse_codex_jsonl(output)
                self.assertEqual("usage_invalid", raised.exception.code)

    def test_failed_turn_redacts_provider_errors(self) -> None:
        output = jsonl(
            {"type": "thread.started", "thread_id": SESSION},
            {"type": "error", "message": "Bearer ghp_abcdefghijklmnopqrstuvwxyz123456"},
            {"type": "turn.failed"},
        )
        summary = parse_codex_jsonl(output)
        self.assertEqual(CodexTerminalStatus.FAILED, summary.status)
        self.assertEqual(("Bearer [REDACTED]",), summary.errors)

    def test_context_failure_classifier_accepts_only_known_error_markers(self) -> None:
        for message in (
            "context_length_exceeded",
            "Codex ran out of room in the context window",
            "Remote compact failed while resuming",
            "Compaction failure",
        ):
            with self.subTest(message=message):
                summary = parse_codex_jsonl(
                    jsonl(
                        {"type": "thread.started", "thread_id": SESSION},
                        {"type": "error", "message": message},
                        {"type": "turn.failed"},
                    )
                )
                self.assertTrue(is_context_failure(summary))
        ordinary = parse_codex_jsonl(
            jsonl(
                {"type": "thread.started", "thread_id": SESSION},
                {"type": "error", "message": "network unavailable"},
                {"type": "turn.failed"},
            )
        )
        self.assertFalse(is_context_failure(ordinary))

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
                with self.assertRaises(CodexJsonlError) as raised:
                    parse_codex_jsonl(output, expected_session_id=SESSION)
                self.assertNotEqual("invalid", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
