from __future__ import annotations

import unittest
from hashlib import sha256

from codex_dispatcher.runner_protocol import (
    RunnerOperation,
    RunnerProtocolError,
    RunnerRequest,
)
from codex_dispatcher.runner_transport import RunnerWireOutput
from codex_dispatcher.runner_wire import (
    decode_runner_input,
    decode_runner_output,
    encode_runner_input,
    encode_runner_output,
)


WORK_ITEM = "wi_" + "a" * 24
TURN = "turn_" + "b" * 32


class RunnerWireTests(unittest.TestCase):
    def test_request_and_prompt_have_unambiguous_length_prefixed_boundaries(self) -> None:
        prompt = "实现一项功能\n{not protocol json}\n".encode()
        request = RunnerRequest(
            RunnerOperation.START,
            WORK_ITEM,
            turn_id=TURN,
            prompt_sha256=sha256(prompt).hexdigest(),
            input_head_sha="c" * 40,
        )
        decoded_request, decoded_prompt, decoded_artifact = decode_runner_input(
            encode_runner_input(request, prompt=prompt)
        )
        self.assertEqual(request, decoded_request)
        self.assertEqual(prompt, decoded_prompt)
        self.assertIsNone(decoded_artifact)

    def test_prepare_frame_binds_binary_source_artifact_by_size_and_hash(self) -> None:
        source_bundle = b"\x00fixture bundle\xff"
        request = RunnerRequest(
            RunnerOperation.PREPARE,
            WORK_ITEM,
            repository="owner/repo",
            issue_number=1,
            task_branch="codex/issue-1-aaaaaaaaaaaa",
            base_sha="c" * 40,
            source_bundle_sha256=sha256(source_bundle).hexdigest(),
            source_bundle_size=len(source_bundle),
        )
        decoded_request, decoded_prompt, decoded_artifact = decode_runner_input(
            encode_runner_input(request, source_artifact=source_bundle)
        )
        self.assertEqual(request, decoded_request)
        self.assertEqual(b"", decoded_prompt)
        self.assertEqual(source_bundle, decoded_artifact)

        with self.assertRaisesRegex(RunnerProtocolError, "match"):
            encode_runner_input(request, source_artifact=source_bundle + b"x")
        with self.assertRaisesRegex(RunnerProtocolError, "source artifact"):
            encode_runner_input(request)

    def test_input_rejects_trailing_bytes_hash_conflict_and_prompt_on_status(self) -> None:
        prompt = b"prompt\n"
        request = RunnerRequest(
            RunnerOperation.START,
            WORK_ITEM,
            turn_id=TURN,
            prompt_sha256=sha256(prompt).hexdigest(),
            input_head_sha="c" * 40,
        )
        frame = encode_runner_input(request, prompt=prompt)
        with self.assertRaisesRegex(RunnerProtocolError, "length"):
            decode_runner_input(frame + b"trailing")
        with self.assertRaisesRegex(RunnerProtocolError, "hash"):
            decode_runner_input(frame[:-1] + b"x")
        status = RunnerRequest(
            RunnerOperation.STATUS,
            WORK_ITEM,
            turn_id=TURN,
        )
        with self.assertRaisesRegex(RunnerProtocolError, "Prompt"):
            encode_runner_input(status, prompt=b"not allowed")
        with self.assertRaisesRegex(RunnerProtocolError, "source artifact"):
            encode_runner_input(status, source_artifact=b"not allowed")

    def test_response_frame_roundtrips_binary_artifact_and_rejects_trailing_bytes(self) -> None:
        output = RunnerWireOutput(b'{"state":"ok"}', b"\x00binary\xffartifact")
        frame = encode_runner_output(output)
        self.assertEqual(output, decode_runner_output(frame))
        with self.assertRaisesRegex(RunnerProtocolError, "length"):
            decode_runner_output(frame + b"trailing")


if __name__ == "__main__":
    unittest.main()
