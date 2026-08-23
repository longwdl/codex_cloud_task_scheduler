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
        self.assertIn("Control service is not stopped", text)
        self.assertIn("codex-runner-release-validate-v1", text)
        self.assertIn('/usr/bin/chmod 0700 "$runner_validation"', text)
        self.assertIn("codex-runner", text)
        self.assertLess(
            text.index("/srv/codex-runner/current.next"),
            text.index("/opt/codex-dispatcher/current.next"),
        )
        self.assertIn("current.rollback", text)
        self.assertIn('"timers_started":false', text)
        self.assertIn('"requires_manual_sweep":true', text)
        self.assertNotIn("eval ", text)
        self.assertNotIn("release'/.'", text)
        self.assertNotIn("--force", text)

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
        completed = subprocess.run(
            [str(SCRIPT)],
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
