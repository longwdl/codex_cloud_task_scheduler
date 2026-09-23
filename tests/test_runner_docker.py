from __future__ import annotations

import json
import shutil
import socket
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.executors.codex_docker import (
    DOCKER_LABEL_POLICY_DIGEST,
    DOCKER_LABEL_SESSION_GENERATION,
    DOCKER_LABEL_SESSION_GENERATION_ID,
    DOCKER_LABEL_TURN,
    DOCKER_LABEL_WORK_ITEM,
    DOCKER_NETWORK,
    DOCKER_NETWORK_GATEWAY,
    DOCKER_NETWORK_SUBNET,
    ROOTLESS_HOST_PROXY_URL,
    DockerCodexRuntime,
    build_docker_codex_plan as plan_docker_turn,
)
from codex_dispatcher.command_runner import BinaryCommandResult, CommandResult
from codex_dispatcher.codex_jsonl import CodexJsonlError
from codex_dispatcher.runner_docker import (
    DockerGenerationContainerState,
    RunnerDockerError,
    bind_docker_session as durable_bind_docker_session,
    docker_generation_container_is_running,
)
from codex_dispatcher.runner_protocol import (
    NEXT_PROTOCOL_VERSION,
    RunnerOperation,
    RunnerRequest,
)
from codex_dispatcher.runner_policy import PolicyBundle
from codex_dispatcher.runner_transport import RunnerTurnRemoteState
from codex_dispatcher.runner_turns import RunnerTurnError, RunnerTurnExecutor
from codex_dispatcher.runner_workspace import RunnerWorkspace
from tests.test_runner_workspace import GIT, WORK_ITEM, fixture, prepare_request


SESSION = "123e4567-e89b-12d3-a456-426614174000"
TURN_ONE = "turn_" + "1" * 32
TURN_TWO = "turn_" + "2" * 32
TURN_THREE = "turn_" + "3" * 32
GENERATION_ONE_ID = "sg_" + "1" * 32
GENERATION_TWO_ID = "sg_" + "2" * 32
IMAGE = "registry.example.invalid/codex-runner@sha256:" + "a" * 64
ROOT = Path(__file__).resolve().parents[1]


def _fixture_policy(root: Path) -> PolicyBundle:
    source = ROOT / "config" / "runner-codex-policy"
    destination = root / "runner-codex-policy"
    shutil.copytree(source, destination)
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    return PolicyBundle.load(destination, manifest["policy_digest"])


