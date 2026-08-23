from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "codex-dispatcher-release-v1"
RUNNER_VALIDATOR = ROOT / "scripts" / "codex-runner-release-validate-v1"


class ReleaseDeploymentTests(unittest.TestCase):
    def test_release_tool_has_fixed_two_host_transaction_boundary(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertNotEqual(0, SCRIPT.stat().st_mode & 0o111)
        self.assertIn("set -eu", text)
        self.assertIn("umask 077", text)
        self.assertIn("/var/tmp/codex-dispatcher-release-", text)
        self.assertIn("sha256sum", text)
        self.assertIn("tarfile.open", text)
        self.assertIn("member.isdir() or member.isfile()", text)
        self.assertIn("release-validation-", text)
        self.assertIn("-m 0700 /run/codex-dispatcher", text)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", text)
        self.assertIn("PYTHONPATH=src:.", text)
        self.assertIn("umask 077; cd", text)
        self.assertEqual(2, text.count("umask 077; exec /usr/bin/flock"))
        self.assertIn("inactive|failed", text)
        self.assertIn("active_service_timeout", text)
        self.assertIn("--wait-active-seconds", text)
        self.assertIn("systemctl mask --runtime", text)
        self.assertNotIn("systemctl stop $control_services", text)
        self.assertIn("codex-runner-release-validate-v1", text)
        self.assertIn('/usr/bin/chmod 0700 "$runner_validation"', text)
        self.assertIn("codex-runner", text)
        self.assertLess(
            text.index("/srv/codex-runner/current.next"),
            text.index("/opt/codex-dispatcher/current.next"),
        )
        self.assertIn("current.rollback", text)
        self.assertIn("release links changed after activation", text)
        self.assertIn("configuration changed after activation", text)
        self.assertLess(
            text.index("record_phase quiesce"),
            text.index("dispatcher_invocation_id_before=$(/usr/bin/systemctl"),
        )
        self.assertLess(
            text.index("dispatcher_invocation_id_before=$(/usr/bin/systemctl"),
            text.index("record_phase stage_control_intent"),
        )
        self.assertIn("candidate release already exists", text)
        self.assertIn('created_control=0', text)
        self.assertIn('created_runner=0', text)
        self.assertIn('if [ "$created_control" -eq 1 ]', text)
        self.assertIn('if [ "$created_runner" -eq 1 ]', text)
        self.assertIn('"timers_started":false', text)
        self.assertIn('"requires_manual_sweep":true', text)
        self.assertNotIn("eval ", text)
        self.assertNotIn("release'/.'", text)
        self.assertNotIn("--force", text)

    def test_release_tool_has_durable_v2_receipts_and_read_only_plan(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("--plan", text)
        self.assertIn("--status", text)
        self.assertIn("--rollback", text)
        self.assertIn("release-receipts", text)
        self.assertIn('"schema_version": 2', text)
        self.assertIn("tempfile.mkstemp", text)
        self.assertIn("os.fsync(stream.fileno())", text)
        self.assertIn("os.replace(raw, path)", text)
        self.assertIn("os.fsync(directory)", text)
        self.assertIn('"authorizes_apply": False', text)
        self.assertIn('"state_writes": 0', text)
        self.assertIn("commit_intent=1", text)
        self.assertLess(text.index("commit_intent=1"), text.index("receipt_write committed"))
        self.assertLess(
            text.index("receipt_write committed"),
            text.index('/usr/bin/printf \'{"ok":true'),
        )

    def test_release_tool_separates_signal_and_exit_traps(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("trap cleanup EXIT", text)
        self.assertIn("trap 'exit 129' HUP", text)
        self.assertIn("trap 'exit 130' INT", text)
        self.assertIn("trap 'exit 143' TERM", text)
        self.assertNotIn("trap cleanup EXIT HUP INT TERM", text)

    def test_release_tool_supports_paired_atomic_configuration(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("--config-sha256", text)
        self.assertIn('/var/tmp/codex-dispatcher-config-$commit.toml', text)
        self.assertIn('"PYTHONPATH=$previous_control/src:$previous_control"', text)
        self.assertIn('"PYTHONPATH=$control_release/src:$control_release"', text)
        self.assertIn("config-backups", text)
        self.assertIn("config.toml.next", text)
        self.assertIn("config.toml.rollback", text)

    def test_runner_validator_enforces_protected_cwd_and_umask(self) -> None:
        text = RUNNER_VALIDATOR.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertNotEqual(0, RUNNER_VALIDATOR.stat().st_mode & 0o111)
        self.assertIn("umask 077", text)
        self.assertIn("release-validation-", text)
        self.assertIn('= 700 ]', text)
        self.assertIn('cd "$validation_root"', text)
        self.assertIn("unittest discover", text)

    def test_release_tool_rejects_unstructured_invocations_before_sudo(self) -> None:
        commit = "a" * 40
        digest = "b" * 64
        archive = f"/var/tmp/codex-dispatcher-release-{commit}.tar"
        invalid_argv = (
            (),
            ("--apply", "--apply", "--commit", commit, "--archive", archive,
             "--sha256", digest),
            ("--plan", "--apply", "--commit", commit, "--archive", archive,
             "--sha256", digest),
            ("--status", "--apply", "--commit", commit),
            ("--rollback", "--commit", commit),
            ("--status", "--commit", commit, "--archive", archive),
            ("--plan", "--commit", commit, "--archive", archive,
             "--sha256", digest, "--config", f"/var/tmp/codex-dispatcher-config-{commit}.toml"),
            ("--plan", "--commit", commit, "--archive", archive,
             "--sha256", digest, "--wait-active-seconds", "3601"),
        )
        for argv in invalid_argv:
            with self.subTest(argv=argv):
                completed = subprocess.run(
                    [str(SCRIPT), *argv],
                    cwd=ROOT,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(2, completed.returncode)
                self.assertIn("usage:", completed.stderr)


if __name__ == "__main__":
    unittest.main()
