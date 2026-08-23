from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from hashlib import sha256

from codex_dispatcher.codex_jsonl import CodexTurnUsage
from codex_dispatcher.delegation_evidence import DelegatedAgent, DelegationReceipt
from codex_dispatcher.runner_protocol import (
    NEXT_PROTOCOL_VERSION,
    RunnerOperation,
    RunnerProtocolError,
    parse_agent_result,
)
from codex_dispatcher.runner_transport import (
    RUNNER_CAPACITY_SCOPE_ID,
    RunnerCapacityReply,
    RunnerAbsenceReply,
    RunnerArchiveReply,
    RunnerArchiveState,
    RunnerAck,
    RunnerExportReply,
    RunnerTurnRemoteState,
    RunnerTurnReply,
    parse_runner_ack,
    parse_runner_absence_reply,
    parse_runner_archive_reply,
    parse_runner_capacity_reply,
    parse_runner_export_reply,
    parse_runner_turn_reply,
)


WORK_ITEM = "wi_" + "a" * 24
TURN = "turn_" + "b" * 32
SESSION = "123e4567-e89b-12d3-a456-426614174000"
GENERATION_ID = "sg_" + "d" * 32
POLICY_DIGEST = "e" * 64
CHILD_SESSION = "223e4567-e89b-12d3-a456-426614174000"


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


def delegation_receipt() -> DelegationReceipt:
    return DelegationReceipt(
        codex_version="0.147.0",
        root_thread_id=SESSION,
        root_model="gpt-5.6-sol",
        root_reasoning_effort="xhigh",
        agents=(
            DelegatedAgent(
                parent_thread_id=SESSION,
                child_thread_id=CHILD_SESSION,
                agent_name="terra_worker",
                model="gpt-5.6-terra",
                reasoning_effort="medium",
                edge_status="closed",
                tokens_used=42,
            ),
        ),
    )


