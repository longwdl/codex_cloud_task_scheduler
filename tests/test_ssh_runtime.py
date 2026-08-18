from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codex_dispatcher.config import SshRuntimeConfig
from codex_dispatcher.contract import ContractCheck
from codex_dispatcher.control_sweep import (
    ControlSweepResult,
    ControlSweepStatus,
    SshControlSweep,
)
from codex_dispatcher.ssh_runtime import (
    SshRuntimeError,
    build_ssh_control_sweep,
    load_protected_ssh_config,
    run_ssh_control_sweep,
    validate_runtime_state_path,
)
from codex_dispatcher.state_store import StateStore
from tests.test_config import SSH_RUNTIME, VALID
from tests.test_scheduler import make_config


TOKEN = "github_pat_runtime_fixture"


def _protected_file(path: Path, *, executable: bool = False) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n" if executable else "fixture\n", encoding="utf-8")
    path.chmod(0o700 if executable else 0o600)
    return path


class SshRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir(mode=0o700)
        self.git_path = _protected_file(self.bin_dir / "git", executable=True)
        self.gh_path = _protected_file(self.bin_dir / "gh", executable=True)
        self.ssh_path = _protected_file(self.bin_dir / "ssh", executable=True)
        self.known_hosts = _protected_file(self.root / "known_hosts")
        self.identity = _protected_file(self.root / "identity")
        self.lock_dir = self.root / "run"
        self.lock_dir.mkdir(mode=0o700)
        runtime = SshRuntimeConfig(
            git_path=self.git_path,
            gh_path=self.gh_path,
            ssh_path=self.ssh_path,
            host="runner.internal",
            user="codex",
            port=22,
            known_hosts_path=self.known_hosts,
            identity_file=self.identity,
            lock_path=self.lock_dir / "dispatcher.lock",
            mirror_root=self.root / "mirrors",
            source_temporary_root=self.root / "source-temporary",
            quarantine_root=self.root / "quarantine",
            runner_root="/srv/codex-runner/work-items",
            connect_timeout_seconds=10,
            operation_timeout_seconds=3900,
        )
        base = make_config(global_max_active=1, repository_max_active=1)
        self.config = replace(
            base,
            tools=replace(base.tools, ssh_version="9.6"),
            scheduler=replace(
                base.scheduler,
                database_path=self.root / "state.db",
                workspace_root=self.root / "workspace",
            ),
            ssh_runtime=runtime,
        )
        self.store = StateStore(self.config.scheduler.database_path)
        self.store.migrate()

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_build_assembles_ports_without_network_or_runner_invocation(self) -> None:
        sweep = build_ssh_control_sweep(
            config=self.config,
            store=self.store,
            github_token=TOKEN,
        )

        self.assertIsInstance(sweep, SshControlSweep)
        self.assertFalse((self.root / "mirrors").exists())
        self.assertFalse((self.root / "quarantine").exists())
        self.assertFalse((self.lock_dir / "dispatcher.lock").exists())

    def test_live_config_and_sqlite_paths_must_be_owned_and_protected(self) -> None:
        config_path = self.root / "dispatcher.toml"
        configured = VALID.replace(
            'codex_version = "0.1.0"',
            'codex_version = "0.1.0"\nssh_version = "9.6"',
        )
        config_path.write_text(configured + SSH_RUNTIME, encoding="utf-8")
        config_path.chmod(0o600)

        loaded = load_protected_ssh_config(config_path)
        self.assertIsNotNone(loaded.ssh_runtime)
        validate_runtime_state_path(self.config.scheduler.database_path)

        config_path.chmod(0o666)
        with self.assertRaisesRegex(SshRuntimeError, "config"):
            load_protected_ssh_config(config_path)
        config_path.chmod(0o600)
        self.root.chmod(0o777)
        with self.assertRaisesRegex(SshRuntimeError, "config directory"):
            load_protected_ssh_config(config_path)
        self.root.chmod(0o700)
        self.config.scheduler.database_path.chmod(0o666)
        with self.assertRaisesRegex(SshRuntimeError, "database file"):
            validate_runtime_state_path(self.config.scheduler.database_path)

    def test_runtime_rejects_missing_settings_concurrency_and_weak_tools(self) -> None:
        for config, message in (
            (replace(self.config, ssh_runtime=None), "configuration is required"),
            (
                replace(
                    self.config,
                    scheduler=replace(
                        self.config.scheduler,
                        global_max_active=2,
                    ),
                ),
                "max_active",
            ),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(SshRuntimeError, message):
                    build_ssh_control_sweep(
                        config=config,
                        store=self.store,
                        github_token=TOKEN,
                    )

        self.git_path.chmod(0o777)
        with self.assertRaisesRegex(SshRuntimeError, "git_path"):
            build_ssh_control_sweep(
                config=self.config,
                store=self.store,
                github_token=TOKEN,
            )

    def test_tool_contract_failure_never_enters_sweep(self) -> None:
        sweep = SimpleNamespace(run_once=lambda: self.fail("sweep must not run"))
        with (
            patch(
                "codex_dispatcher.ssh_runtime._assemble_ssh_control_sweep",
                return_value=sweep,
            ),
            patch(
                "codex_dispatcher.ssh_runtime.run_control_host_contract_checks",
                return_value=(ContractCheck("git", False, "version drift"),),
            ),
        ):
            with self.assertRaisesRegex(SshRuntimeError, "git"):
                run_ssh_control_sweep(
                    config=self.config,
                    store=self.store,
                    github_token=TOKEN,
                )

    def test_verified_contract_executes_exactly_one_sweep(self) -> None:
        result = ControlSweepResult(ControlSweepStatus.IDLE)
        sweep = SimpleNamespace(run_once=Mock(return_value=result))
        with (
            patch(
                "codex_dispatcher.ssh_runtime._assemble_ssh_control_sweep",
                return_value=sweep,
            ),
            patch(
                "codex_dispatcher.ssh_runtime.run_control_host_contract_checks",
                return_value=(
                    ContractCheck("git", True, "2.55.0"),
                    ContractCheck("gh", True, "2.97.0"),
                ),
            ),
        ):
            observed = run_ssh_control_sweep(
                config=self.config,
                store=self.store,
                github_token=TOKEN,
            )

        self.assertEqual(result, observed)
        sweep.run_once.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
