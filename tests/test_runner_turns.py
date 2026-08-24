from __future__ import annotations

import io
import json
import fcntl
import sys
import tempfile
import unittest
from unittest.mock import patch
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from codex_dispatcher.runner_protocol import (
    NEXT_PROTOCOL_VERSION,
    RunnerOperation,
    RunnerRequest,
    parse_agent_result,
)
from codex_dispatcher.runner_service import LinuxRunnerService, serve_one
from codex_dispatcher.runner_transport import (
    RUNNER_CAPACITY_SCOPE_ID,
    RUNNER_RECLAMATION_SCOPE_ID,
    RunnerArchiveState,
    RunnerInactiveContainerState,
    RunnerTransportRejected,
    RunnerTurnRemoteState,
    RunnerTurnReply,
    parse_runner_ack,
    parse_runner_archive_reply,
    parse_runner_capacity_reply,
    parse_runner_reclamation_status_reply,
    parse_runner_turn_reply,
)
from codex_dispatcher.runner_reclamation_status import RunnerReclamationStatus
from codex_dispatcher.runner_turns import (
    RunnerTurnError,
    RunnerTurnExecutor,
    audit_mutation_is_forbidden,
)
from codex_dispatcher.runner_wire import (
    decode_runner_output,
    encode_runner_input,
)
from codex_dispatcher.runner_workspace import RunnerWorkspace
from tests.test_runner_workspace import GIT, WORK_ITEM, fixture, prepare_request


SESSION = "123e4567-e89b-12d3-a456-426614174000"
TURN_ONE = "turn_" + "1" * 32
TURN_TWO = "turn_" + "2" * 32
GENERATION_ID = "sg_" + "d" * 32
POLICY_DIGEST = "e" * 64


def fake_codex(
    path: Path,
    *,
    valid_result: bool = True,
    login_method: str = "ChatGPT",
    expected_proxy_url: str | None = None,
) -> None:
    result_expression = (
        "json.dumps(result, sort_keys=True, separators=(',', ':'))"
        if valid_result
        else "'not-json'"
    )
    proxy_check = ""
    if expected_proxy_url is not None:
        proxy_check = f"""
proxy = {expected_proxy_url!r}
for name in (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
):
    assert os.environ.get(name) == proxy
assert os.environ.get("NO_PROXY") == ""
assert os.environ.get("no_proxy") == ""
"""
    path.write_text(
        f"""#!{sys.executable}
import json
import os
import subprocess
import sys
from pathlib import Path
{proxy_check}

if sys.argv[-2:] == ["login", "status"]:
    sys.stderr.write("Logged in using {login_method}\\n")
    raise SystemExit(0)

prompt = sys.stdin.read()
session = sys.argv[sys.argv.index("resume") + 1] if "resume" in sys.argv else "{SESSION}"
changed = []
if "change" in prompt:
    Path("result.txt").write_text(prompt + "\\n", encoding="utf-8")
    subprocess.run(["{GIT}", "add", "result.txt"], check=True)
    subprocess.run(["{GIT}", "commit", "-m", "checkpoint"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    changed = ["result.txt"]
result = {{
    "schema_version": 2,
    "status": "completed",
    "summary": "fixture complete",
    "acceptance": [],
    "remaining_work": [],
    "needs_input": [],
    "tests": [{{"name": "fixture", "status": "passed"}}],
    "changed_paths": changed,
    "blocker_code": None,
    "next_step": "review",
}}
events = [
    {{"type": "thread.started", "thread_id": session}},
    {{"type": "turn.started"}},
    {{"type": "item.completed", "item": {{"type": "agent_message", "text": {result_expression}}}}},
    {{"type": "turn.completed"}},
]
for event in events:
    print(json.dumps(event, separators=(",", ":")), flush=True)
""",
        encoding="utf-8",
    )
    path.chmod(0o700)


def start_request(turn_id: str, prompt: bytes, input_head: str) -> RunnerRequest:
    return RunnerRequest(
        RunnerOperation.START,
        WORK_ITEM,
        turn_id=turn_id,
        prompt_sha256=sha256(prompt).hexdigest(),
        input_head_sha=input_head,
    )


