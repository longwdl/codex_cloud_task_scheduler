from __future__ import annotations

import json
import unittest
from hashlib import sha256

from codex_dispatcher.runner_protocol import (
    RunnerOperation,
    RunnerProtocolError,
    parse_agent_result,
)
from codex_dispatcher.runner_transport import (
    RunnerAck,
    RunnerExportReply,
    RunnerTurnRemoteState,
    RunnerTurnReply,
    parse_runner_ack,
    parse_runner_export_reply,
    parse_runner_turn_reply,
)


WORK_ITEM = "wi_" + "a" * 24
TURN = "turn_" + "b" * 32
SESSION = "123e4567-e89b-12d3-a456-426614174000"


def agent_result():
    return parse_agent_result(
        json.dumps(
            {
                "status": "completed",
                "summary": "Implemented",
                "needs_input": [],
                "tests": [{"name": "unit", "status": "passed"}],
                "changed_paths": ["src/main.py"],
                "next_step": "Publish checkpoint",
            }
        )
    )


class RunnerTransportContractTests(unittest.TestCase):
    def test_ack_and_finished_turn_roundtrip(self) -> None:
        ack = RunnerAck(RunnerOperation.PREPARE, WORK_ITEM)
        self.assertEqual(ack, parse_runner_ack(ack.to_json()))

        result = agent_result()
        result_json = json.dumps(
            {
                "changed_paths": ["src/main.py"],
                "needs_input": [],
                "next_step": "Publish checkpoint",
                "status": "completed",
                "summary": "Implemented",
                "tests": [{"name": "unit", "status": "passed"}],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        reply = RunnerTurnReply(
            operation=RunnerOperation.START,
            work_item_id=WORK_ITEM,
            turn_id=TURN,
            state=RunnerTurnRemoteState.FINISHED,
            session_id=SESSION,
            head_sha="c" * 40,
            output_sha256=sha256(result_json.encode()).hexdigest(),
            result=result,
        )
        self.assertEqual(reply, parse_runner_turn_reply(reply.to_json()))

    def test_turn_reply_rejects_hash_duplicate_and_shape_conflicts(self) -> None:
        result = agent_result()
        with self.assertRaisesRegex(RunnerProtocolError, "hash"):
            RunnerTurnReply(
                operation=RunnerOperation.START,
                work_item_id=WORK_ITEM,
                turn_id=TURN,
                state=RunnerTurnRemoteState.FINISHED,
                session_id=SESSION,
                head_sha="c" * 40,
                output_sha256="d" * 64,
                result=result,
            )
        with self.assertRaises(RunnerProtocolError):
            parse_runner_turn_reply(
                '{"version":1,"op":"status","op":"start",'
                f'"work_item_id":"{WORK_ITEM}","turn_id":"{TURN}",'
                '"state":"running","session_id":"'
                f'{SESSION}"}}'
            )
        with self.assertRaises(RunnerProtocolError):
            parse_runner_turn_reply(
                json.dumps(
                    {
                        "version": 1,
                        "op": "start",
                        "work_item_id": WORK_ITEM,
                        "turn_id": TURN,
                        "state": "running",
                        "session_id": SESSION,
                        "head_sha": "c" * 40,
                    }
                )
            )

    def test_export_manifest_verifies_artifact_bytes(self) -> None:
        artifact = b"fake-git-bundle"
        reply = RunnerExportReply(
            work_item_id=WORK_ITEM,
            head_sha="c" * 40,
            bundle_sha256=sha256(artifact).hexdigest(),
            size_bytes=len(artifact),
        )
        parsed = parse_runner_export_reply(reply.to_json())
        self.assertEqual(artifact, parsed.validate_artifact(artifact))
        with self.assertRaisesRegex(RunnerProtocolError, "hash"):
            parsed.validate_artifact(b"other-artifact!")


if __name__ == "__main__":
    unittest.main()
