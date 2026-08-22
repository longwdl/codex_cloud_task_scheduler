from __future__ import annotations

import json
import unittest
from hashlib import sha256

from codex_dispatcher.runner_protocol import (
    NEXT_PROTOCOL_VERSION,
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
GENERATION_ID = "sg_" + "d" * 32
POLICY_DIGEST = "e" * 64


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

    def test_v1_rejects_v2_generation_fields(self) -> None:
        with self.assertRaises(RunnerProtocolError):
            RunnerRequest(
                RunnerOperation.START,
                WORK_ITEM,
                turn_id=TURN,
                prompt_sha256="c" * 64,
                input_head_sha="a" * 40,
                session_generation_id=GENERATION_ID,
                session_generation=1,
                agent_policy_digest=POLICY_DIGEST,
            )

    def test_v2_turn_requests_roundtrip_and_reject_invalid_shapes(self) -> None:
        common = {
            "version": NEXT_PROTOCOL_VERSION,
            "session_generation_id": GENERATION_ID,
            "session_generation": 1,
            "agent_policy_digest": POLICY_DIGEST,
        }
        requests = (
            RunnerRequest(
                RunnerOperation.START,
                WORK_ITEM,
                turn_id=TURN,
                prompt_sha256="c" * 64,
                input_head_sha="a" * 40,
                **common,
            ),
            RunnerRequest(
                RunnerOperation.RESUME,
                WORK_ITEM,
                turn_id=TURN,
                session_id=SESSION,
                prompt_sha256="c" * 64,
                input_head_sha="a" * 40,
                **common,
            ),
            RunnerRequest(RunnerOperation.STATUS, WORK_ITEM, turn_id=TURN, **common),
            RunnerRequest(RunnerOperation.STOP, WORK_ITEM, turn_id=TURN, **common),
        )
        for request in requests:
            with self.subTest(operation=request.operation):
                self.assertEqual(request, parse_runner_request(request.to_json()))

        invalid = (
            {**common, "session_generation": 0},
            {**common, "agent_policy_digest": "not-a-digest"},
        )
        for identity in invalid:
            with self.subTest(identity=identity):
                with self.assertRaises(RunnerProtocolError):
                    RunnerRequest(
                        RunnerOperation.STATUS, WORK_ITEM, turn_id=TURN, **identity
                    )
        encoded = requests[0].to_json()
        missing = json.loads(encoded)
        del missing["agent_policy_digest"]
        with self.assertRaises(RunnerProtocolError):
            parse_runner_request(json.dumps(missing))
        extra = json.loads(encoded)
        extra["unexpected"] = True
        with self.assertRaises(RunnerProtocolError):
            parse_runner_request(json.dumps(extra))
        for operation, fields in (
            (
                RunnerOperation.PREPARE,
                {
                    "repository": "owner/repo", "issue_number": 1,
                    "task_branch": "codex/issue-1-aaaaaaaaaaaa", "base_sha": "a" * 40,
                    "source_bundle_sha256": sha256(SOURCE_BUNDLE).hexdigest(),
                    "source_bundle_size": len(SOURCE_BUNDLE),
                },
            ),
            (RunnerOperation.EXPORT, {"expected_head_sha": "a" * 40}),
            (RunnerOperation.ARCHIVE, {}),
        ):
            with self.subTest(v2_only_operation=operation):
                with self.assertRaises(RunnerProtocolError):
                    RunnerRequest(operation, WORK_ITEM, version=NEXT_PROTOCOL_VERSION, **fields)

    def test_v2_archive_requests_require_exact_head_and_reject_v1_status(self) -> None:
        for operation in (
            RunnerOperation.ARCHIVE,
            RunnerOperation.ARCHIVE_STATUS,
        ):
            request = RunnerRequest(
                operation,
                WORK_ITEM,
                version=NEXT_PROTOCOL_VERSION,
                expected_head_sha="c" * 40,
            )
            self.assertEqual(request, parse_runner_request(request.to_json()))
            with self.assertRaises(RunnerProtocolError):
                RunnerRequest(operation, WORK_ITEM, version=NEXT_PROTOCOL_VERSION)
        with self.assertRaises(RunnerProtocolError):
            RunnerRequest(RunnerOperation.ARCHIVE_STATUS, WORK_ITEM)

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