class RunnerTurnExecutorTests(unittest.TestCase):
    def test_audit_role_rejects_head_or_reported_path_mutation(self) -> None:
        request = RunnerRequest(
            RunnerOperation.START,
            WORK_ITEM,
            version=NEXT_PROTOCOL_VERSION,
            turn_id=TURN_ONE,
            prompt_sha256="a" * 64,
            input_head_sha="b" * 40,
            session_generation_id=GENERATION_ID,
            session_generation=1,
            agent_policy_digest=POLICY_DIGEST,
            session_role="audit",
        )
        result = parse_agent_result(
            json.dumps(
                {
                    "schema_version": 2,
                    "status": "completed",
                    "summary": "read-only audit",
                    "acceptance": [],
                    "remaining_work": [],
                    "needs_input": [],
                    "tests": [{"name": "audit", "status": "passed"}],
                    "changed_paths": [],
                    "blocker_code": None,
                    "next_step": "review",
                }
            )
        )
        self.assertFalse(audit_mutation_is_forbidden(request, result, "b" * 40))
        self.assertTrue(audit_mutation_is_forbidden(request, result, "c" * 40))
        changed = parse_agent_result(
            json.dumps(
                {
                    **json.loads(json.dumps({
                        "schema_version": 2,
                        "status": "completed",
                        "summary": "audit changed a file",
                        "acceptance": [],
                        "remaining_work": [],
                        "needs_input": [],
                        "tests": [],
                        "changed_paths": ["src/module.py"],
                        "blocker_code": None,
                        "next_step": "reject",
                    }))
                }
            )
        )
        self.assertTrue(audit_mutation_is_forbidden(request, changed, "b" * 40))

    def setUpRunner(
        self,
        root: Path,
        *,
        valid_result: bool = True,
        login_method: str = "ChatGPT",
        egress_proxy_url: str | None = None,
    ) -> tuple[bytes, str, RunnerWorkspace, RunnerTurnExecutor]:
        artifact, base_sha = fixture(root)
        workspace = RunnerWorkspace(git_path=GIT, work_items_root=root / "runner")
        workspace.prepare(prepare_request(artifact, base_sha), artifact)
        codex = root / "fake-codex"
        fake_codex(
            codex,
            valid_result=valid_result,
            login_method=login_method,
            expected_proxy_url=egress_proxy_url,
        )
        schema = root / "schema.json"
        schema.write_text("{}\n", encoding="utf-8")
        audit_schema = root / "audit-schema.json"
        audit_schema.write_text("{}\n", encoding="utf-8")
        codex_home = root / "shared-codex-home"
        codex_home.mkdir(mode=0o700)
        turns = RunnerTurnExecutor(
            workspace=workspace,
            codex_path=codex,
            codex_home=codex_home,
            output_schema=schema,
            audit_output_schema=audit_schema,
            timeout_seconds=10,
            egress_proxy_url=egress_proxy_url,
        )
        return artifact, base_sha, workspace, turns

    def test_first_and_resume_turn_reuse_session_and_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, base_sha, workspace, turns = self.setUpRunner(root)
            first_prompt = b"first change"
            first_request = start_request(TURN_ONE, first_prompt, base_sha)
            first = turns.execute(first_request, first_prompt)

            self.assertEqual(RunnerTurnRemoteState.FINISHED, first.state)
            self.assertEqual(SESSION, first.session_id)
            self.assertNotEqual(base_sha, first.head_sha)
            status = turns.status(
                RunnerRequest(RunnerOperation.STATUS, WORK_ITEM, turn_id=TURN_ONE)
            )
            self.assertEqual(RunnerOperation.STATUS, status.operation)
            self.assertEqual(first.head_sha, status.head_sha)

            assert first.head_sha is not None
            second_prompt = b"second change"
            second_request = RunnerRequest(
                RunnerOperation.RESUME,
                WORK_ITEM,
                turn_id=TURN_TWO,
                session_id=SESSION,
                prompt_sha256=sha256(second_prompt).hexdigest(),
                input_head_sha=first.head_sha,
            )
            second = turns.execute(second_request, second_prompt)
            repeated = turns.execute(second_request, second_prompt)
            self.assertEqual(second, repeated)
            self.assertEqual(SESSION, second.session_id)
            self.assertNotEqual(first.head_sha, second.head_sha)
            self.assertEqual(second.head_sha, workspace.current_head(WORK_ITEM))

            conflicting_prompt = b"conflicting change"
            conflict = RunnerRequest(
                RunnerOperation.RESUME,
                WORK_ITEM,
                turn_id=TURN_TWO,
                session_id=SESSION,
                prompt_sha256=sha256(conflicting_prompt).hexdigest(),
                input_head_sha=second_request.input_head_sha,
            )
            with self.assertRaisesRegex(RunnerTurnError, "identity"):
                turns.execute(conflict, conflicting_prompt)

    def test_v2_start_and_status_are_rejected_before_turn_record_io(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, base_sha, workspace, turns = self.setUpRunner(root)
            prompt = b"v2 must not run"
            generation = {
                "version": NEXT_PROTOCOL_VERSION,
                "session_generation_id": GENERATION_ID,
                "session_generation": 1,
                "agent_policy_digest": POLICY_DIGEST,
            }
            start = RunnerRequest(
                RunnerOperation.START,
                WORK_ITEM,
                turn_id=TURN_ONE,
                prompt_sha256=sha256(prompt).hexdigest(),
                input_head_sha=base_sha,
                **generation,
            )
            status = RunnerRequest(
                RunnerOperation.STATUS, WORK_ITEM, turn_id=TURN_TWO, **generation
            )
            records = workspace.paths(WORK_ITEM).state / "turns"
            self.assertFalse(records.exists())
            for request, supplied_prompt in ((start, prompt), (status, None)):
                with self.subTest(operation=request.operation):
                    with self.assertRaisesRegex(
                        RunnerTurnError, "requires policy-bound Docker execution"
                    ):
                        if supplied_prompt is None:
                            turns.status(request)
                        else:
                            turns.execute(request, supplied_prompt)
            self.assertFalse(records.exists())

    def test_context_failure_reply_proves_clean_anchor_and_rejects_dirty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, base_sha, workspace, turns = self.setUpRunner(root)
            prompt = b"context failure"
            common = {
                "version": NEXT_PROTOCOL_VERSION,
                "session_generation_id": GENERATION_ID,
                "session_generation": 1,
                "agent_policy_digest": POLICY_DIGEST,
            }
            request = RunnerRequest(
                RunnerOperation.START,
                WORK_ITEM,
                turn_id=TURN_ONE,
                prompt_sha256=sha256(prompt).hexdigest(),
                input_head_sha=base_sha,
                **common,
            )

            clean = turns._context_failure_reply(request, session_id=SESSION)

            self.assertEqual("session_context_failure_clean", clean.error_code)
            self.assertEqual(base_sha, clean.failure_head_sha)
            self.assertTrue(clean.worktree_clean)
            self.assertEqual(clean, parse_runner_turn_reply(clean.to_json()))

            repository = workspace.paths(WORK_ITEM).repository
            (repository / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            dirty = turns._context_failure_reply(request, session_id=SESSION)

            self.assertEqual(
                "session_context_failure_dirty_worktree", dirty.error_code
            )
            self.assertEqual(base_sha, dirty.failure_head_sha)
            self.assertFalse(dirty.worktree_clean)

    def test_invalid_agent_result_is_durable_failure_not_a_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, base_sha, _, turns = self.setUpRunner(root, valid_result=False)
            prompt = b"readonly"
            request = start_request(TURN_ONE, prompt, base_sha)
            reply = turns.execute(request, prompt)
            repeated = turns.execute(request, prompt)

            self.assertEqual(reply, repeated)
            self.assertEqual(RunnerTurnRemoteState.FAILED, reply.state)
            self.assertEqual("agent_result_invalid", reply.error_code)
            self.assertEqual(SESSION, reply.session_id)

    def test_non_chatgpt_authentication_fails_before_starting_a_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, base_sha, workspace, turns = self.setUpRunner(
                root, login_method="an API key"
            )
            prompt = b"must not run"
            request = start_request(TURN_ONE, prompt, base_sha)

            reply = turns.execute(request, prompt)
            repeated = turns.execute(request, prompt)

            self.assertEqual(reply, repeated)
            self.assertEqual(RunnerTurnRemoteState.FAILED, reply.state)
            self.assertEqual("codex_auth_invalid", reply.error_code)
            self.assertEqual(base_sha, workspace.current_head(WORK_ITEM))

    def test_fixed_proxy_reaches_auth_preflight_and_turn_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, base_sha, _, turns = self.setUpRunner(
                root,
                egress_proxy_url="http://127.0.0.1:3128",
            )
            prompt = b"readonly"

            reply = turns.execute(start_request(TURN_ONE, prompt, base_sha), prompt)

            self.assertEqual(RunnerTurnRemoteState.FINISHED, reply.state)

    def test_forced_command_service_roundtrips_prepare_and_turn_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, base_sha = fixture(root)
            workspace = RunnerWorkspace(git_path=GIT, work_items_root=root / "runner")
            codex = root / "fake-codex"
            fake_codex(codex)
            schema = root / "schema.json"
            schema.write_text("{}\n", encoding="utf-8")
            audit_schema = root / "audit-schema.json"
            audit_schema.write_text("{}\n", encoding="utf-8")
            codex_home = root / "shared-codex-home"
            codex_home.mkdir(mode=0o700)
            turns = RunnerTurnExecutor(
                workspace=workspace,
                codex_path=codex,
                codex_home=codex_home,
                output_schema=schema,
                audit_output_schema=audit_schema,
                timeout_seconds=10,
            )
            service = LinuxRunnerService(
                workspace=workspace,
                turns=turns,
                active_lock_path=root / "active.lock",
            )
            prepare_output = io.BytesIO()
            serve_one(
                service,
                io.BytesIO(
                    encode_runner_input(
                        prepare_request(artifact, base_sha),
                        source_artifact=artifact,
                    )
                ),
                prepare_output,
            )
            ack = parse_runner_ack(decode_runner_output(prepare_output.getvalue()).payload)
            self.assertEqual(WORK_ITEM, ack.work_item_id)

            prompt = b"service change"
            turn_output = decode_runner_output(
                service.handle_frame(
                    encode_runner_input(
                        start_request(TURN_ONE, prompt, base_sha), prompt=prompt
                    )
                )
            )
            reply = parse_runner_turn_reply(turn_output.payload)
            self.assertEqual(RunnerTurnRemoteState.FINISHED, reply.state)
            self.assertIsNone(turn_output.artifact)
            assert reply.head_sha is not None
            archived_output = decode_runner_output(
                service.handle_frame(
                    encode_runner_input(
                        RunnerRequest(
                            RunnerOperation.ARCHIVE,
                            WORK_ITEM,
                            version=NEXT_PROTOCOL_VERSION,
                            expected_head_sha=reply.head_sha,
                        )
                    )
                )
            )
            archived = parse_runner_archive_reply(archived_output.payload)
            self.assertIs(RunnerArchiveState.ARCHIVED, archived.state)
            self.assertFalse(
                (root / "runner" / "owner__repo" / "issue-42").exists()
            )
            repeated = parse_runner_archive_reply(
                decode_runner_output(
                    service.handle_frame(
                        encode_runner_input(
                            RunnerRequest(
                                RunnerOperation.ARCHIVE,
                                WORK_ITEM,
                                version=NEXT_PROTOCOL_VERSION,
                                expected_head_sha=reply.head_sha,
                            )
                        )
                    )
                ).payload
            )
            self.assertEqual(archived.archived_at, repeated.archived_at)

    def test_forced_command_capacity_is_read_only_and_does_not_take_turn_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = SimpleNamespace(
                capacity_bytes=1_000,
                available_bytes=500,
                image_size_bytes=200,
                host_reserve_bytes=200,
                turn_admissible=True,
                provision_admissible=True,
                provision_shortfall_bytes=0,
            )
            workspace = SimpleNamespace(capacity_snapshot=lambda: snapshot)
            service = LinuxRunnerService(
                workspace=workspace,
                turns=SimpleNamespace(),
                active_lock_path=Path(temp_dir) / "missing-parent" / "active.lock",
            )
            response = decode_runner_output(
                service.handle_frame(
                    encode_runner_input(
                        RunnerRequest(
                            RunnerOperation.CAPACITY,
                            RUNNER_CAPACITY_SCOPE_ID,
                            version=NEXT_PROTOCOL_VERSION,
                        )
                    )
                )
            )
            reply = parse_runner_capacity_reply(response.payload)
            self.assertTrue(reply.provision_admissible)
            self.assertFalse((Path(temp_dir) / "missing-parent").exists())

    def test_forced_reclamation_status_is_read_only_and_does_not_take_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LinuxRunnerService(
                workspace=SimpleNamespace(),
                turns=SimpleNamespace(),
                active_lock_path=Path(temp_dir) / "missing-parent" / "active.lock",
            )
            status = RunnerReclamationStatus(
                checked_at="2026-08-24T00:00:00Z",
                host_available_bytes=64 * 1024**3,
                release_count=2,
                release_target_count=0,
                image_target_count=0,
                expected_total_bytes=0,
                trigger_reasons=(),
                plan_sha256=None,
            )
            with patch(
                "codex_dispatcher.runner_service.read_runner_reclamation_status",
                return_value=status,
            ):
                response = decode_runner_output(
                    service.handle_frame(
                        encode_runner_input(
                            RunnerRequest(
                                RunnerOperation.RECLAMATION_STATUS,
                                RUNNER_RECLAMATION_SCOPE_ID,
                                version=NEXT_PROTOCOL_VERSION,
                            )
                        )
                    )
                )
            reply = parse_runner_reclamation_status_reply(response.payload)
            self.assertEqual((), reply.trigger_reasons)
            self.assertFalse((Path(temp_dir) / "missing-parent").exists())

    def test_forced_command_stop_is_abandonment_only_and_requires_turn_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock_path = root / "active.lock"
            calls = []
            request = RunnerRequest(
                RunnerOperation.STOP,
                WORK_ITEM,
                turn_id=TURN_ONE,
                session_generation_id=GENERATION_ID,
                session_generation=1,
                agent_policy_digest=POLICY_DIGEST,
                version=NEXT_PROTOCOL_VERSION,
            )
            expected = RunnerTurnReply(
                operation=RunnerOperation.STOP,
                work_item_id=WORK_ITEM,
                turn_id=TURN_ONE,
                state=RunnerTurnRemoteState.FAILED,
                error_code="turn_abandoned_inactive",
                inactive_container_state=RunnerInactiveContainerState.ABSENT,
                inactive_observed_at="2026-08-23T00:00:00+00:00",
                session_generation_id=GENERATION_ID,
                session_generation=1,
                agent_policy_digest=POLICY_DIGEST,
                version=NEXT_PROTOCOL_VERSION,
            )
            turns = SimpleNamespace(
                abandon=lambda observed: calls.append(observed) or expected
            )
            service = LinuxRunnerService(
                workspace=SimpleNamespace(),
                turns=turns,
                active_lock_path=lock_path,
            )
            with lock_path.open("a+b") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(RunnerTransportRejected):
                    service.handle_frame(encode_runner_input(request))
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            self.assertEqual([], calls)

            output = decode_runner_output(
                service.handle_frame(encode_runner_input(request))
            )
            self.assertEqual(expected, parse_runner_turn_reply(output.payload))
            self.assertEqual([request], calls)


if __name__ == "__main__":
    unittest.main()
