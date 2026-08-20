from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "deploy/systemd/codex-dispatcher.service"
TIMER_PATH = ROOT / "deploy/systemd/codex-dispatcher.timer"
BACKUP_SERVICE_PATH = ROOT / "deploy/systemd/codex-dispatcher-backup.service"
BACKUP_TIMER_PATH = ROOT / "deploy/systemd/codex-dispatcher-backup.timer"
ENVIRONMENT_EXAMPLE_PATH = ROOT / "deploy/systemd/dispatcher.env.example"
WRAPPER_PATH = ROOT / "scripts/codex-dispatcher-v1"
BACKUP_WRAPPER_PATH = ROOT / "scripts/codex-dispatcher-backup-v1"


def _unit_sections(path: Path) -> dict[str, list[tuple[str, str]]]:
    sections: dict[str, list[tuple[str, str]]] = {}
    current: list[tuple[str, str]] | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], [])
            continue
        if current is None or "=" not in line:
            raise AssertionError(f"invalid unit line in {path.name}: {raw_line!r}")
        key, value = line.split("=", 1)
        current.append((key, value))
    return sections


def _values(
    sections: dict[str, list[tuple[str, str]]], section: str, key: str
) -> list[str]:
    return [value for candidate, value in sections[section] if candidate == key]


class ControlHostSystemdTests(unittest.TestCase):
    def test_service_is_one_fixed_nonconcurrent_write_enabled_sweep(self) -> None:
        sections = _unit_sections(SERVICE_PATH)
        self.assertEqual(["oneshot"], _values(sections, "Service", "Type"))
        self.assertEqual(
            ["codex-dispatcher"], _values(sections, "Service", "User")
        )
        self.assertEqual(
            ["codex-dispatcher"], _values(sections, "Service", "Group")
        )
        self.assertEqual(
            ["/etc/codex-dispatcher/dispatcher.env"],
            _values(sections, "Service", "EnvironmentFile"),
        )
        self.assertEqual(
            ["/opt/codex-dispatcher/current/scripts/codex-dispatcher-v1"],
            _values(sections, "Service", "ExecStart"),
        )
        self.assertEqual(["no"], _values(sections, "Service", "Restart"))
        self.assertEqual(
            ["infinity"], _values(sections, "Service", "TimeoutStartSec")
        )
        self.assertEqual([], _values(sections, "Service", "Environment"))
        service_text = SERVICE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("fixture", service_text.lower())
        self.assertNotIn("ssh-preflight", service_text)

    def test_service_has_protected_state_and_no_privilege_escalation(self) -> None:
        sections = _unit_sections(SERVICE_PATH)
        required = {
            ("UMask", "0077"),
            ("RuntimeDirectory", "codex-dispatcher"),
            ("RuntimeDirectoryMode", "0700"),
            ("StateDirectory", "codex-dispatcher"),
            ("StateDirectoryMode", "0700"),
            ("NoNewPrivileges", "yes"),
            ("PrivateDevices", "yes"),
            ("PrivateTmp", "yes"),
            ("ProtectHome", "yes"),
            ("ProtectSystem", "strict"),
            ("RestrictSUIDSGID", "yes"),
            ("KillMode", "control-group"),
            ("TimeoutStopSec", "30s"),
            ("SendSIGKILL", "yes"),
        }
        self.assertTrue(required.issubset(set(sections["Service"])))
        self.assertEqual([""], _values(sections, "Service", "CapabilityBoundingSet"))
        self.assertEqual([""], _values(sections, "Service", "AmbientCapabilities"))
        self.assertEqual(
            ["/var/lib/codex-dispatcher /run/codex-dispatcher"],
            _values(sections, "Service", "ReadWritePaths"),
        )

    def test_timer_waits_until_the_previous_sweep_is_inactive(self) -> None:
        sections = _unit_sections(TIMER_PATH)
        self.assertEqual(
            ["codex-dispatcher.service"], _values(sections, "Timer", "Unit")
        )
        self.assertEqual(["2min"], _values(sections, "Timer", "OnBootSec"))
        self.assertEqual(
            ["2min"], _values(sections, "Timer", "OnUnitInactiveSec")
        )
        self.assertEqual([], _values(sections, "Timer", "OnUnitActiveSec"))
        self.assertEqual(["false"], _values(sections, "Timer", "Persistent"))
        self.assertEqual(
            ["timers.target"], _values(sections, "Install", "WantedBy")
        )

    def test_wrapper_is_argument_free_and_executes_only_the_live_cli(self) -> None:
        wrapper = WRAPPER_PATH.read_text(encoding="utf-8")
        self.assertTrue(wrapper.startswith("#!/bin/sh\n"))
        self.assertNotEqual(0, WRAPPER_PATH.stat().st_mode & 0o111)
        self.assertIn('if [ "$#" -ne 0 ]; then', wrapper)
        self.assertIn(
            "exec /opt/codex-python/current/bin/python3 -P -s -m codex_dispatcher",
            wrapper,
        )
        self.assertIn("ssh-run-once", wrapper)
        self.assertIn("--apply", wrapper)
        self.assertIn("--config /etc/codex-dispatcher/config.toml", wrapper)
        self.assertNotIn("fixture", wrapper.lower())
        self.assertNotIn("eval ", wrapper)
        self.assertNotIn('"$@"', wrapper)

    def test_environment_example_contains_no_credential_value(self) -> None:
        environment = ENVIRONMENT_EXAMPLE_PATH.read_text(encoding="utf-8")
        self.assertIn("CODEX_DISPATCHER_ENABLE_SSH_WRITES=1", environment)
        self.assertIn("GITHUB_TOKEN=replace-with-repository-scoped-token", environment)
        self.assertNotIn("github_pat_", environment)
        self.assertNotIn("ghp_", environment)
        self.assertNotIn("xoxb-", environment)
        self.assertNotIn("export ", environment)

    def test_backup_service_is_credential_free_and_network_isolated(self) -> None:
        sections = _unit_sections(BACKUP_SERVICE_PATH)
        self.assertEqual(["oneshot"], _values(sections, "Service", "Type"))
        self.assertEqual(
            ["/opt/codex-dispatcher/current/scripts/codex-dispatcher-backup-v1"],
            _values(sections, "Service", "ExecStart"),
        )
        self.assertEqual([], _values(sections, "Service", "EnvironmentFile"))
        self.assertEqual(["yes"], _values(sections, "Service", "PrivateNetwork"))
        self.assertEqual(
            ["/var/lib/codex-dispatcher"],
            _values(sections, "Service", "ReadWritePaths"),
        )
        self.assertEqual([""], _values(sections, "Service", "CapabilityBoundingSet"))

        wrapper = BACKUP_WRAPPER_PATH.read_text(encoding="utf-8")
        self.assertNotEqual(0, BACKUP_WRAPPER_PATH.stat().st_mode & 0o111)
        self.assertIn('if [ "$#" -ne 0 ]; then', wrapper)
        self.assertIn(
            "exec /opt/codex-python/current/bin/python3 -P -s -m codex_dispatcher",
            wrapper,
        )
        self.assertIn("state-backup", wrapper)
        self.assertNotIn("ssh-run-once", wrapper)
        self.assertNotIn("--apply", wrapper)

    def test_backup_timer_is_daily_persistent_and_boundedly_randomized(self) -> None:
        sections = _unit_sections(BACKUP_TIMER_PATH)
        self.assertEqual(
            ["codex-dispatcher-backup.service"],
            _values(sections, "Timer", "Unit"),
        )
        self.assertEqual(["daily"], _values(sections, "Timer", "OnCalendar"))
        self.assertEqual(
            ["15min"], _values(sections, "Timer", "RandomizedDelaySec")
        )
        self.assertEqual(["true"], _values(sections, "Timer", "Persistent"))


if __name__ == "__main__":
    unittest.main()
