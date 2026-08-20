from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "deploy" / "runner"
SSHD_CONFIG = DEPLOYMENT / "codex-runner-sshd.conf"
DOCUMENTATION = DEPLOYMENT / "README.md"
WRAPPER = ROOT / "scripts" / "codex-runner-v1"


class RunnerDeploymentTests(unittest.TestCase):
    def test_sshd_match_is_one_fixed_noninteractive_protocol_account(self) -> None:
        configuration = SSHD_CONFIG.read_text(encoding="utf-8")

        self.assertIn("Match User codex-runner", configuration)
        self.assertIn(
            "AuthorizedKeysFile /etc/ssh/authorized_keys/codex-runner",
            configuration,
        )
        self.assertIn(
            "ForceCommand /srv/codex-runner/bin/codex-runner-v1",
            configuration,
        )
        for directive in (
            "AuthenticationMethods publickey",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "DisableForwarding yes",
            "AllowAgentForwarding no",
            "AllowTcpForwarding no",
            "X11Forwarding no",
            "PermitTunnel no",
            "PermitTTY no",
            "PermitUserRC no",
        ):
            self.assertIn(directive, configuration)
        self.assertNotIn("ForceCommand internal-sftp", configuration)
        self.assertNotIn("AuthorizedKeysFile %h/", configuration)
        self.assertEqual("Match all", configuration.rstrip().splitlines()[-1])

    def test_wrapper_uses_a_separate_minimal_home_and_fixed_entrypoint(self) -> None:
        wrapper = WRAPPER.read_text(encoding="utf-8")

        self.assertIn("/usr/bin/env -i", wrapper)
        self.assertIn("HOME=/var/lib/codex-runner/home", wrapper)
        self.assertIn("PYTHONPATH=/srv/codex-runner/current/src", wrapper)
        self.assertIn("-m codex_dispatcher.runner_main", wrapper)
        self.assertNotIn("SSH_ORIGINAL_COMMAND", wrapper)
        self.assertNotIn("$@", wrapper)
        self.assertNotIn("eval ", wrapper)

    def test_documentation_preserves_admin_access_and_exact_rollback(self) -> None:
        documentation = DOCUMENTATION.read_text(encoding="utf-8")

        self.assertIn("Do not edit or replace `~ecs-user/.ssh/authorized_keys`", documentation)
        self.assertIn(
            'restrict,command="/srv/codex-runner/bin/codex-runner-v1"',
            documentation,
        )
        self.assertIn("root-owned external `AuthorizedKeysFile`", documentation)
        self.assertIn("sudo /usr/sbin/sshd -t", documentation)
        self.assertIn("Reload rather than restart sshd", documentation)
        self.assertIn("wrapper discards the inherited environment", documentation)
        self.assertIn("Keep the Dispatcher timer disabled", documentation)
        self.assertIn("Container boundary still required", documentation)
        self.assertIn("do not move or\nrewrite their contents", documentation)


if __name__ == "__main__":
    unittest.main()
