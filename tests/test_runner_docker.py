from __future__ import annotations

import json
import socket
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.executors.codex_docker import (
    DOCKER_NETWORK,
    DOCKER_NETWORK_GATEWAY,
    DOCKER_NETWORK_SUBNET,
    ROOTLESS_HOST_PROXY_URL,
    DockerCodexRuntime,
)
from codex_dispatcher.command_runner import BinaryCommandResult, CommandResult
from codex_dispatcher.runner_protocol import RunnerOperation, RunnerRequest
from codex_dispatcher.runner_transport import RunnerTurnRemoteState
from codex_dispatcher.runner_turns import RunnerTurnError, RunnerTurnExecutor
from codex_dispatcher.runner_workspace import RunnerWorkspace
from tests.test_runner_workspace import GIT, WORK_ITEM, fixture, prepare_request


SESSION = "123e4567-e89b-12d3-a456-426614174000"
TURN_ONE = "turn_" + "1" * 32
TURN_TWO = "turn_" + "2" * 32
IMAGE = "registry.example.invalid/codex-runner@sha256:" + "a" * 64


def _fake_docker(path: Path, call_log: Path, docker_config: Path) -> None:
    path.write_text(
        f"""#!{sys.executable}
import json
import os
import subprocess
import sys
from pathlib import Path

assert os.environ.get("DOCKER_CONFIG") == {str(docker_config)!r}
assert "HOME" not in os.environ
args = sys.argv[1:]
command = args[1:]
if command[:3] == ["image", "inspect", {IMAGE!r}]:
    print(json.dumps({{"Os":"linux","Architecture":"amd64","RepoDigests":[{IMAGE!r}]}}))
    raise SystemExit(0)
if command[:3] == ["network", "inspect", {DOCKER_NETWORK!r}]:
    print(json.dumps({{
        "Name": {DOCKER_NETWORK!r},
        "Driver": "bridge",
        "Scope": "local",
        "Internal": False,
        "Attachable": False,
        "Ingress": False,
        "EnableIPv6": False,
        "Containers": {{}},
        "IPAM": {{"Driver":"default","Config":[{{
            "Subnet": {DOCKER_NETWORK_SUBNET!r},
            "Gateway": {DOCKER_NETWORK_GATEWAY!r},
        }}]}},
        "Options": {{
            "com.docker.network.bridge.enable_icc": "false",
            "com.docker.network.bridge.enable_ip_masquerade": "true",
        }},
    }}))
    raise SystemExit(0)
assert "--user=0:0" in args
assert "--network=codex-egress" in args
assert "--entrypoint=" in args
assert "--env=HTTP_PROXY={ROOTLESS_HOST_PROXY_URL}" in args
image_index = args.index({IMAGE!r})
inner = args[image_index + 1:]
assert inner[0] == "/usr/bin/timeout"
assert inner[1] == "--signal=KILL"
codex = inner[4:]

def mount_source(target):
    for argument in args:
        if argument.startswith("--mount=") and f"target={{target}}" in argument:
            for field in argument.split(","):
                if field.startswith("source="):
                    return Path(field.removeprefix("source="))
    raise AssertionError(f"missing mount {{target}}")

assert mount_source("/usr/local/bin/codex").is_file()

if codex[-2:] == ["login", "status"]:
    with Path({str(call_log)!r}).open("a", encoding="utf-8") as stream:
        stream.write("auth\\n")
    sys.stderr.write("Logged in using ChatGPT\\n")
    raise SystemExit(0)

with Path({str(call_log)!r}).open("a", encoding="utf-8") as stream:
    stream.write("turn\\n")
repository = mount_source("/workspace")
os.chdir(repository)
prompt = sys.stdin.read()
session = codex[codex.index("resume") + 1] if "resume" in codex else {SESSION!r}
changed = []
if "change" in prompt:
    Path("result.txt").write_text(prompt + "\\n", encoding="utf-8")
    subprocess.run([{GIT!r}, "add", "result.txt"], check=True)
    subprocess.run(
        [{GIT!r}, "commit", "-m", "checkpoint"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
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
    {{"type": "item.completed", "item": {{
        "type": "agent_message",
        "text": json.dumps(result, sort_keys=True, separators=(",", ":")),
    }}}},
    {{"type": "turn.completed"}},
]
for event in events:
    print(json.dumps(event, separators=(",", ":")), flush=True)
""",
        encoding="utf-8",
    )
    path.chmod(0o700)


def _request(
    operation: RunnerOperation,
    turn_id: str,
    prompt: bytes,
    head: str,
    *,
    session_id: str | None = None,
) -> RunnerRequest:
    return RunnerRequest(
        operation,
        WORK_ITEM,
        turn_id=turn_id,
        session_id=session_id,
        prompt_sha256=sha256(prompt).hexdigest(),
        input_head_sha=head,
    )