class RunnerTransportContractTests(unittest.TestCase):
    def test_absence_receipt_is_strict_request_bound_and_canonical(self) -> None:
        reply = RunnerAbsenceReply(
            work_item_id=WORK_ITEM,
            repository="owner/repo",
            issue_number=42,
            task_branch="codex/issue-42-aaaaaaaaaaaa",
            expected_head_sha="c" * 40,
            archive_request_sha256="d" * 64,
            request_sha256="e" * 64,
            observed_at="2026-08-23T00:00:00+00:00",
        )
        self.assertEqual(reply, parse_runner_absence_reply(reply.to_json()))
        payload = json.loads(reply.to_json())
        payload["state"] = "active"
        with self.assertRaises(RunnerProtocolError):
            parse_runner_absence_reply(json.dumps(payload))
        payload = json.loads(reply.to_json())
        del payload["archive_request_sha256"]
        with self.assertRaises(RunnerProtocolError):
            parse_runner_absence_reply(json.dumps(payload))

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

    def test_v2_turn_replies_roundtrip_for_each_state(self) -> None:
        common = {
            "version": NEXT_PROTOCOL_VERSION,
            "session_generation_id": GENERATION_ID,
            "session_generation": 1,
            "agent_policy_digest": POLICY_DIGEST,
        }
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
        replies = (
            RunnerTurnReply(
                operation=RunnerOperation.STATUS,
                work_item_id=WORK_ITEM,
                turn_id=TURN,
                state=RunnerTurnRemoteState.RUNNING,
                session_id=SESSION,
                **common,
            ),
            RunnerTurnReply(
                operation=RunnerOperation.START,
                work_item_id=WORK_ITEM,
                turn_id=TURN,
                state=RunnerTurnRemoteState.FINISHED,
                session_id=SESSION,
                head_sha="c" * 40,
                output_sha256=sha256(result_json.encode()).hexdigest(),
                result=result,
                usage=CodexTurnUsage(1, 2, 3, 4, 5),
                delegation_receipt=delegation_receipt(),
                **common,
            ),
            RunnerTurnReply(
                operation=RunnerOperation.RESUME,
                work_item_id=WORK_ITEM,
                turn_id=TURN,
                state=RunnerTurnRemoteState.FAILED,
                error_code="codex_failed",
                **common,
            ),
            RunnerTurnReply(
                operation=RunnerOperation.STATUS,
                work_item_id=WORK_ITEM,
                turn_id=TURN,
                state=RunnerTurnRemoteState.UNKNOWN,
                error_code="unknown_turn",
                **common,
            ),
        )
        for reply in replies:
            with self.subTest(state=reply.state):
                self.assertEqual(reply, parse_runner_turn_reply(reply.to_json()))

    def test_v2_finished_usage_and_identity_are_strict(self) -> None:
        result = agent_result()
        common = {
            "version": NEXT_PROTOCOL_VERSION,
            "session_generation_id": GENERATION_ID,
            "session_generation": 1,
            "agent_policy_digest": POLICY_DIGEST,
        }
        result_json = json.dumps(
            {
                "changed_paths": ["src/main.py"], "needs_input": [],
                "next_step": "Publish checkpoint", "status": "completed",
                "summary": "Implemented", "tests": [{"name": "unit", "status": "passed"}],
            }, sort_keys=True, separators=(",", ":")
        )
        reply = RunnerTurnReply(
            operation=RunnerOperation.START, work_item_id=WORK_ITEM, turn_id=TURN,
            state=RunnerTurnRemoteState.FINISHED, session_id=SESSION, head_sha="c" * 40,
            output_sha256=sha256(result_json.encode()).hexdigest(), result=result,
            usage=CodexTurnUsage(0, 0, 0, 1, 0), **common,
        )
        payload = json.loads(reply.to_json())
        del payload["usage"]
        with self.assertRaises(RunnerProtocolError):
            parse_runner_turn_reply(json.dumps(payload))
        payload = json.loads(reply.to_json())
        payload["usage"]["input_tokens"] = -1
        with self.assertRaises(RunnerProtocolError):
            parse_runner_turn_reply(json.dumps(payload))
        payload = json.loads(reply.to_json())
        payload["usage"]["unexpected"] = 1
        with self.assertRaises(RunnerProtocolError):
            parse_runner_turn_reply(json.dumps(payload))
        payload = json.loads(reply.to_json())
        del payload["session_generation_id"]
        with self.assertRaises(RunnerProtocolError):
            parse_runner_turn_reply(json.dumps(payload))
        with self.assertRaises(RunnerProtocolError):
            RunnerTurnReply(
                operation=RunnerOperation.START, work_item_id=WORK_ITEM, turn_id=TURN,
                state=RunnerTurnRemoteState.FINISHED, session_id=SESSION, head_sha="c" * 40,
                output_sha256=sha256(result_json.encode()).hexdigest(), result=result,
                usage=CodexTurnUsage(-1, 0, 0, 1, 0), **common,
            )
        legacy_without_delegation = json.loads(reply.to_json())
        self.assertNotIn("delegation_receipt", legacy_without_delegation)
        self.assertIsNone(
            parse_runner_turn_reply(json.dumps(legacy_without_delegation)).delegation_receipt
        )

    def test_v2_clean_context_failure_requires_exact_checkpoint_evidence(self) -> None:
        common = {
            "version": NEXT_PROTOCOL_VERSION,
            "session_generation_id": GENERATION_ID,
            "session_generation": 1,
            "agent_policy_digest": POLICY_DIGEST,
        }
        reply = RunnerTurnReply(
            operation=RunnerOperation.RESUME,
            work_item_id=WORK_ITEM,
            turn_id=TURN,
            state=RunnerTurnRemoteState.FAILED,
            session_id=SESSION,
            error_code="session_context_failure_clean",
            failure_head_sha="c" * 40,
            worktree_clean=True,
            **common,
        )
        self.assertEqual(reply, parse_runner_turn_reply(reply.to_json()))
        for changes in (
            {"failure_head_sha": None},
            {"worktree_clean": False},
        ):
            with self.subTest(changes=changes), self.assertRaises(
                RunnerProtocolError
            ):
                RunnerTurnReply(
                    operation=RunnerOperation.RESUME,
                    work_item_id=WORK_ITEM,
                    turn_id=TURN,
                    state=RunnerTurnRemoteState.FAILED,
                    session_id=SESSION,
                    error_code="session_context_failure_clean",
                    failure_head_sha=changes.get("failure_head_sha", "c" * 40),
                    worktree_clean=changes.get("worktree_clean", True),
                    **common,
                )

    def test_v1_turn_reply_rejects_v2_field_injection(self) -> None:
        reply = RunnerTurnReply(
            operation=RunnerOperation.STATUS,
            work_item_id=WORK_ITEM,
            turn_id=TURN,
            state=RunnerTurnRemoteState.RUNNING,
            session_id=SESSION,
        )
        payload = json.loads(reply.to_json())
        payload["session_generation"] = 1
        with self.assertRaises(RunnerProtocolError):
            parse_runner_turn_reply(json.dumps(payload))

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

    def test_archive_replies_are_strict_and_operation_bound(self) -> None:
        archived_at = datetime.now(timezone.utc).isoformat()
        reply = RunnerArchiveReply(
            RunnerOperation.ARCHIVE,
            WORK_ITEM,
            "c" * 40,
            RunnerArchiveState.ARCHIVED,
            archived_at=archived_at,
            reclaimed_bytes=42,
        )
        self.assertEqual(reply, parse_runner_archive_reply(reply.to_json()))
        active = RunnerArchiveReply(
            RunnerOperation.ARCHIVE_STATUS,
            WORK_ITEM,
            "c" * 40,
            RunnerArchiveState.ACTIVE,
        )
        self.assertEqual(active, parse_runner_archive_reply(active.to_json()))
        payload = json.loads(reply.to_json())
        payload["unexpected"] = True
        with self.assertRaises(RunnerProtocolError):
            parse_runner_archive_reply(json.dumps(payload))
        with self.assertRaises(RunnerProtocolError):
            RunnerArchiveReply(
                RunnerOperation.ARCHIVE,
                WORK_ITEM,
                "c" * 40,
                RunnerArchiveState.ARCHIVING,
            )

    def test_capacity_reply_is_strict_and_recomputes_admission_flags(self) -> None:
        reply = RunnerCapacityReply(
            capacity_bytes=1_000,
            available_bytes=300,
            image_size_bytes=200,
            host_reserve_bytes=200,
            turn_admissible=True,
            provision_admissible=False,
            provision_shortfall_bytes=100,
        )
        self.assertEqual(reply, parse_runner_capacity_reply(reply.to_json()))
        self.assertEqual(RUNNER_CAPACITY_SCOPE_ID, reply.work_item_id)
        payload = json.loads(reply.to_json())
        payload["provision_shortfall_bytes"] = 0
        with self.assertRaises(RunnerProtocolError):
            parse_runner_capacity_reply(json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
