from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "deploy/systemd-user"
SERVICE_PATH = DEPLOYMENT / "codex-dispatcher.service"
TIMER_PATH = DEPLOYMENT / "codex-dispatcher.timer"
BACKUP_SERVICE_PATH = DEPLOYMENT / "codex-dispatcher-backup.service"
BACKUP_TIMER_PATH = DEPLOYMENT / "codex-dispatcher-backup.timer"
ENVIRONMENT_EXAMPLE_PATH = DEPLOYMENT / "dispatcher.env.example"
README_PATH = DEPLOYMENT / "README.md"
WRAPPER_PATH = ROOT / "scripts/codex-dispatcher-user-v1"
BACKUP_WRAPPER_PATH = ROOT / "scripts/codex-dispatcher-user-backup-v1"


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


class ControlHostUserSystemdTests(unittest.TestCase):
    def test_dispatcher_is_a_fixed_user_sweep_with_protected_paths(self) -> None:
        sections = _unit_sections(SERVICE_PATH)
        self.assertEqual(["oneshot"], _values(sections, "Service", "Type"))
        self.assertEqual([], _values(sections, "Service", "User"))
        self.assertEqual([], _values(sections, "Service", "Group"))
        self.assertEqual(["HOME=%h"], _values(sections, "Service", "Environment"))
        self.assertEqual(
            ["%h/.config/codex-dispatcher/dispatcher.env"],
            _values(sections, "Service", "EnvironmentFile"),
        )
        self.assertEqual(
            ["%h/.local/opt/codex-dispatcher/current/scripts/codex-dispatcher-user-v1"],
            _values(sections, "Service", "ExecStart"),
        )
        required = {
            ("UMask", "0077"),
            ("RuntimeDirectory", "codex-dispatcher"),
            ("RuntimeDirectoryMode", "0700"),
            ("StateDirectory", "codex-dispatcher"),
            ("StateDirectoryMode", "0700"),
            ("ReadWritePaths", "%S/codex-dispatcher %t/codex-dispatcher"),
            ("ProtectHome", "read-only"),
            ("ProtectSystem", "strict"),
            ("NoNewPrivileges", "yes"),
            ("KillMode", "control-group"),
            ("TimeoutStartSec", "infinity"),
            ("TimeoutStopSec", "30s"),
            ("Restart", "no"),
        }
        self.assertTrue(required.issubset(set(sections["Service"])))
        self.assertEqual([""], _values(sections, "Service", "CapabilityBoundingSet"))
        self.assertEqual([""], _values(sections, "Service", "AmbientCapabilities"))
        self.assertEqual([], _values(sections, "Service", "RemoveIPC"))
        service_text = SERVICE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("fixture", service_text.lower())
        self.assertNotIn("ssh-preflight", service_text)

    def test_dispatcher_timer_uses_user_manager_start_and_no_backlog(self) -> None:
        sections = _unit_sections(TIMER_PATH)
        self.assertEqual(["2min"], _values(sections, "Timer", "OnStartupSec"))
        self.assertEqual([], _values(sections, "Timer", "OnBootSec"))
        self.assertEqual(["2min"], _values(sections, "Timer", "OnUnitInactiveSec"))
        self.assertEqual([], _values(sections, "Timer", "OnUnitActiveSec"))
        self.assertEqual(["false"], _values(sections, "Timer", "Persistent"))
        self.assertEqual(["timers.target"], _values(sections, "Install", "WantedBy"))

    def test_dispatcher_wrapper_has_no_argument_or_shell_injection_surface(self) -> None:
        wrapper = WRAPPER_PATH.read_text(encoding="utf-8")
        self.assertNotEqual(0, WRAPPER_PATH.stat().st_mode & 0o111)
        self.assertIn('if [ "$#" -ne 0 ]; then', wrapper)
        self.assertIn('case "${HOME-}" in', wrapper)
        self.assertIn(
            "exec /home/linuxbrew/.linuxbrew/bin/python3 -P -s -m codex_dispatcher",
            wrapper,
        )
        self.assertIn("ssh-run-once", wrapper)
        self.assertIn("--apply", wrapper)
        self.assertIn(
            '--config "$HOME/.config/codex-dispatcher/config.toml"', wrapper
        )
        self.assertNotIn("fixture", wrapper.lower())
        self.assertNotIn("eval ", wrapper)
        self.assertNotIn('"$@"', wrapper)

    def test_backup_is_credential_free_and_network_isolated(self) -> None:
        sections = _unit_sections(BACKUP_SERVICE_PATH)
        self.assertEqual(["oneshot"], _values(sections, "Service", "Type"))
        self.assertEqual([], _values(sections, "Service", "EnvironmentFile"))
        self.assertEqual(["HOME=%h"], _values(sections, "Service", "Environment"))
        self.assertEqual(["yes"], _values(sections, "Service", "PrivateNetwork"))
        self.assertEqual([], _values(sections, "Service", "RemoveIPC"))
        self.assertEqual(
            ["%S/codex-dispatcher"],
            _values(sections, "Service", "ReadWritePaths"),
        )
        wrapper = BACKUP_WRAPPER_PATH.read_text(encoding="utf-8")
        self.assertNotEqual(0, BACKUP_WRAPPER_PATH.stat().st_mode & 0o111)
        self.assertIn('if [ "$#" -ne 0 ]; then', wrapper)
        self.assertIn("state-backup", wrapper)
        self.assertNotIn("ssh-run-once", wrapper)
        self.assertNotIn("--apply", wrapper)

    def test_backup_timer_is_daily_persistent_and_randomized(self) -> None:
        sections = _unit_sections(BACKUP_TIMER_PATH)
        self.assertEqual(["daily"], _values(sections, "Timer", "OnCalendar"))
        self.assertEqual(["15min"], _values(sections, "Timer", "RandomizedDelaySec"))
        self.assertEqual(["true"], _values(sections, "Timer", "Persistent"))

    def test_secret_example_and_docs_preserve_rootless_boundary(self) -> None:
        environment = ENVIRONMENT_EXAMPLE_PATH.read_text(encoding="utf-8")
        self.assertIn("CODEX_DISPATCHER_ENABLE_SSH_WRITES=1", environment)
        self.assertIn("GITHUB_TOKEN=replace-with-repository-scoped-token", environment)
        self.assertNotIn("github_pat_", environment)
        self.assertNotIn("ghp_", environment)
        self.assertNotIn("xoxb-", environment)
        self.assertNotIn("export ", environment)

        documentation = README_PATH.read_text(encoding="utf-8")
        self.assertIn("sudo loginctl enable-linger ecs-user", documentation)
        self.assertIn("Linger=yes", documentation)
        self.assertIn("Do not use this variant for production", documentation)
        self.assertIn("separate approval", documentation)


if __name__ == "__main__":
    unittest.main()
