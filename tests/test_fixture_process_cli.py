from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.fixture_faults import (
    FIXTURE_REPOSITORY,
    FixtureFaultRejected,
    validate_fixture_orphan_claim_recovery,
)
from codex_dispatcher.fixture_process_cli import (
    _HANDSHAKE_TIMEOUT_SECONDS,
    _child_environment,
    _handshake,
    _run_parent,
    _wait_for_exact_handshake,
)
from codex_dispatcher.ssh_preflight import SshPreflightPlan, SshPreflightStatus
from codex_dispatcher.ssh_recovery import SshRecoveryAction
from codex_dispatcher.ssh_runtime import SshPreflightInspection
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.trackers.base import TaskState, TrackerTask
from tests.test_scheduler import make_config


ISSUE = 11
TOKEN = "github_pat_fixture_process_test"


def _task(state: TaskState) -> TrackerTask:
    return TrackerTask(
        repository=FIXTURE_REPOSITORY,
        task_id=str(ISSUE),
        issue_number=ISSUE,
        title="Process kill fixture",
        body="fixture",
        state=state,
        labels=(f"agent:{state.value}", "exec:ssh-cli", "priority:p1"),
        created_at="2026-08-19T00:00:00Z",
        ready_approved_by="longwdl",
        issue_node_id="I_fixture_process_11",
        updated_at="2026-08-19T00:00:00Z",
    )


class _FakeProcess:
    def __init__(self, args) -> None:
        self.args = args
        self.killed = False
        self.terminated = False

    def poll(self):
        return None

    def kill(self) -> None:
        self.killed = True

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None) -> int:
        if self.killed:
            return -signal.SIGKILL
        return -signal.SIGTERM


class FixtureProcessCliTests(unittest.TestCase):
    def test_parent_handshake_deadline_exceeds_preclaim_git_timeout(self) -> None:
        self.assertGreater(_HANDSHAKE_TIMEOUT_SECONDS, 120)

    def test_orphan_recovery_validator_requires_exact_unpersisted_claim(self) -> None:
        plan = SshPreflightPlan(
            SshPreflightStatus.READY_RECOVERY,
            SshRecoveryAction.RECOVER_ORPHAN_CLAIM,
            task=_task(TaskState.DISPATCHING),
        )

        validate_fixture_orphan_claim_recovery(plan, issue_number=ISSUE)

        with self.assertRaisesRegex(FixtureFaultRejected, "orphan claim"):
            validate_fixture_orphan_claim_recovery(
                replace(plan, recovery_action=SshRecoveryAction.IDLE),
                issue_number=ISSUE,
            )

    def test_handshake_is_exact_and_bounded(self) -> None:
        expected = _handshake(_task(TaskState.DISPATCHING))
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, expected)
            _wait_for_exact_handshake(read_fd, expected, timeout_seconds=1)
        finally:
            os.close(read_fd)
            os.close(write_fd)

        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, expected + b"x")
            with self.assertRaisesRegex(FixtureFaultRejected, "invalid handshake"):
                _wait_for_exact_handshake(read_fd, expected, timeout_seconds=1)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_child_environment_is_minimal_and_does_not_copy_host_secrets(self) -> None:
        with patch.dict(
            "os.environ",
            {"UNRELATED_SECRET": "must-not-copy", "HOME": "/unexpected"},
            clear=True,
        ):
            environment = _child_environment(TOKEN)

        self.assertEqual(TOKEN, environment["GITHUB_TOKEN"])
        self.assertNotIn("UNRELATED_SECRET", environment)
        self.assertNotIn("HOME", environment)
        self.assertEqual(FIXTURE_REPOSITORY, environment["CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS"])

    def test_parent_kills_only_verified_child_and_requires_orphan_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / "state.db"
            with StateStore(database) as store:
                store.migrate()
            config = make_config(global_max_active=1, repository_max_active=1)
            config = replace(
                config,
                scheduler=replace(config.scheduler, database_path=database),
                repositories=(
                    replace(
                        config.repositories[0],
                        slug=FIXTURE_REPOSITORY,
                        allowed_paths=("README.md",),
                        denied_paths=(),
                        maintainers=("longwdl",),
                        required_checks=("fixture",),
                    ),
                ),
            )
            ready = SshPreflightInspection(
                SshPreflightPlan(
                    SshPreflightStatus.READY_CANDIDATE,
                    SshRecoveryAction.IDLE,
                    task=_task(TaskState.READY),
                ),
                (),
                True,
            )
            recovery = SshPreflightInspection(
                SshPreflightPlan(
                    SshPreflightStatus.READY_RECOVERY,
                    SshRecoveryAction.RECOVER_ORPHAN_CLAIM,
                    task=_task(TaskState.DISPATCHING),
                ),
                (),
                True,
            )
            observed: dict[str, object] = {}

            def start_process(args, **kwargs):
                observed["args"] = args
                observed["kwargs"] = kwargs
                process = _FakeProcess(args)
                observed["process"] = process
                return process

            with (
                patch.dict(
                    "os.environ",
                    {
                        "CODEX_DISPATCHER_ENABLE_SSH_WRITES": "1",
                        "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS": FIXTURE_REPOSITORY,
                        "GITHUB_TOKEN": TOKEN,
                    },
                    clear=True,
                ),
                patch(
                    "codex_dispatcher.fixture_process_cli.load_protected_ssh_config",
                    return_value=config,
                ),
                patch("codex_dispatcher.fixture_process_cli.validate_runtime_state_path"),
                patch(
                    "codex_dispatcher.fixture_process_cli.run_ssh_preflight",
                    return_value=ready,
                ),
                patch(
                    "codex_dispatcher.fixture_process_cli._await_orphan_recovery",
                    return_value=recovery,
                ),
                patch(
                    "codex_dispatcher.fixture_process_cli._create_backup",
                    return_value=Path(root) / "state.pre-kill.db",
                ),
                patch(
                    "codex_dispatcher.fixture_process_cli._wait_for_exact_handshake"
                ) as wait_handshake,
                patch(
                    "codex_dispatcher.fixture_process_cli.subprocess.Popen",
                    side_effect=start_process,
                ),
            ):
                code, payload = _run_parent(
                    Path(root) / "dispatcher.toml",
                    issue_number=ISSUE,
                )

        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["terminated"])
        self.assertEqual("SIGKILL", payload["termination_signal"])
        self.assertEqual("recover_orphan_claim", payload["recovery_action"])
        self.assertFalse(payload["local_work_item_persisted"])
        process = observed["process"]
        assert isinstance(process, _FakeProcess)
        self.assertTrue(process.killed)
        self.assertFalse(process.terminated)
        wait_handshake.assert_called_once()
        kwargs = observed["kwargs"]
        assert isinstance(kwargs, dict)
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(TOKEN, kwargs["env"]["GITHUB_TOKEN"])
        self.assertEqual(subprocess.DEVNULL, kwargs["stdin"])

    def test_parent_guards_fail_before_loading_live_config(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "codex_dispatcher.fixture_process_cli.load_protected_ssh_config"
            ) as load,
        ):
            code, payload = _run_parent(
                Path("/protected/dispatcher.toml"),
                issue_number=ISSUE,
            )

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"])
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
