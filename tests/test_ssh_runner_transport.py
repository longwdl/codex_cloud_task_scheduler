from __future__ import annotations

import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.command_runner import BinaryCommandResult
from codex_dispatcher.runner_protocol import RunnerOperation, RunnerRequest
from codex_dispatcher.runner_transport import (
    RunnerAck,
    RunnerTransportInterrupted,
    RunnerTransportRejected,
    RunnerWireOutput,
)
from codex_dispatcher.runner_wire import encode_runner_output
from codex_dispatcher.ssh_runner_transport import SshRunnerTransport


WORK_ITEM = "wi_" + "a" * 24
SOURCE_BUNDLE = b"fixture source bundle"


def transport(root: Path, *, assh_proxy_path: Path | None = None) -> SshRunnerTransport:
    known_hosts = root / "known_hosts"
    known_hosts.write_text("runner.invalid ssh-ed25519 AAAAFIXTURE\n", encoding="utf-8")
    known_hosts.chmod(0o600)
    identity = root / "runner_key"
    identity.write_text("fixture private key placeholder\n", encoding="utf-8")
    identity.chmod(0o600)
    assh_home = None
    if assh_proxy_path is not None:
        assh_home = root / "assh-home"
        assh_home.mkdir(mode=0o700)
    return SshRunnerTransport(
        ssh_path=Path("/usr/bin/ssh"),
        host="runner.invalid",
        user="codex_runner",
        port=2222,
        known_hosts_path=known_hosts,
        identity_file=identity,
        assh_proxy_path=assh_proxy_path,
        assh_home=assh_home,
    )