def _fake_docker(path: Path, call_log: Path, docker_config: Path) -> None:
    path.write_text(
        f"""#!{sys.executable}
import json
import os
import sqlite3
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

def mount_argument(target):
    for argument in args:
        if argument.startswith("--mount=") and f"target={{target}}" in argument:
            return argument
    raise AssertionError(f"missing mount {{target}}")

def mount_source(target):
    for field in mount_argument(target).split(","):
        if field.startswith("source="):
            return Path(field.removeprefix("source="))
    raise AssertionError(f"missing source for {{target}}")

assert mount_source("/usr/local/bin/codex").is_file()
assert mount_source("/usr/local/bin/codex-code-mode-host").is_file()
auth_file = mount_source("/codex-home") / "auth.json"
assert auth_file.name == "auth.json"
assert ",readonly" not in mount_argument("/codex-home")

state_database = auth_file.parent / "state_5.sqlite"
with sqlite3.connect(state_database) as state:
    state.execute(
        "CREATE TABLE IF NOT EXISTS thread_spawn_edges ("
        "parent_thread_id TEXT NOT NULL, child_thread_id TEXT PRIMARY KEY, "
        "status TEXT NOT NULL)"
    )
    state.execute(
        "CREATE TABLE IF NOT EXISTS threads ("
        "id TEXT PRIMARY KEY, cli_version TEXT NOT NULL, agent_role TEXT, "
        "model TEXT NOT NULL, reasoning_effort TEXT NOT NULL, "
        "tokens_used INTEGER NOT NULL)"
    )
state_database.chmod(0o600)

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
if "refresh auth" in prompt:
    refreshed = auth_file.with_name(".auth-refresh.tmp")
    refreshed.write_text('{{"refreshed":true}}\\n', encoding="utf-8")
    refreshed.chmod(0o600)
    refreshed.replace(auth_file)
if "corrupt auth" in prompt:
    auth_file.write_bytes(b"not-json")
session = codex[codex.index("resume") + 1] if "resume" in codex else {SESSION!r}
root_effort = "high" if "--profile" in codex and codex[codex.index("--profile") + 1] in {{"repair", "audit"}} else "medium"
with sqlite3.connect(state_database) as state:
    state.execute(
        "INSERT OR REPLACE INTO threads "
        "(id, cli_version, agent_role, model, reasoning_effort, tokens_used) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session, "0.147.0", None, "gpt-6-sol", root_effort, 100),
    )
    if "delegate luna" in prompt:
        child = "223e4567-e89b-12d3-a456-426614174000"
        state.execute(
            "INSERT OR REPLACE INTO threads "
            "(id, cli_version, agent_role, model, reasoning_effort, tokens_used) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (child, "0.147.0", "luna_worker", "gpt-6-luna", "medium", 42),
        )
        state.execute(
            "INSERT OR REPLACE INTO thread_spawn_edges "
            "(parent_thread_id, child_thread_id, status) VALUES (?, ?, ?)",
            (session, child, "closed"),
        )
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
turn_completed = {{"type": "turn.completed"}}
if "omit usage" not in prompt:
    turn_completed["usage"] = {{
        "input_tokens": 100,
        "cached_input_tokens": 20,
        "cache_write_input_tokens": 0,
        "output_tokens": 30,
        "reasoning_output_tokens": 10,
    }}
events = [
    {{"type": "thread.started", "thread_id": session}},
    {{"type": "turn.started"}},
    {{"type": "item.completed", "item": {{
        "type": "agent_message",
        "text": json.dumps(result, sort_keys=True, separators=(",", ":")),
    }}}},
    turn_completed,
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


def _v2_request(
    operation: RunnerOperation,
    turn_id: str,
    prompt: bytes,
    head: str,
    *,
    policy_digest: str,
    generation_id: str = GENERATION_ONE_ID,
    generation: int = 1,
    session_id: str | None = None,
    session_role: str | None = None,
) -> RunnerRequest:
    return RunnerRequest(
        operation,
        WORK_ITEM,
        turn_id=turn_id,
        session_id=session_id,
        prompt_sha256=sha256(prompt).hexdigest(),
        input_head_sha=head,
        session_generation_id=generation_id,
        session_generation=generation,
        agent_policy_digest=policy_digest,
        version=NEXT_PROTOCOL_VERSION,
        session_role=session_role,
    )


class RunnerDockerExecutionTests(unittest.TestCase):
    def _setup(
        self,
        root: Path,
        *,
        create_socket: bool = True,
        policy_bundle: PolicyBundle | None = None,
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
        code_mode_host = root / "codex-code-mode-host"
        code_mode_host.write_bytes(b"fixture-code-mode-host-binary")
        code_mode_host.chmod(0o700)
        runtime = DockerCodexRuntime(
            docker_path=docker,
            docker_host=f"unix://{socket_path}",
            cli_config_directory=docker_config,
            image=IMAGE,
            codex_sha256=sha256(codex.read_bytes()).hexdigest(),
            code_mode_host_path=code_mode_host,
            code_mode_host_sha256=sha256(
                code_mode_host.read_bytes()
            ).hexdigest(),
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
        audit_schema = root / "agent-result-audit.schema.json"
        audit_schema.write_text("{}\n", encoding="utf-8")
        audit_schema.chmod(0o600)
        turns = RunnerTurnExecutor(
            workspace=workspace,
            codex_path=codex,
            codex_home=shared_home,
            output_schema=schema,
            audit_output_schema=audit_schema,
            timeout_seconds=10,
            egress_proxy_url="http://127.0.0.1:3128",
            docker_runtime=runtime,
            policy_bundle=policy_bundle,
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
                    paths = workspace.paths(WORK_ITEM)
                    (paths.state / "codex-session-tools.json").unlink()
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
            self.assertEqual(1, binding["version"])
            self.assertNotIn("code_mode_host_sha256", binding)
            self.assertEqual(
                sha256((root / "unused-codex").read_bytes()).hexdigest(),
                binding["codex_sha256"],
            )
            tool_binding = json.loads(
                (paths.state / "codex-session-tools.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(1, tool_binding["version"])
            self.assertEqual(WORK_ITEM, tool_binding["work_item_id"])
            self.assertEqual(SESSION, tool_binding["session_id"])
            self.assertEqual(IMAGE, tool_binding["image"])
            self.assertEqual(binding["codex_sha256"], tool_binding["codex_sha256"])
            self.assertEqual(
                sha256(
                    (root / "codex-code-mode-host").read_bytes()
                ).hexdigest(),
                tool_binding["code_mode_host_sha256"],
            )
            self.assertTrue((paths.state / "codex-home").is_dir())
            source_auth = root / "shared-codex-home" / "auth.json"
            work_item_auth = paths.state / "codex-home" / "auth.json"
            auth_binding = json.loads(
                (paths.state / "codex-auth-binding.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(1, auth_binding["version"])
            self.assertEqual(WORK_ITEM, auth_binding["work_item_id"])
            self.assertEqual(
                sha256(source_auth.read_bytes()).hexdigest(),
                auth_binding["source_sha256"],
            )
            self.assertEqual(source_auth.read_bytes(), work_item_auth.read_bytes())
            self.assertNotEqual(source_auth.stat().st_ino, work_item_auth.stat().st_ino)
            self.assertEqual(0o600, work_item_auth.stat().st_mode & 0o777)
            self.assertEqual(1, work_item_auth.stat().st_nlink)
            self.assertEqual(
                ["auth", "turn", "auth", "turn"],
                call_log.read_text().splitlines(),
            )

    def test_work_item_auth_refresh_is_isolated_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace, turns, base_sha, _, socket_path, listener = self._setup(root)
            assert listener is not None
            source_auth = root / "shared-codex-home" / "auth.json"
            source_bytes = source_auth.read_bytes()
            try:
                with patch(
                    "codex_dispatcher.runner_docker._expected_rootless_socket",
                    return_value=socket_path,
                ):
                    first_prompt = b"first change refresh auth"
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
            self.assertEqual(source_bytes, source_auth.read_bytes())
            self.assertEqual(
                b'{"refreshed":true}\n',
                (
                    workspace.paths(WORK_ITEM).state / "codex-home" / "auth.json"
                ).read_bytes(),
            )

    def test_resume_seeds_one_legacy_auth_binding_without_rewriting_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace, turns, base_sha, _, socket_path, listener = self._setup(root)
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
                    paths = workspace.paths(WORK_ITEM)
                    session_binding = paths.state / "codex-session.json"
                    tool_binding = paths.state / "codex-session-tools.json"
                    session_bytes = session_binding.read_bytes()
                    tool_bytes = tool_binding.read_bytes()
                    (paths.state / "codex-home" / "auth.json").unlink()
                    (paths.state / "codex-auth-binding.json").unlink()
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

            self.assertEqual(RunnerTurnRemoteState.FINISHED, second.state)
            self.assertEqual(session_bytes, session_binding.read_bytes())
            self.assertEqual(tool_bytes, tool_binding.read_bytes())
            self.assertEqual(
                (root / "shared-codex-home" / "auth.json").read_bytes(),
                (paths.state / "codex-home" / "auth.json").read_bytes(),
            )

    def test_resume_rejects_incomplete_host_auth_binding(self) -> None:
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
                    (workspace.paths(WORK_ITEM).state / "codex-auth-binding.json").unlink()
                    second_prompt = b"must not resume"
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

            self.assertEqual(RunnerTurnRemoteState.FAILED, second.state)
            self.assertEqual("docker_boundary_invalid", second.error_code)
            self.assertEqual(["auth", "turn"], call_log.read_text().splitlines())

    def test_invalid_auth_mutation_is_a_durable_boundary_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, turns, base_sha, _, socket_path, listener = self._setup(root)
            assert listener is not None
            prompt = b"change corrupt auth"
            request = _request(RunnerOperation.START, TURN_ONE, prompt, base_sha)
            try:
                with patch(
                    "codex_dispatcher.runner_docker._expected_rootless_socket",
                    return_value=socket_path,
                ):
                    reply = turns.execute(request, prompt)
                    repeated = turns.execute(request, prompt)
            finally:
                listener.close()

            self.assertEqual(reply, repeated)
            self.assertEqual(RunnerTurnRemoteState.FAILED, reply.state)
            self.assertEqual("docker_boundary_invalid", reply.error_code)
            self.assertEqual(
                b"{}\n",
                (root / "shared-codex-home" / "auth.json").read_bytes(),
            )

    def test_new_start_reuses_only_bound_auth_after_login_preflight_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace, turns, base_sha, call_log, socket_path, listener = self._setup(
                root
            )
            assert listener is not None
            first_prompt = b"first attempt"
            try:
                with (
                    patch(
                        "codex_dispatcher.runner_docker._expected_rootless_socket",
                        return_value=socket_path,
                    ),
                    patch(
                        "codex_dispatcher.runner_turns.run_binary_command",
                        return_value=BinaryCommandResult(1, b"", b"logged out\n"),
                    ),
                ):
                    first = turns.execute(
                        _request(
                            RunnerOperation.START,
                            TURN_ONE,
                            first_prompt,
                            base_sha,
                        ),
                        first_prompt,
                    )
                with patch(
                    "codex_dispatcher.runner_docker._expected_rootless_socket",
                    return_value=socket_path,
                ):
                    second_prompt = b"second change"
                    second = turns.execute(
                        _request(
                            RunnerOperation.START,
                            TURN_TWO,
                            second_prompt,
                            base_sha,
                        ),
                        second_prompt,
                    )
            finally:
                listener.close()

            self.assertEqual(RunnerTurnRemoteState.FAILED, first.state)
            self.assertEqual("codex_auth_invalid", first.error_code)
            self.assertEqual(RunnerTurnRemoteState.FINISHED, second.state)
            paths = workspace.paths(WORK_ITEM)
            self.assertTrue((paths.state / "codex-auth-binding.json").is_file())
            self.assertEqual(["auth", "turn"], call_log.read_text().splitlines())

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

    def test_v1_ignores_configured_v2_policy_and_preserves_legacy_argv(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            policy = _fixture_policy(root)
            _, turns, base_sha, call_log, socket_path, listener = self._setup(
                root,
                policy_bundle=policy,
            )
            assert listener is not None
            try:
                with patch(
                    "codex_dispatcher.runner_docker._expected_rootless_socket",
                    return_value=socket_path,
                ):
                    prompt = b"legacy request"
                    reply = turns.execute(
                        _request(RunnerOperation.START, TURN_ONE, prompt, base_sha),
                        prompt,
                    )
            finally:
                listener.close()

            self.assertEqual(RunnerTurnRemoteState.FINISHED, reply.state)
            docker_argv = call_log.read_text(encoding="utf-8")
            self.assertNotIn(str(policy.root), docker_argv)
            self.assertNotIn("--strict-config", docker_argv)
            self.assertNotIn("gpt-5.6-sol", docker_argv)
            self.assertNotIn("multi_agent", docker_argv)

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

    def test_resume_rejects_drifted_code_mode_host_session_binding(self) -> None:
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
                    binding_path = (
                        workspace.paths(WORK_ITEM).state
                        / "codex-session-tools.json"
                    )
                    binding = json.loads(binding_path.read_text(encoding="utf-8"))
                    binding["code_mode_host_sha256"] = "f" * 64
                    binding_path.write_text(json.dumps(binding), encoding="utf-8")
                    binding_path.chmod(0o600)
                    second_prompt = b"must not resume"
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
            self.assertEqual(RunnerTurnRemoteState.FAILED, second.state)
            self.assertEqual("docker_boundary_invalid", second.error_code)
            self.assertEqual(["auth", "turn"], call_log.read_text().splitlines())

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

    def test_missing_or_changed_code_mode_host_fails_before_docker(self) -> None:
        for failure in ("missing", "changed"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _, turns, base_sha, call_log, socket_path, listener = self._setup(root)
                assert listener is not None
                code_mode_host = root / "codex-code-mode-host"
                if failure == "missing":
                    code_mode_host.unlink()
                else:
                    code_mode_host.write_bytes(b"changed-after-runtime-binding")
                prompt = b"must not expose an incomplete Codex tool bundle"
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

    def test_v2_generations_use_separate_homes_usage_and_latest_resume(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            policy = _fixture_policy(root)
            workspace, turns, base_sha, call_log, socket_path, listener = self._setup(
                root,
                policy_bundle=policy,
            )
            assert listener is not None
            try:
                with patch(
                    "codex_dispatcher.runner_docker._expected_rootless_socket",
                    return_value=socket_path,
                ):
                    first_prompt = b"first change"
                    first = turns.execute(
                        _v2_request(
                            RunnerOperation.START,
                            TURN_ONE,
                            first_prompt,
                            base_sha,
                            policy_digest=policy.policy_digest,
                        ),
                        first_prompt,
                    )
                    assert first.head_sha is not None
                    second_prompt = b"second change"
                    second = turns.execute(
                        _v2_request(
                            RunnerOperation.START,
                            TURN_TWO,
                            second_prompt,
                            first.head_sha,
                            policy_digest=policy.policy_digest,
                            generation_id=GENERATION_TWO_ID,
                            generation=2,
                        ),
                        second_prompt,
                    )
                    assert second.head_sha is not None
                    old_prompt = b"must not resume old generation"
                    old = turns.execute(
                        _v2_request(
                            RunnerOperation.RESUME,
                            TURN_THREE,
                            old_prompt,
                            second.head_sha,
                            policy_digest=policy.policy_digest,
                            session_id=SESSION,
                        ),
                        old_prompt,
                    )
            finally:
                listener.close()

            self.assertEqual(RunnerTurnRemoteState.FINISHED, first.state)
            self.assertEqual(NEXT_PROTOCOL_VERSION, first.version)
            self.assertEqual(GENERATION_ONE_ID, first.session_generation_id)
            self.assertEqual(policy.policy_digest, first.agent_policy_digest)
            self.assertIsNotNone(first.usage)
            assert first.usage is not None
            self.assertEqual(100, first.usage.input_tokens)
            self.assertEqual(RunnerTurnRemoteState.FINISHED, second.state)
            self.assertEqual(RunnerTurnRemoteState.FAILED, old.state)
            self.assertEqual("docker_boundary_invalid", old.error_code)
            generation_root = workspace.paths(WORK_ITEM).state / "generations"
            homes = {
                generation_id: generation_root / generation_id / "codex-home"
                for generation_id in (GENERATION_ONE_ID, GENERATION_TWO_ID)
            }
            self.assertTrue(all(home.is_dir() for home in homes.values()))
            self.assertNotEqual(
                homes[GENERATION_ONE_ID].stat().st_ino,
                homes[GENERATION_TWO_ID].stat().st_ino,
            )
            for generation, generation_id in enumerate(homes, start=1):
                record = json.loads(
                    (generation_root / generation_id / "generation.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(generation, record["session_generation"])
                self.assertEqual(policy.policy_digest, record["agent_policy_digest"])
                self.assertEqual(IMAGE, record["image"])
            self.assertEqual(
                ["auth", "turn", "auth", "turn"],
                call_log.read_text().splitlines(),
            )

    def test_v2_audit_uses_audit_schema_and_readonly_repository(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            policy = _fixture_policy(root)
            _, turns, base_sha, _, socket_path, listener = self._setup(
                root, policy_bundle=policy
            )
            assert listener is not None
            prompt = b"audit readonly"
            try:
                with (
                    patch(
                        "codex_dispatcher.runner_docker._expected_rootless_socket",
                        return_value=socket_path,
                    ),
                    patch(
                        "codex_dispatcher.runner_turns.build_docker_codex_plan",
                        wraps=plan_docker_turn,
                    ) as planned,
                ):
                    reply = turns.execute(
                        _v2_request(
                            RunnerOperation.START,
                            TURN_ONE,
                            prompt,
                            base_sha,
                            policy_digest=policy.policy_digest,
                            session_role="audit",
                        ),
                        prompt,
                    )
            finally:
                listener.close()

            self.assertEqual(RunnerTurnRemoteState.FINISHED, reply.state)
            self.assertEqual(
                root / "agent-result-audit.schema.json",
                planned.call_args.kwargs["output_schema"],
            )
            self.assertTrue(planned.call_args.kwargs["repository_readonly"])

    def test_v2_returns_policy_verified_delegation_metadata(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            policy = _fixture_policy(root)
            _, turns, base_sha, _, socket_path, listener = self._setup(
                root,
                policy_bundle=policy,
            )
            assert listener is not None
            prompt = b"delegate luna"
            try:
                with patch(
                    "codex_dispatcher.runner_docker._expected_rootless_socket",
                    return_value=socket_path,
                ):
                    reply = turns.execute(
                        _v2_request(
                            RunnerOperation.START,
                            TURN_ONE,
                            prompt,
                            base_sha,
                            policy_digest=policy.policy_digest,
                        ),
                        prompt,
                    )
            finally:
                listener.close()

            self.assertEqual(RunnerTurnRemoteState.FINISHED, reply.state)
            self.assertIsNotNone(reply.delegation_receipt)
            assert reply.delegation_receipt is not None
            self.assertEqual("gpt-6-sol", reply.delegation_receipt.root_model)
            self.assertEqual(1, len(reply.delegation_receipt.agents))
            self.assertEqual(
                "luna_worker", reply.delegation_receipt.agents[0].agent_name
            )
            self.assertEqual(42, reply.delegation_receipt.agents[0].tokens_used)

    def test_v2_policy_mismatch_and_drift_reject_before_docker(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            policy = _fixture_policy(root)
            workspace, turns, base_sha, call_log, _, listener = self._setup(
                root,
                policy_bundle=policy,
            )
            assert listener is not None
            prompt = b"must not run"
            try:
                mismatched = _v2_request(
                    RunnerOperation.START,
                    TURN_ONE,
                    prompt,
                    base_sha,
                    policy_digest="f" * 64,
                )
                with self.assertRaisesRegex(RunnerTurnError, "policy digest"):
                    turns.execute(mismatched, prompt)

                policy.config_path.write_text("drifted = true\n", encoding="utf-8")
                drifted = _v2_request(
                    RunnerOperation.START,
                    TURN_TWO,
                    prompt,
                    base_sha,
                    policy_digest=policy.policy_digest,
                )
                with self.assertRaisesRegex(RunnerTurnError, "policy is invalid"):
                    turns.execute(drifted, prompt)
            finally:
                listener.close()

            self.assertFalse(call_log.exists())
            self.assertFalse((workspace.paths(WORK_ITEM).state / "turns").exists())

    def test_v2_completed_output_without_usage_is_durable_failure(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            policy = _fixture_policy(root)
            _, turns, base_sha, _, socket_path, listener = self._setup(
                root,
                policy_bundle=policy,
            )
            assert listener is not None
            prompt = b"omit usage"
            request = _v2_request(
                RunnerOperation.START,
                TURN_ONE,
                prompt,
                base_sha,
                policy_digest=policy.policy_digest,
            )
            try:
                with patch(
                    "codex_dispatcher.runner_docker._expected_rootless_socket",
                    return_value=socket_path,
                ):
                    reply = turns.execute(request, prompt)
                    repeated = turns.execute(request, prompt)
            finally:
                listener.close()

            self.assertEqual(reply, repeated)
            self.assertEqual(RunnerTurnRemoteState.FAILED, reply.state)
            self.assertEqual("codex_output_usage_missing", reply.error_code)
            self.assertEqual(NEXT_PROTOCOL_VERSION, reply.version)
            self.assertEqual(GENERATION_ONE_ID, reply.session_generation_id)
            self.assertEqual(policy.policy_digest, reply.agent_policy_digest)

    def test_v2_stream_receipt_precedes_terminal_output_validation(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            policy = _fixture_policy(root)
            workspace, turns, base_sha, _, socket_path, listener = self._setup(
                root,
                policy_bundle=policy,
            )
            assert listener is not None
            prompt = b"terminal parse fails"
            request = _v2_request(
                RunnerOperation.START,
                TURN_ONE,
                prompt,
                base_sha,
                policy_digest=policy.policy_digest,
            )
            try:
                with (
                    patch(
                        "codex_dispatcher.runner_docker._expected_rootless_socket",
                        return_value=socket_path,
                    ),
                    patch(
                        "codex_dispatcher.runner_turns.parse_codex_jsonl",
                        side_effect=CodexJsonlError(
                            "fixture",
                            code="fixture_invalid",
                        ),
                    ),
                ):
                    reply = turns.execute(request, prompt)
            finally:
                listener.close()

            self.assertEqual(RunnerTurnRemoteState.FAILED, reply.state)
            self.assertEqual("codex_output_fixture_invalid", reply.error_code)
            generation = (
                workspace.paths(WORK_ITEM).state
                / "generations"
                / GENERATION_ONE_ID
            )
            session = json.loads(
                (generation / "codex-session.json").read_text(encoding="utf-8")
            )
            tools = json.loads(
                (generation / "codex-session-tools.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(SESSION, session["session_id"])
            self.assertEqual(SESSION, tools["session_id"])

    def test_v2_stdout_hook_failure_can_recover_from_full_output(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            policy = _fixture_policy(root)
            _, turns, base_sha, _, socket_path, listener = self._setup(
                root,
                policy_bundle=policy,
            )
            assert listener is not None
            prompt = b"recover receipt"
            request = _v2_request(
                RunnerOperation.START,
                TURN_ONE,
                prompt,
                base_sha,
                policy_digest=policy.policy_digest,
            )
            attempts = 0

            def transient_bind(context, *, work_item_id, session_id):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RunnerDockerError("fixture transient receipt failure")
                return durable_bind_docker_session(
                    context,
                    work_item_id=work_item_id,
                    session_id=session_id,
                )

            try:
                with (
                    patch(
                        "codex_dispatcher.runner_docker._expected_rootless_socket",
                        return_value=socket_path,
                    ),
                    patch(
                        "codex_dispatcher.runner_turns.bind_docker_session",
                        side_effect=transient_bind,
                    ),
                ):
                    reply = turns.execute(request, prompt)
            finally:
                listener.close()

            self.assertEqual(2, attempts)
            self.assertEqual(RunnerTurnRemoteState.FINISHED, reply.state)
            self.assertEqual(SESSION, reply.session_id)
            self.assertIsNotNone(reply.usage)

    def test_v2_executing_receipt_status_and_replay_never_restart(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            policy = _fixture_policy(root)
            _, turns, base_sha, _, socket_path, listener = self._setup(
                root,
                policy_bundle=policy,
            )
            assert listener is not None
            prompt = b"ambiguous timeout"
            request = _v2_request(
                RunnerOperation.START,
                TURN_ONE,
                prompt,
                base_sha,
                policy_digest=policy.policy_digest,
            )
            calls = 0

            def run_with_receipt(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return BinaryCommandResult(
                        0,
                        b"",
                        b"Logged in using ChatGPT\n",
                    )
                hook = kwargs["stdout_line_hook"]
                hook(
                    json.dumps(
                        {"type": "thread.started", "thread_id": SESSION}
                    ).encode("utf-8")
                )
                return BinaryCommandResult(
                    None,
                    b"",
                    b"",
                    timed_out=True,
                    error="command timed out",
                )

            try:
                with (
                    patch(
                        "codex_dispatcher.runner_docker._expected_rootless_socket",
                        return_value=socket_path,
                    ),
                    patch(
                        "codex_dispatcher.runner_turns.run_binary_command",
                        side_effect=run_with_receipt,
                    ),
                ):
                    with self.assertRaisesRegex(RunnerTurnError, "unresolved"):
                        turns.execute(request, prompt)
                status_request = RunnerRequest(
                    RunnerOperation.STATUS,
                    WORK_ITEM,
                    turn_id=TURN_ONE,
                    session_generation_id=GENERATION_ONE_ID,
                    session_generation=1,
                    agent_policy_digest=policy.policy_digest,
                    version=NEXT_PROTOCOL_VERSION,
                )
                with (
                    patch(
                        "codex_dispatcher.runner_docker._expected_rootless_socket",
                        return_value=socket_path,
                    ),
                    patch(
                        "codex_dispatcher.runner_turns.inspect_docker_generation_container",
                        return_value=DockerGenerationContainerState.RUNNING,
                    ),
                ):
                    status = turns.status(status_request)
                    replay = turns.execute(request, prompt)
                with (
                    patch(
                        "codex_dispatcher.runner_docker._expected_rootless_socket",
                        return_value=socket_path,
                    ),
                    patch(
                        "codex_dispatcher.runner_turns.inspect_docker_generation_container",
                        return_value=DockerGenerationContainerState.ABSENT,
                    ),
                ):
                    unknown = turns.status(status_request)
                with (
                    patch(
                        "codex_dispatcher.runner_docker._expected_rootless_socket",
                        return_value=socket_path,
                    ),
                    patch(
                        "codex_dispatcher.runner_turns.inspect_docker_generation_container",
                        side_effect=RunnerDockerError("fixture unavailable"),
                    ),
                ):
                    unavailable = turns.status(status_request)
                stop_request = RunnerRequest(
                    RunnerOperation.STOP,
                    WORK_ITEM,
                    turn_id=TURN_ONE,
                    session_generation_id=GENERATION_ONE_ID,
                    session_generation=1,
                    agent_policy_digest=policy.policy_digest,
                    version=NEXT_PROTOCOL_VERSION,
                )
                with (
                    patch(
                        "codex_dispatcher.runner_docker._expected_rootless_socket",
                        return_value=socket_path,
                    ),
                    patch(
                        "codex_dispatcher.runner_turns.inspect_docker_generation_container",
                        return_value=DockerGenerationContainerState.RUNNING,
                    ),
                    self.assertRaisesRegex(RunnerTurnError, "running Turn"),
                ):
                    turns.abandon(stop_request)
                with (
                    patch(
                        "codex_dispatcher.runner_docker._expected_rootless_socket",
                        return_value=socket_path,
                    ),
                    patch(
                        "codex_dispatcher.runner_turns.inspect_docker_generation_container",
                        return_value=DockerGenerationContainerState.ABSENT,
                    ),
                ):
                    abandoned = turns.abandon(stop_request)
                terminal_status = turns.status(status_request)
                terminal_replay = turns.execute(request, prompt)
                mismatched_status = RunnerRequest(
                    RunnerOperation.STATUS,
                    WORK_ITEM,
                    turn_id=TURN_ONE,
                    session_generation_id=GENERATION_TWO_ID,
                    session_generation=2,
                    agent_policy_digest=policy.policy_digest,
                    version=NEXT_PROTOCOL_VERSION,
                )
                with self.assertRaisesRegex(RunnerTurnError, "generation identity"):
                    turns.status(mismatched_status)
            finally:
                listener.close()

            self.assertEqual(2, calls)
            self.assertEqual(RunnerTurnRemoteState.RUNNING, status.state)
            self.assertEqual(SESSION, status.session_id)
            self.assertEqual(RunnerTurnRemoteState.RUNNING, replay.state)
            self.assertEqual(SESSION, replay.session_id)
            self.assertEqual(RunnerTurnRemoteState.UNKNOWN, unknown.state)
            self.assertEqual(SESSION, unknown.session_id)
            self.assertEqual("turn_container_inactive", unknown.error_code)
            self.assertEqual("absent", unknown.inactive_container_state.value)
            self.assertEqual(
                "turn_container_observation_unavailable", unavailable.error_code
            )
            self.assertIsNone(unavailable.inactive_container_state)
            self.assertEqual(RunnerOperation.STOP, abandoned.operation)
            self.assertEqual(RunnerTurnRemoteState.FAILED, abandoned.state)
            self.assertEqual("turn_abandoned_inactive", abandoned.error_code)
            self.assertEqual("absent", abandoned.inactive_container_state.value)
            self.assertEqual(RunnerOperation.STATUS, terminal_status.operation)
            self.assertEqual("turn_abandoned_inactive", terminal_status.error_code)
            self.assertEqual("turn_abandoned_inactive", terminal_replay.error_code)
            self.assertEqual(2, calls)

    def test_v2_running_container_proof_requires_exact_identity(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            policy = _fixture_policy(root)
            _, turns, base_sha, _, socket_path, listener = self._setup(
                root,
                policy_bundle=policy,
            )
            assert listener is not None
            prompt = b"proof"
            request = _v2_request(
                RunnerOperation.START,
                TURN_ONE,
                prompt,
                base_sha,
                policy_digest=policy.policy_digest,
            )
            runtime = turns._docker_runtime
            assert runtime is not None
            labels = {
                DOCKER_LABEL_WORK_ITEM: WORK_ITEM,
                DOCKER_LABEL_SESSION_GENERATION_ID: GENERATION_ONE_ID,
                DOCKER_LABEL_SESSION_GENERATION: "1",
                DOCKER_LABEL_TURN: TURN_ONE,
                DOCKER_LABEL_POLICY_DIGEST: policy.policy_digest,
            }
            payload = {
                "Name": f"/codex-{TURN_ONE}",
                "Config": {"Image": IMAGE, "Labels": labels},
                "HostConfig": {"NetworkMode": DOCKER_NETWORK},
                "NetworkSettings": {"Networks": {DOCKER_NETWORK: {}}},
                "State": {
                    "Running": True,
                    "Paused": False,
                    "Restarting": False,
                    "Dead": False,
                    "Pid": 123,
                },
            }
            try:
                with (
                    patch(
                        "codex_dispatcher.runner_docker._expected_rootless_socket",
                        return_value=socket_path,
                    ),
                    patch(
                        "codex_dispatcher.runner_docker.run_command",
                        side_effect=lambda *args, **kwargs: CommandResult(
                            0,
                            json.dumps(payload),
                            "",
                        ),
                    ),
                ):
                    self.assertTrue(
                        docker_generation_container_is_running(
                            runtime=runtime,
                            request=request,
                        )
                    )
                    labels.pop(DOCKER_LABEL_POLICY_DIGEST)
                    with self.assertRaisesRegex(
                        RunnerDockerError, "identity"
                    ):
                        docker_generation_container_is_running(
                            runtime=runtime,
                            request=request,
                        )
                missing = (
                    "Error response from daemon: No such container: "
                    f"codex-{TURN_ONE}\n"
                )
                with (
                    patch(
                        "codex_dispatcher.runner_docker._expected_rootless_socket",
                        return_value=socket_path,
                    ),
                    patch(
                        "codex_dispatcher.runner_docker.run_command",
                        return_value=CommandResult(1, "", missing),
                    ),
                ):
                    self.assertFalse(
                        docker_generation_container_is_running(
                            runtime=runtime,
                            request=request,
                        )
                    )
                with (
                    patch(
                        "codex_dispatcher.runner_docker._expected_rootless_socket",
                        return_value=socket_path,
                    ),
                    patch(
                        "codex_dispatcher.runner_docker.run_command",
                        return_value=CommandResult(1, "", "daemon unavailable\n"),
                    ),
                    self.assertRaisesRegex(RunnerDockerError, "inspection"),
                ):
                    docker_generation_container_is_running(
                        runtime=runtime,
                        request=request,
                    )
            finally:
                listener.close()

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
