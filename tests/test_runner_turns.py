from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from codex_dispatcher.runner_protocol import RunnerOperation, RunnerRequest
from codex_dispatcher.runner_service import LinuxRunnerService, serve_one
from codex_dispatcher.runner_transport import (
    RunnerTurnRemoteState,
    parse_runner_ack,
    parse_runner_turn_reply,
)
from codex_dispatcher.runner_turns import RunnerTurnError, RunnerTurnExecutor
from codex_dispatcher.runner_wire import (
    decode_runner_output,
    encode_runner_input,
)
from codex_dispatcher.runner_workspace import RunnerWorkspace
from tests.test_runner_workspace import GIT, WORK_ITEM, fixture, prepare_request


SESSION = "123e4567-e89b-12d3-a456-426614174000"
TURN_ONE = "turn_" + "1" * 32
TURN_TWO = "turn_" + "2" * 32


def fake_codex(
    path: Path,
    *,
    valid_result: bool = True,
    login_method: str = "ChatGPT",
) -> None:
    result_expression = (
        "json.dumps(result, sort_keys=True, separators=(',', ':'))"
        if valid_result
        else "'not-json'"
    )
    path.write_text(
        f"""#!{sys.executable}
import json
import subprocess
import sys
from pathlib import Path

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
    "status": "completed",
    "summary": "fixture complete",
    "needs_input": [],
    "tests": [{{"name": "fixture", "status": "passed"}}],
    "changed_paths": changed,
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
    def setUpRunner(
        self,
        root: Path,
        *,
        valid_result: bool = True,
        login_method: str = "ChatGPT",
    ) -> tuple[bytes, str, RunnerWorkspace, RunnerTurnExecutor]:
        artifact, base_sha = fixture(root)
        workspace = RunnerWorkspace(git_path=GIT, work_items_root=root / "runner")
        workspace.prepare(prepare_request(artifact, base_sha), artifact)
        codex = root / "fake-codex"
        fake_codex(
            codex,
            valid_result=valid_result,
            login_method=login_method,
        )
        schema = root / "schema.json"
        schema.write_text("{}\n", encoding="utf-8")
        codex_home = root / "shared-codex-home"
        codex_home.mkdir(mode=0o700)
        turns = RunnerTurnExecutor(
            workspace=workspace,
            codex_path=codex,
            codex_home=codex_home,
            output_schema=schema,
            timeout_seconds=10,
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

    def test_forced_command_service_roundtrips_prepare_and_turn_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact, base_sha = fixture(root)
            workspace = RunnerWorkspace(git_path=GIT, work_items_root=root / "runner")
            codex = root / "fake-codex"
            fake_codex(codex)
            schema = root / "schema.json"
            schema.write_text("{}\n", encoding="utf-8")
            codex_home = root / "shared-codex-home"
            codex_home.mkdir(mode=0o700)
            turns = RunnerTurnExecutor(
                workspace=workspace,
                codex_path=codex,
                codex_home=codex_home,
                output_schema=schema,
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


if __name__ == "__main__":
    unittest.main()
