from __future__ import annotations

import json
import unittest
from hashlib import sha256

from codex_dispatcher.runner_protocol import (
    AgentResultStatus,
    RunnerOperation,
    RunnerProtocolError,
    RunnerRequest,
    parse_agent_result,
    parse_runner_request,
)


WORK_ITEM = "wi_" + "a" * 24
TURN = "turn_" + "b" * 32
SESSION = "123e4567-e89b-12d3-a456-426614174000"
SOURCE_BUNDLE = b"fixture source bundle"


class RunnerProtocolTests(unittest.TestCase):
    def test_prepare_start_and_resume_roundtrip_without_prompt_or_path_fields(self) -> None:
        requests = (
            RunnerRequest(
                RunnerOperation.PREPARE,
                WORK_ITEM,
                repository="owner/repo",
                issue_number=1,
                task_branch="codex/issue-1-aaaaaaaaaaaa",
                base_sha="a" * 40,
                source_bundle_sha256=sha256(SOURCE_BUNDLE).hexdigest(),
                source_bundle_size=len(SOURCE_BUNDLE),
            ),
            RunnerRequest(
                RunnerOperation.START,
                WORK_ITEM,
                turn_id=TURN,
                prompt_sha256="c" * 64,
                input_head_sha="a" * 40,
            ),
            RunnerRequest(
                RunnerOperation.RESUME,
                WORK_ITEM,
                turn_id=TURN,
                session_id=SESSION,
                prompt_sha256="c" * 64,
                input_head_sha="a" * 40,
            ),
        )
        for request in requests:
            with self.subTest(operation=request.operation):
                encoded = request.to_json()
                self.assertEqual(request, parse_runner_request(encoded))
                self.assertNotIn("prompt\"", encoded)
                self.assertNotIn("runner_directory", encoded)
                self.assertNotIn("remote_url", encoded)

    def test_prepare_requires_bounded_source_bundle_identity(self) -> None:
        with self.assertRaises(RunnerProtocolError):
            RunnerRequest(
                RunnerOperation.PREPARE,
                WORK_ITEM,
                repository="owner/repo",
                issue_number=1,
                task_branch="codex/issue-1-aaaaaaaaaaaa",
                base_sha="a" * 40,
            )
        with self.assertRaises(RunnerProtocolError):
            RunnerRequest(
                RunnerOperation.PREPARE,
                WORK_ITEM,
                repository="owner/repo",
                issue_number=1,
                task_branch="codex/issue-1-aaaaaaaaaaaa",
                base_sha="a" * 40,
                source_bundle_sha256="d" * 64,
                source_bundle_size=0,
            )

    def test_rejects_unknown_missing_duplicate_and_operation_specific_fields(self) -> None:
        valid = RunnerRequest(
            RunnerOperation.START,
            WORK_ITEM,
            turn_id=TURN,
            prompt_sha256="c" * 64,
            input_head_sha="a" * 40,
        ).to_json()
        cases = (
            valid[:-1] + ',"remote_url":"https://evil.invalid/repo"}',
            valid.replace(',"turn_id"', ',"prompt_sha256":"' + "c" * 64 + '","turn_id"'),
            valid.replace('"version":1,', ""),
            valid.replace('"op":"start"', '"op":"shell"'),
        )
        for payload in cases:
            with self.subTest(payload=payload[:60]):
                with self.assertRaises(RunnerProtocolError):
                    parse_runner_request(payload)

    def test_agent_result_is_strict_bounded_and_path_safe(self) -> None:
        payload = {
            "status": "needs_input",
            "summary": "Need one decision",
            "needs_input": ["Which format should be used?"],
            "tests": [{"name": "unit", "status": "not_run"}],
            "changed_paths": ["src/main.py"],
            "next_step": "Wait for the Issue update",
        }
        result = parse_agent_result(json.dumps(payload))
        self.assertEqual(AgentResultStatus.NEEDS_INPUT, result.status)
        self.assertEqual(("src/main.py",), result.changed_paths)

        redacted = parse_agent_result(
            json.dumps(
                {
                    **payload,
                    "summary": "token=secret-value",
                    "needs_input": ["Use api_key=secret-value?"],
                }
            )
        )
        self.assertEqual("token=[REDACTED]", redacted.summary)
        self.assertEqual(("Use api_key=[REDACTED]",), redacted.needs_input)

        invalid = (
            {**payload, "unexpected": True},
            {**payload, "changed_paths": ["../secret"]},
            {**payload, "changed_paths": ["src/main.py", "src/main.py"]},
            {**payload, "status": "completed"},
            {**payload, "tests": [{"name": "unit", "status": "unknown"}]},
            {**payload, "summary": "bad\u0001text"},
        )
        for item in invalid:
            with self.subTest(item=item):
                with self.assertRaises(RunnerProtocolError):
                    parse_agent_result(json.dumps(item))


if __name__ == "__main__":
    unittest.main()