class RunnerDockerExecutionTests(unittest.TestCase):
    def _setup(
        self, root: Path, *, create_socket: bool = True
    ) -> tuple[
        RunnerWorkspace,
        RunnerTurnExecutor,
        str,
        Path,
        Path,
        socket.socket | None,
    ]:
        artifact, base_sha = fixture(root)
        workspace = RunnerWorkspace(git_path=GIT, work_items_root=root / "runner")
        workspace.prepare(prepare_request(artifact, base_sha), artifact)
        docker_config = root / "docker-config"
        docker_config.mkdir(mode=0o700)
        call_log = root / "docker-calls.log"
        docker = root / "docker"
        _fake_docker(docker, call_log, docker_config)
        socket_path = root / "docker.sock"
        listener = None
        if create_socket:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            socket_path.chmod(0o600)
        codex = root / "unused-codex"
        codex.write_bytes(b"fixture-codex-binary")
        codex.chmod(0o700)
        runtime = DockerCodexRuntime(
            docker_path=docker,
            docker_host=f"unix://{socket_path}",
            cli_config_directory=docker_config,
            image=IMAGE,
            codex_sha256=sha256(codex.read_bytes()).hexdigest(),
            egress_proxy_url=ROOTLESS_HOST_PROXY_URL,
        )
        shared_home = root / "shared-codex-home"
        shared_home.mkdir(mode=0o700)
        auth = shared_home / "auth.json"
        auth.write_text("{}\n", encoding="utf-8")
        auth.chmod(0o600)
        schema = root / "agent-result.schema.json"
        schema.write_text("{}\n", encoding="utf-8")
        schema.chmod(0o600)
        turns = RunnerTurnExecutor(
            workspace=workspace,
            codex_path=codex,
            codex_home=shared_home,
            output_schema=schema,
            timeout_seconds=10,
            egress_proxy_url="http://127.0.0.1:3128",
            docker_runtime=runtime,
        )
        return workspace, turns, base_sha, call_log, socket_path, listener

    def test_start_and_resume_use_one_isolated_home_and_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace, turns, base_sha, call_log, socket_path, listener = self._setup(
                root
            )
            assert listener is not None
            try:
                with patch(
                    "codex_dispatcher.runner_docker._expected_rootless_socket",
                    return_value=socket_path,
                ):
                    first_prompt = b"first change"
                    first = turns.execute(
                        _request(
                            RunnerOperation.START,
                            TURN_ONE,
                            first_prompt,
                            base_sha,
                        ),
                        first_prompt,
                    )
                    assert first.head_sha is not None
                    second_prompt = b"second change"
                    second = turns.execute(
                        _request(
                            RunnerOperation.RESUME,
                            TURN_TWO,
                            second_prompt,
                            first.head_sha,
                            session_id=SESSION,
                        ),
                        second_prompt,
                    )
            finally:
                listener.close()

            self.assertEqual(RunnerTurnRemoteState.FINISHED, first.state)
            self.assertEqual(RunnerTurnRemoteState.FINISHED, second.state)
            self.assertEqual(SESSION, first.session_id)
            self.assertEqual(SESSION, second.session_id)
            paths = workspace.paths(WORK_ITEM)
            binding = json.loads(
                (paths.state / "codex-session.json").read_text(encoding="utf-8")
            )
            self.assertEqual(WORK_ITEM, binding["work_item_id"])
            self.assertEqual(SESSION, binding["session_id"])
            self.assertEqual(IMAGE, binding["image"])
            self.assertEqual(
                sha256((root / "unused-codex").read_bytes()).hexdigest(),
                binding["codex_sha256"],
            )
            self.assertTrue((paths.state / "codex-home").is_dir())
            self.assertEqual(
                ["auth", "turn", "auth", "turn"],
                call_log.read_text().splitlines(),
            )

    def test_missing_socket_is_a_durable_failure_without_starting_docker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, turns, base_sha, call_log, socket_path, _ = self._setup(
                root, create_socket=False
            )
            prompt = b"must not run"
            request = _request(RunnerOperation.START, TURN_ONE, prompt, base_sha)
            with patch(
                "codex_dispatcher.runner_docker._expected_rootless_socket",
                return_value=socket_path,
            ):
                reply = turns.execute(request, prompt)
                repeated = turns.execute(request, prompt)

            self.assertEqual(reply, repeated)
            self.assertEqual(RunnerTurnRemoteState.FAILED, reply.state)
            self.assertEqual("docker_boundary_invalid", reply.error_code)
            self.assertFalse(call_log.exists())

    def test_resume_without_exact_binding_fails_before_docker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace, turns, base_sha, call_log, socket_path, listener = self._setup(
                root
            )
            assert listener is not None
            paths = workspace.paths(WORK_ITEM)
            (paths.state / "codex-home").mkdir(mode=0o700)
            prompt = b"must not resume"
            request = _request(
                RunnerOperation.RESUME,
                TURN_ONE,
                prompt,
                base_sha,
                session_id=SESSION,
            )
            try:
                with patch(
                    "codex_dispatcher.runner_docker._expected_rootless_socket",
                    return_value=socket_path,
                ):
                    reply = turns.execute(request, prompt)
            finally:
                listener.close()

            self.assertEqual(RunnerTurnRemoteState.FAILED, reply.state)
            self.assertEqual("docker_boundary_invalid", reply.error_code)
            self.assertFalse(call_log.exists())

    def test_writable_auth_source_fails_before_docker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, turns, base_sha, call_log, socket_path, listener = self._setup(root)
            assert listener is not None
            (root / "shared-codex-home" / "auth.json").chmod(0o640)
            prompt = b"must not expose auth"
            try:
                with patch(
                    "codex_dispatcher.runner_docker._expected_rootless_socket",
                    return_value=socket_path,
                ):
                    reply = turns.execute(
                        _request(
                            RunnerOperation.START,
                            TURN_ONE,
                            prompt,
                            base_sha,
                        ),
                        prompt,
                    )
            finally:
                listener.close()

            self.assertEqual(RunnerTurnRemoteState.FAILED, reply.state)
            self.assertEqual("docker_boundary_invalid", reply.error_code)
            self.assertFalse(call_log.exists())

    def test_changed_host_codex_binary_fails_before_docker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, turns, base_sha, call_log, socket_path, listener = self._setup(root)
            assert listener is not None
            (root / "unused-codex").write_bytes(b"changed-after-runtime-binding")
            prompt = b"must not expose a changed tool"
            try:
                with patch(
                    "codex_dispatcher.runner_docker._expected_rootless_socket",
                    return_value=socket_path,
                ):
                    reply = turns.execute(
                        _request(
                            RunnerOperation.START,
                            TURN_ONE,
                            prompt,
                            base_sha,
                        ),
                        prompt,
                    )
            finally:
                listener.close()

            self.assertEqual(RunnerTurnRemoteState.FAILED, reply.state)
            self.assertEqual("docker_boundary_invalid", reply.error_code)
            self.assertFalse(call_log.exists())

    def test_drifted_image_or_network_fails_before_docker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, turns, base_sha, call_log, socket_path, listener = self._setup(root)
            assert listener is not None
            prompt = b"must not use drifted Docker assets"
            try:
                with (
                    patch(
                        "codex_dispatcher.runner_docker._expected_rootless_socket",
                        return_value=socket_path,
                    ),
                    patch(
                        "codex_dispatcher.runner_docker.run_command",
                        return_value=CommandResult(
                            0,
                            json.dumps(
                                {
                                    "Os": "linux",
                                    "Architecture": "amd64",
                                    "RepoDigests": ["registry.invalid/other@sha256:" + "f" * 64],
                                }
                            ),
                            "",
                        ),
                    ),
                ):
                    reply = turns.execute(
                        _request(RunnerOperation.START, TURN_ONE, prompt, base_sha),
                        prompt,
                    )
            finally:
                listener.close()

            self.assertEqual(RunnerTurnRemoteState.FAILED, reply.state)
            self.assertEqual("docker_boundary_invalid", reply.error_code)
            self.assertFalse(call_log.exists())

    def test_host_timeout_leaves_executing_record_for_status_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, turns, base_sha, _, socket_path, listener = self._setup(root)
            assert listener is not None
            prompt = b"ambiguous timeout"
            request = _request(RunnerOperation.START, TURN_ONE, prompt, base_sha)
            try:
                with (
                    patch(
                        "codex_dispatcher.runner_docker._expected_rootless_socket",
                        return_value=socket_path,
                    ),
                    patch(
                        "codex_dispatcher.runner_turns.run_binary_command",
                        side_effect=(
                            BinaryCommandResult(
                                0,
                                b"",
                                b"Logged in using ChatGPT\n",
                            ),
                            BinaryCommandResult(
                                None,
                                b"",
                                b"",
                                timed_out=True,
                                error="command timed out",
                            ),
                        ),
                    ),
                ):
                    with self.assertRaisesRegex(RunnerTurnError, "unresolved"):
                        turns.execute(request, prompt)
            finally:
                listener.close()

            status = turns.status(
                RunnerRequest(RunnerOperation.STATUS, WORK_ITEM, turn_id=TURN_ONE)
            )
            self.assertEqual(RunnerTurnRemoteState.UNKNOWN, status.state)
            self.assertEqual("turn_outcome_unresolved", status.error_code)


if __name__ == "__main__":
    unittest.main()
