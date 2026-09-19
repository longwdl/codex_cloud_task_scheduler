from __future__ import annotations

import concurrent.futures
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_dispatcher.command_runner import CommandResult, run_command
from codex_dispatcher import fixture_runner_cli as cli
from codex_dispatcher.executors.codex_docker import (
    DOCKER_NETWORK, DOCKER_LABEL_POLICY_DIGEST, DOCKER_LABEL_SESSION_GENERATION,
    DOCKER_LABEL_SESSION_GENERATION_ID, DOCKER_LABEL_TURN, DOCKER_LABEL_WORK_ITEM,
)
from codex_dispatcher.runner_protocol import NEXT_PROTOCOL_VERSION, RunnerOperation, RunnerRequest
from codex_dispatcher.runner_transport import RunnerAbsenceReply

REPOSITORY = "longwdl/codex-dispatcher-fixture"
WORK_ITEM = "wi_" + "a" * 24
TURN = "turn_" + "b" * 32
GENERATION = "sg_" + "c" * 32
POLICY = "e" * 64
SESSION = "123e4567-e89b-12d3-a456-426614174000"
CONTAINER = "a" * 64
IMAGE = "registry.example/codex@sha256:" + "f" * 64


class FixtureRunnerCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "work-items"
        self.operator = self.base / "operator"
        self.operator.mkdir(mode=0o700)
        self.workspace = self.root / REPOSITORY.replace("/", "__") / "issue-7"
        self.repo = self.workspace / "repo"
        self.state = self.workspace / "runner-state"
        for directory in (self.root, self.root / ".registry", self.workspace.parent,
                          self.workspace, self.repo, self.state):
            directory.mkdir(mode=0o700, exist_ok=True)
        self.git = str(Path(shutil.which("git")).resolve())
        self.git_run("init", "-b", "codex/issue-7-fixture")
        (self.repo / "README.md").write_text("fixture\n")
        self.git_run("add", "README.md")
        self.git_run("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "-m", "Fixture")
        self.head = self.git_run("rev-parse", "HEAD").strip()
        self.metadata = {"version": 1, "work_item_id": WORK_ITEM, "repository": REPOSITORY,
                         "issue_number": 7, "task_branch": "codex/issue-7-fixture",
                         "base_sha": self.head, "source_bundle_sha256": "f" * 64}
        self.write_json(self.root / ".registry" / f"{WORK_ITEM}.json", self.metadata)
        self.write_json(self.state / "workspace.json", self.metadata)
        self.generation = self.state / "generations" / GENERATION
        self.generation.mkdir(mode=0o700, parents=True)
        self.write_json(self.generation / "generation.json", {
            "version": 1, "work_item_id": WORK_ITEM, "session_generation_id": GENERATION,
            "session_generation": 1, "agent_policy_digest": POLICY,
            "image": IMAGE, "codex_sha256": "a" * 64, "code_mode_host_sha256": "b" * 64,
        })
        session = {"version": 1, "work_item_id": WORK_ITEM, "session_id": SESSION,
                   "image": IMAGE, "codex_sha256": "a" * 64}
        self.write_json(self.generation / "codex-session.json", session)
        self.write_json(self.generation / "codex-session-tools.json", {**session, "code_mode_host_sha256": "b" * 64})
        (self.state / "turns").mkdir(mode=0o700)
        request = RunnerRequest(RunnerOperation.START, WORK_ITEM, turn_id=TURN,
                                prompt_sha256="c" * 64, input_head_sha=self.head,
                                session_generation_id=GENERATION, session_generation=1,
                                agent_policy_digest=POLICY, version=NEXT_PROTOCOL_VERSION)
        self.turn_path = self.state / "turns" / f"{TURN}.json"
        self.write_json(self.turn_path, {"version": 1, "request": request.to_mapping(),
                                        "state": "executing", "reply": None})
        lock = self.root / "active.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        self.config = SimpleNamespace(
            work_items_root=self.root, active_lock_path=lock, git_path=Path(self.git),
            git_timeout_seconds=5, work_item_disk=None,
            docker_runtime=SimpleNamespace(docker_path=Path("/protected/docker"),
                docker_host="unix:///run/docker.sock", cli_config_directory=self.base,
                image=IMAGE, codex_sha256="a" * 64, code_mode_host_sha256="b" * 64),
            policy_bundle=SimpleNamespace(policy_digest=POLICY),
        )
        self.identity = {"repository": REPOSITORY, "issue": 7, "work_item_id": WORK_ITEM,
                         "turn_id": TURN, "session_generation_id": GENERATION, "generation": 1,
                         "policy_digest": POLICY, "expected_head": self.head}
        self.row = {"Id": CONTAINER, "Name": "/codex-" + TURN,
                    "Config": {"Image": IMAGE, "Labels": {
                        DOCKER_LABEL_WORK_ITEM: WORK_ITEM, DOCKER_LABEL_TURN: TURN,
                        DOCKER_LABEL_SESSION_GENERATION_ID: GENERATION,
                        DOCKER_LABEL_SESSION_GENERATION: "1", DOCKER_LABEL_POLICY_DIGEST: POLICY,
                        "org.opencontainers.image.title": "fixture"}},
                    "HostConfig": {"NetworkMode": DOCKER_NETWORK},
                    "NetworkSettings": {"Networks": {DOCKER_NETWORK: {}}},
                    "State": {"Running": True, "Paused": False, "Restarting": False,
                              "Dead": False, "Pid": 42}}
        self.present = True
        self.stop_calls = 0
        self.stop_timeout = False
        self.multiple = False
        self.command_lock = threading.Lock()
        for patcher in (
            patch.object(cli, "OPERATOR_ROOT", self.operator),
            patch.object(cli, "load_runner_configuration", return_value=self.config),
            patch.object(cli, "_require_runner_user"),
            patch.object(cli, "run_command", side_effect=self.command),
            patch.dict(os.environ, {cli.FAULT_ENV: REPOSITORY}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def git_run(self, *arguments: str) -> str:
        env = {"PATH": os.defpath, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run([self.git, "-C", str(self.repo), *arguments], env=env,
                              capture_output=True, text=True, check=True).stdout

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def command(self, argv, **kwargs):
        if argv[0] != "/protected/docker":
            return run_command(argv, **kwargs)
        with self.command_lock:
            if "ls" in argv:
                return CommandResult(0, ((CONTAINER + "\n") * (2 if self.multiple else 1)) if self.present else "", "")
            if "inspect" in argv:
                return CommandResult(0, json.dumps(self.row), "")
            if "stop" in argv:
                self.stop_calls += 1
                intents = list(self.operator.rglob("*.intent.json"))
                self.assertEqual(1, len(intents))
                self.assertEqual(CONTAINER, json.loads(intents[0].read_text())["container_id"])
                self.assertEqual(CONTAINER, argv[-1])
                self.present = False  # Runtime uses docker run --rm.
                return CommandResult(None if self.stop_timeout else 0, "", "", timed_out=self.stop_timeout)
        self.fail("unexpected Docker operation")

    def invoke(self, command, **overrides):
        identity = {**self.identity, **overrides}
        if command == "diagnose":
            identity = {key: identity[key] for key in ("repository", "issue", "work_item_id", "expected_head")}
        argv = [command]
        for key, value in identity.items():
            argv.extend(["--" + key.replace("_", "-"), str(value)])
        if command == "stop":
            argv.append("--apply")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(argv)
        return code, json.loads(output.getvalue())

    def snapshot(self):
        return {str(path.relative_to(self.base)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.base.rglob("*") if path.is_file() and not path.is_symlink()}

    def test_real_git_plan_is_read_only(self):
        before = self.snapshot()
        code, result = self.invoke("plan-stop")
        self.assertEqual((0, "stop_ready"), (code, result["reason"]))
        self.assertEqual(before, self.snapshot())
        self.assertEqual([], list(self.operator.iterdir()))

    def test_main_stop_auto_remove_replay_and_status(self):
        self.assertEqual("stopped", self.invoke("stop")[1]["reason"])
        with patch.object(cli, "_load_snapshot", side_effect=AssertionError("must replay receipt first")):
            self.assertEqual((0, "already_stopped"), (lambda r: (r[0], r[1]["reason"]))(self.invoke("stop")))
            self.assertEqual("already_stopped", self.invoke("status")[1]["reason"])
        self.assertEqual(1, self.stop_calls)

    def test_replay_rejects_changed_full_identity(self):
        self.invoke("stop")
        for override in ({"expected_head": "1" * 40}, {"policy_digest": "2" * 64}, {"generation": 2}):
            code, result = self.invoke("stop", **override)
            self.assertEqual(1, code)
            self.assertEqual("stop_receipt_conflict", result["reason"])
        self.assertEqual(1, self.stop_calls)

    def test_timeout_intent_remains_ambiguous_even_when_container_absent(self):
        self.stop_timeout = True
        self.assertEqual("stop_intent_ambiguous", self.invoke("stop")[1]["reason"])
        before = self.snapshot()
        self.assertEqual("stop_intent_ambiguous", self.invoke("status")[1]["reason"])
        self.assertEqual("stop_intent_ambiguous", self.invoke("stop")[1]["reason"])
        self.assertEqual("stop_intent_conflict", self.invoke("status", expected_head="1" * 40)[1]["reason"])
        self.assertEqual(1, self.stop_calls)
        self.assertEqual(before, self.snapshot())

    def test_concurrent_apply_stops_at_most_once(self):
        args = SimpleNamespace(command="stop", **self.identity)
        plan = cli._stop_plan(self.config, args)
        barrier = threading.Barrier(2)
        def apply():
            barrier.wait(timeout=5)
            try:
                return cli._apply_stop(self.config, args, plan)["reason"]
            except (cli.FixtureRunnerError, OSError):
                return "blocked"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: apply(), range(2)))
        self.assertIn("stopped", outcomes)
        self.assertEqual(1, self.stop_calls)

    def test_missing_environment_never_stops(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual("fixture_fault_enablement_missing", self.invoke("stop")[1]["reason"])
        self.assertEqual(0, self.stop_calls)

    def test_malformed_or_mismatched_session_receipts_reject(self):
        for name in ("generation.json", "codex-session.json", "codex-session-tools.json"):
            path = self.generation / name
            original = path.read_bytes()
            self.write_json(path, {})
            self.assertEqual(1, self.invoke("stop")[0])
            path.write_bytes(original)
        self.assertEqual(0, self.stop_calls)

    def test_finished_turn_cannot_authorize_stop(self):
        receipt = json.loads(self.turn_path.read_text())
        receipt["state"] = "finished"
        self.write_json(self.turn_path, receipt)
        self.assertEqual("turn_receipt_invalid", self.invoke("stop")[1]["reason"])

    def test_multiple_or_mismatched_container_blocks(self):
        self.multiple = True
        self.assertEqual("container_identity_ambiguous", self.invoke("stop")[1]["reason"])
        self.multiple = False
        self.row["Config"]["Labels"][DOCKER_LABEL_TURN] = "turn_" + "f" * 32
        self.assertEqual("container_identity_ambiguous", self.invoke("stop")[1]["reason"])
        self.assertEqual(0, self.stop_calls)

    def test_dirty_and_untracked_diagnose_preserves_bytes_and_index(self):
        self.present = False
        (self.repo / "README.md").write_text("dirty\n")
        (self.repo / "unknown.txt").write_text("unknown\n")
        before = self.snapshot()
        code, result = self.invoke("diagnose")
        self.assertEqual((0, "dirty"), (code, result["reason"]))
        self.assertEqual(["README.md"], result["tracked_paths"])
        self.assertEqual(["unknown.txt"], result["untracked_paths"])
        self.assertEqual(before, self.snapshot())

    def test_diagnose_busy_and_missing_lock_do_not_create_files(self):
        with self.config.active_lock_path.open("rb") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual("runner_operation_busy", self.invoke("diagnose")[1]["reason"])
        self.config.active_lock_path.unlink()
        before = self.snapshot()
        self.assertEqual(1, self.invoke("diagnose")[0])
        self.assertEqual(before, self.snapshot())

    def test_fsmonitor_and_filter_commands_are_not_executed(self):
        self.present = False
        marker = self.repo / "executed"
        self.git_run("config", "core.fsmonitor", "touch " + str(marker))
        self.assertEqual(0, self.invoke("diagnose")[0])
        self.assertFalse(marker.exists())
        self.git_run("config", "filter.evil.clean", "touch " + str(marker))
        (self.repo / ".gitattributes").write_text("README.md filter=evil\n")
        self.assertEqual("git_filter_configuration_unsafe", self.invoke("diagnose")[1]["reason"])
        self.assertFalse(marker.exists())

    def test_parent_symlink_and_receipt_hardlink_reject(self):
        self.turn_path.rename(self.turn_path.with_suffix(".saved"))
        os.link(self.turn_path.with_suffix(".saved"), self.turn_path)
        self.assertEqual(1, self.invoke("stop")[0])
        self.turn_path.unlink()
        self.turn_path.with_suffix(".saved").rename(self.turn_path)
        parent = self.state / "generations"
        parent.rename(self.state / "saved-generations")
        parent.symlink_to(self.state / "saved-generations", target_is_directory=True)
        self.assertEqual(1, self.invoke("stop")[0])
        self.assertEqual(0, self.stop_calls)

    def test_operator_root_symlink_rejects_even_receipt_reads(self):
        self.operator.rmdir()
        self.operator.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(1, self.invoke("status")[0])
        self.assertEqual(0, self.stop_calls)

    def test_absence_schema_and_exact_identity(self):
        self.present = False
        directory = self.root / ".absences"
        directory.mkdir(mode=0o700)
        receipt = RunnerAbsenceReply(WORK_ITEM, REPOSITORY, 7, "codex/issue-7-fixture",
                                    self.head, "1" * 64, "2" * 64, "2026-09-20T00:00:00Z")
        self.write_json(directory / f"{WORK_ITEM}.json", receipt.to_mapping())
        self.assertEqual("absent", self.invoke("diagnose")[1]["archive_status"])
        self.assertEqual("absence_identity_mismatch", self.invoke("diagnose", issue=8)[1]["reason"])

    def test_archive_schema_and_metadata_binding(self):
        self.present = False
        directory = self.root / ".archives"
        directory.mkdir(mode=0o700)
        record = {"version": 2, "work_item_id": WORK_ITEM, "expected_head_sha": self.head,
                  "metadata_sha256": cli._canonical_sha(self.metadata), "state": "archiving",
                  "reclaimed_bytes": 0, "archived_at": None, "storage_kind": "bounded_image"}
        self.write_json(directory / f"{WORK_ITEM}.json", record)
        code, result = self.invoke("diagnose")
        self.assertEqual(0, code, result)
        self.assertEqual("archiving", result["archive_status"])
        self.assertFalse(result["cleanup_verified"])
        record["metadata_sha256"] = "0" * 64
        self.write_json(directory / f"{WORK_ITEM}.json", record)
        self.assertEqual("archive_identity_mismatch", self.invoke("diagnose")[1]["reason"])

    def test_changed_head_and_branch_are_blocked(self):
        self.assertEqual("workspace_head_mismatch", self.invoke("plan-stop", expected_head="1" * 40)[1]["reason"])
        self.git_run("checkout", "-b", "other")
        self.assertEqual("workspace_branch_mismatch", self.invoke("plan-stop")[1]["reason"])

    def test_cleanup_rejects_archive_staging_and_registry_after_absence(self):
        shutil.rmtree(self.workspace)
        self.assertFalse(cli._storage_absent(self.config, self.identity, archive="absent"))
        (self.root / ".registry" / f"{WORK_ITEM}.json").unlink()
        self.assertTrue(cli._storage_absent(self.config, self.identity, archive="absent"))
        staging = self.root / ".archive-staging"
        staging.mkdir(mode=0o700)
        (staging / f"{WORK_ITEM}.workspace").mkdir(mode=0o700)
        self.assertFalse(cli._storage_absent(self.config, self.identity, archive="archived"))
        self.assertFalse(cli._storage_absent(self.config, self.identity, archive="absent"))

    def test_errors_do_not_echo_os_exception_details(self):
        with patch.object(cli, "load_runner_configuration", side_effect=OSError("secret-config-value")):
            code, result = self.invoke("diagnose")
        self.assertEqual((1, "operator_inspection_failed"), (code, result["reason"]))


if __name__ == "__main__":
    unittest.main()
