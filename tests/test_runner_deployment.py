from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "deploy" / "runner"
SSHD_CONFIG = DEPLOYMENT / "codex-runner-sshd.conf"
DOCUMENTATION = DEPLOYMENT / "README.md"
DOCKER_DOCUMENTATION = DEPLOYMENT / "DOCKER.md"
WRAPPER = ROOT / "scripts" / "codex-runner-v1"
CAPACITY_WRAPPER = ROOT / "scripts" / "codex-runner-capacity-v1"
MAINTENANCE_WRAPPER = ROOT / "scripts" / "codex-runner-maintenance-v1"
CAPACITY_SERVICE = DEPLOYMENT / "codex-runner-capacity.service"
CAPACITY_TIMER = DEPLOYMENT / "codex-runner-capacity.timer"
RECLAMATION_SERVICE = DEPLOYMENT / "codex-runner-reclamation-plan.service"
RECLAMATION_TIMER = DEPLOYMENT / "codex-runner-reclamation-plan.timer"


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
        self.assertIn(
            "/etc/ssh/authorized_keys/codex-runner     root:codex-runner     0640",
            documentation,
        )
        self.assertIn("sudo /usr/sbin/sshd -t", documentation)
        self.assertIn("Reload rather than restart sshd", documentation)
        self.assertIn("wrapper discards the inherited environment", documentation)
        self.assertIn("Keep the Dispatcher timer disabled", documentation)
        self.assertIn("Container boundary status", documentation)
        self.assertIn("successful container RESUME Turn", documentation)
        self.assertIn("does not authorize\nhigher-value repositories", documentation)
        self.assertIn("do not move or\nrewrite their contents", documentation)

    def test_documentation_uses_exact_per_turn_auth_readiness_gate(self) -> None:
        documentation = DOCKER_DOCUMENTATION.read_text(encoding="utf-8")

        self.assertIn("immediately before every START and RESUME", documentation)
        self.assertIn(
            "returns\n`codex_auth_invalid` before `codex exec`",
            documentation,
        )
        self.assertIn(
            "does not claim that `login status`\nperforms a model request",
            documentation,
        )
        self.assertNotIn("| Natural token refresh |", documentation)
        self.assertNotIn("token refresh remains an admission gate", documentation)

    def test_capacity_monitor_is_fixed_read_only_and_hardened(self) -> None:
        wrapper = CAPACITY_WRAPPER.read_text(encoding="utf-8")
        service = CAPACITY_SERVICE.read_text(encoding="utf-8")
        timer = CAPACITY_TIMER.read_text(encoding="utf-8")

        self.assertNotEqual(0, CAPACITY_WRAPPER.stat().st_mode & 0o111)
        self.assertIn('if [ "$#" -ne 0 ]; then', wrapper)
        self.assertIn("runner-capacity", wrapper)
        self.assertIn("--require-provision-admissible", wrapper)
        self.assertNotIn("--apply", wrapper)
        self.assertNotIn('"$@"', wrapper)
        self.assertIn("User=codex-runner", service)
        self.assertIn("PrivateNetwork=yes", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("CapabilityBoundingSet=\n", service)
        self.assertNotIn("ReadWritePaths=", service)
        self.assertIn("OnBootSec=5min", timer)
        self.assertIn("OnUnitInactiveSec=15min", timer)
        self.assertIn("Persistent=false", timer)

    def test_maintenance_wrapper_accepts_only_exact_admin_command_shapes(self) -> None:
        wrapper = MAINTENANCE_WRAPPER.read_text(encoding="utf-8")

        self.assertNotEqual(0, MAINTENANCE_WRAPPER.stat().st_mode & 0o111)
        self.assertIn("root is required", wrapper)
        self.assertIn("2:reclamation-plan", wrapper)
        self.assertIn("1:reclamation-auto-plan", wrapper)
        self.assertIn("3:recovery-snapshot", wrapper)
        self.assertIn("3:reclamation-recheck", wrapper)
        self.assertIn("4:reclamation-apply", wrapper)
        self.assertIn("codex_dispatcher.runner_maintenance", wrapper)
        self.assertNotIn("docker image prune", wrapper)
        self.assertNotIn("docker system prune", wrapper)

    def test_reclamation_timer_can_only_write_exact_plans_and_status(self) -> None:
        service = RECLAMATION_SERVICE.read_text(encoding="utf-8")
        timer = RECLAMATION_TIMER.read_text(encoding="utf-8")

        self.assertIn("User=root", service)
        self.assertIn("Group=codex-runner", service)
        self.assertIn("reclamation-auto-plan", service)
        self.assertIn("PrivateNetwork=yes", service)
        self.assertIn("DevicePolicy=closed", service)
        self.assertNotIn("PrivateDevices=", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("ProtectHome=tmpfs", service)
        self.assertNotIn("ProtectHome=read-only", service)
        self.assertIn(
            "BindReadOnlyPaths=/run/user/1002/docker.sock", service
        )
        self.assertIn(
            "ReadWritePaths=/srv/codex-runner/reclamation-plans "
            "/srv/codex-runner/reclamation-status",
            service,
        )
        self.assertIn(
            "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER CAP_SETGID CAP_SETUID",
            service,
        )
        self.assertIn("AmbientCapabilities=\n", service)
        self.assertNotIn("RestrictSUIDSGID=", service)
        self.assertNotIn("RestrictAddressFamilies=", service)
        self.assertNotIn("RestrictNamespaces=", service)
        self.assertNotIn("SystemCallFilter=", service)
        self.assertNotIn("reclamation-apply", service)
        self.assertNotIn("docker image prune", service)
        self.assertIn("OnUnitInactiveSec=6h", timer)
        self.assertIn("Persistent=false", timer)


if __name__ == "__main__":
    unittest.main()