class SshRunnerTransportTests(unittest.TestCase):
    def test_plan_disables_config_forwarding_prompts_and_unpinned_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = transport(Path(temp_dir)).invocation_plan()
        self.assertEqual("/usr/bin/ssh", plan.argv[0])
        self.assertIn("/dev/null", plan.argv)
        for option in (
            "BatchMode=yes",
            "ClearAllForwardings=yes",
            "ForwardAgent=no",
            "ForwardX11=no",
            "StrictHostKeyChecking=yes",
            "ProxyCommand=none",
            "ProxyJump=none",
            "RequestTTY=no",
            "ControlMaster=no",
            "IdentityAgent=none",
        ):
            self.assertIn(option, plan.argv)
        self.assertEqual(("codex_runner@runner.invalid", "codex-runner-v1"), plan.argv[-2:])
        self.assertNotIn("SSH_AUTH_SOCK", plan.environment)
        self.assertNotIn("GITHUB_TOKEN", plan.environment)

    def test_plan_can_use_only_the_fixed_assh_proxy_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assh = root / "assh proxy"
            assh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            assh.chmod(0o700)
            plan = transport(root, assh_proxy_path=assh).invocation_plan()

        self.assertIn(
            f"ProxyCommand='{assh}' connect --port=%p %h",
            plan.argv,
        )
        self.assertIn("ProxyJump=none", plan.argv)
        self.assertEqual(str(root / "assh-home"), plan.environment["HOME"])

    def test_invoke_sends_framed_stdin_and_decodes_success_without_logging_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = transport(Path(temp_dir))
            request = RunnerRequest(
                RunnerOperation.PREPARE,
                WORK_ITEM,
                repository="owner/repo",
                issue_number=1,
                task_branch="codex/issue-1-aaaaaaaaaaaa",
                base_sha="b" * 40,
                source_bundle_sha256=sha256(SOURCE_BUNDLE).hexdigest(),
                source_bundle_size=len(SOURCE_BUNDLE),
            )
            ack = RunnerAck(RunnerOperation.PREPARE, WORK_ITEM)
            stdout = encode_runner_output(
                RunnerWireOutput(ack.to_json().encode())
            )
            with patch(
                "codex_dispatcher.ssh_runner_transport.run_binary_command",
                return_value=BinaryCommandResult(0, stdout, b""),
            ) as runner:
                output = adapter.invoke(request, source_artifact=SOURCE_BUNDLE)
        self.assertEqual(ack.to_json().encode(), output.payload)
        kwargs = runner.call_args.kwargs
        self.assertTrue(kwargs["input_bytes"].startswith(b"CODEX-RUNNER-REQUEST/1\n"))
        self.assertNotIn(request.to_json(), runner.call_args.args[0])

    def test_ambiguous_and_confirmed_failures_are_distinct(self) -> None:
        request = RunnerRequest(
            RunnerOperation.PREPARE,
            WORK_ITEM,
            repository="owner/repo",
            issue_number=1,
            task_branch="codex/issue-1-aaaaaaaaaaaa",
            base_sha="b" * 40,
            source_bundle_sha256=sha256(SOURCE_BUNDLE).hexdigest(),
            source_bundle_size=len(SOURCE_BUNDLE),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = transport(Path(temp_dir))
            with patch(
                "codex_dispatcher.ssh_runner_transport.run_binary_command",
                return_value=BinaryCommandResult(255, b"", b"connection lost"),
            ):
                with self.assertRaises(RunnerTransportInterrupted):
                    adapter.invoke(request, source_artifact=SOURCE_BUNDLE)
            with patch(
                "codex_dispatcher.ssh_runner_transport.run_binary_command",
                return_value=BinaryCommandResult(2, b"", b"rejected"),
            ):
                with self.assertRaises(RunnerTransportRejected):
                    adapter.invoke(request, source_artifact=SOURCE_BUNDLE)
            with patch(
                "codex_dispatcher.ssh_runner_transport.run_binary_command",
                return_value=BinaryCommandResult(3, b"", b"internal failure"),
            ):
                with self.assertRaises(RunnerTransportInterrupted):
                    adapter.invoke(request, source_artifact=SOURCE_BUNDLE)
            with patch(
                "codex_dispatcher.ssh_runner_transport.run_binary_command",
                return_value=BinaryCommandResult(0, b"malformed", b""),
            ):
                with self.assertRaises(RunnerTransportInterrupted):
                    adapter.invoke(request, source_artifact=SOURCE_BUNDLE)

    def test_rejects_unsafe_host_and_file_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            known_hosts = root / "known_hosts"
            known_hosts.write_text("fixture\n", encoding="utf-8")
            known_hosts.chmod(0o666)
            identity = root / "key"
            identity.write_text("fixture\n", encoding="utf-8")
            identity.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "protected"):
                SshRunnerTransport(
                    ssh_path=Path("/usr/bin/ssh"),
                    host="runner.invalid",
                    user="codex_runner",
                    port=22,
                    known_hosts_path=known_hosts,
                    identity_file=identity,
                )
            known_hosts.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "host"):
                SshRunnerTransport(
                    ssh_path=Path("/usr/bin/ssh"),
                    host="-oProxyCommand=evil",
                    user="codex_runner",
                    port=22,
                    known_hosts_path=known_hosts,
                    identity_file=identity,
                )

            unsafe_assh = root / "assh"
            unsafe_assh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            unsafe_assh.chmod(0o777)
            with self.assertRaisesRegex(ValueError, "protected executable"):
                SshRunnerTransport(
                    ssh_path=Path("/usr/bin/ssh"),
                    host="runner.invalid",
                    user="codex_runner",
                    port=22,
                    known_hosts_path=known_hosts,
                    identity_file=identity,
                    assh_proxy_path=unsafe_assh,
                    assh_home=root,
                )

            safe_assh = root / "safe-assh"
            safe_assh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            safe_assh.chmod(0o700)
            with self.assertRaisesRegex(ValueError, "configured together"):
                SshRunnerTransport(
                    ssh_path=Path("/usr/bin/ssh"),
                    host="runner.invalid",
                    user="codex_runner",
                    port=22,
                    known_hosts_path=known_hosts,
                    identity_file=identity,
                    assh_proxy_path=safe_assh,
                )


if __name__ == "__main__":
    unittest.main()
