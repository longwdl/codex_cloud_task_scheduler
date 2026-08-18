"""OS-process kill fixture for one exact post-claim Dispatcher boundary."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from codex_dispatcher.fixture_fault_cli import _create_backup, _github_token
from codex_dispatcher.fixture_faults import (
    FIXTURE_REPOSITORY,
    FixtureFaultInjection,
    FixtureFaultPoint,
    FixtureFaultRejected,
    validate_fixture_config,
    validate_fixture_orphan_claim_recovery,
    validate_fixture_preflight,
)
from codex_dispatcher.redaction import redact_text
from codex_dispatcher.ssh_preflight import SshPreflightStatus
from codex_dispatcher.ssh_runtime import (
    SshPreflightInspection,
    SshRuntimeError,
    build_ssh_fixture_fault_sweep,
    load_protected_ssh_config,
    run_ssh_preflight,
    validate_runtime_state_path,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.trackers.base import TrackerTask


_FAULT_ENV = "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS"
_WRITE_ENV = "CODEX_DISPATCHER_ENABLE_SSH_WRITES"
# Must exceed the longest bounded pre-claim Git operation (120 seconds).  A
# shorter parent deadline can kill Python while its isolated Git process group
# is still active, leaving that group orphaned on POSIX.
_HANDSHAKE_TIMEOUT_SECONDS = 180
_POST_KILL_ATTEMPTS = 10
_POST_KILL_DELAY_SECONDS = 1.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m codex_dispatcher.fixture_process_cli",
        description=(
            "Kill one exact Fixture Dispatcher child after its GitHub claim and "
            "before local WorkItem persistence."
        ),
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument(
        "--apply",
        action="store_true",
        required=True,
        help="Required acknowledgement that this claims a real Fixture Issue and kills a child.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--internal-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--signal-fd", type=int, help=argparse.SUPPRESS)
    return parser


def _guard_environment(issue_number: int) -> tuple[str | None, dict[str, object] | None]:
    if os.environ.get(_WRITE_ENV) != "1":
        return None, {"ok": False, "error": f"{_WRITE_ENV}=1 is required"}
    if os.environ.get(_FAULT_ENV) != FIXTURE_REPOSITORY:
        return None, {
            "ok": False,
            "error": f"{_FAULT_ENV} must equal the fixed Fixture repository",
        }
    token = _github_token()
    if token is None:
        return None, {"ok": False, "error": "a recognized explicit GitHub token is required"}
    if type(issue_number) is not int or issue_number <= 0:
        return None, {"ok": False, "error": "fixture issue number must be positive"}
    return token, None


def _handshake(task: TrackerTask) -> bytes:
    payload = {
        "event": "claim_acquired",
        "issue_node_id": task.issue_node_id,
        "issue_number": task.issue_number,
        "repository": task.repository,
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _wait_for_exact_handshake(
    descriptor: int,
    expected: bytes,
    *,
    timeout_seconds: int = _HANDSHAKE_TIMEOUT_SECONDS,
) -> None:
    if descriptor < 0 or not expected or len(expected) > 1024:
        raise FixtureFaultRejected("invalid process-kill handshake contract")
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        if not selector.select(timeout_seconds):
            raise FixtureFaultRejected("timed out waiting for the exact post-claim handshake")
    observed = os.read(descriptor, len(expected) + 1)
    if observed != expected:
        raise FixtureFaultRejected("process-kill child returned an invalid handshake")


def _kill_exact_child(
    process: subprocess.Popen[bytes],
    expected_argv: tuple[str, ...],
) -> int:
    if tuple(process.args) != expected_argv or process.poll() is not None:
        raise FixtureFaultRejected("refusing to kill an unverified or exited child process")
    process.kill()
    try:
        exit_code = process.wait(timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise FixtureFaultRejected("killed Fixture child did not exit") from exc
    if exit_code != -signal.SIGKILL:
        raise FixtureFaultRejected("Fixture child did not terminate by SIGKILL")
    return exit_code


def _stop_child_if_running(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _child_environment(token: str) -> dict[str, str]:
    source_root = Path(__file__).resolve().parents[1]
    return {
        _WRITE_ENV: "1",
        _FAULT_ENV: FIXTURE_REPOSITORY,
        "GITHUB_TOKEN": token,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": str(source_root),
    }


def _child_argv(config_path: Path, issue_number: int, descriptor: int) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "codex_dispatcher.fixture_process_cli",
        "--config",
        str(config_path),
        "--issue",
        str(issue_number),
        "--apply",
        "--internal-child",
        "--signal-fd",
        str(descriptor),
    )


def _await_orphan_recovery(
    *,
    config,
    token: str,
    issue_number: int,
) -> SshPreflightInspection:
    last: SshPreflightInspection | None = None
    for attempt in range(_POST_KILL_ATTEMPTS):
        last = run_ssh_preflight(config=config, github_token=token)
        try:
            validate_fixture_orphan_claim_recovery(
                last.plan,
                issue_number=issue_number,
            )
        except FixtureFaultRejected:
            task = last.plan.task
            transient_same_issue = (
                last.plan.status in {
                    SshPreflightStatus.IDLE,
                    SshPreflightStatus.READY_CANDIDATE,
                }
                and (task is None or task.issue_number == issue_number)
            )
            if not transient_same_issue or attempt + 1 == _POST_KILL_ATTEMPTS:
                raise
            time.sleep(_POST_KILL_DELAY_SECONDS)
            continue
        return last
    raise AssertionError("post-kill preflight loop exhausted without a result")


def _run_child(config_path: Path, *, issue_number: int, signal_fd: int | None) -> int:
    token, error = _guard_environment(issue_number)
    if error is not None or token is None:
        return 1
    if signal_fd is None or signal_fd < 3:
        return 1
    try:
        if not stat.S_ISFIFO(os.fstat(signal_fd).st_mode):
            return 1
        config = load_protected_ssh_config(config_path)
        validate_runtime_state_path(config.scheduler.database_path)
        validate_fixture_config(config)
        inspection = run_ssh_preflight(config=config, github_token=token)
        validate_fixture_preflight(
            inspection.plan,
            fault=FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL,
            issue_number=issue_number,
        )
        expected_task = inspection.plan.task
        assert expected_task is not None

        def signal_and_wait(task: TrackerTask) -> None:
            if task.issue_node_id != expected_task.issue_node_id:
                raise FixtureFaultRejected("claimed Issue identity changed after preflight")
            message = _handshake(task)
            if os.write(signal_fd, message) != len(message):
                raise FixtureFaultRejected("post-claim handshake write was incomplete")
            while True:
                signal.pause()

        injection = FixtureFaultInjection(
            FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL,
            issue_number,
            claim_acquired_callback=signal_and_wait,
        )
        with StateStore(config.scheduler.database_path) as store:
            store.migrate()
            if store.integrity_check() != "ok":
                raise SshRuntimeError("state database integrity check failed")
            sweep = build_ssh_fixture_fault_sweep(
                config=config,
                store=store,
                github_token=token,
                injection=injection,
            )
            sweep.run_once()
        return 1
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        return 1


def _run_parent(
    config_path: Path,
    *,
    issue_number: int,
) -> tuple[int, dict[str, object]]:
    token, error = _guard_environment(issue_number)
    if error is not None or token is None:
        assert error is not None
        return 1, error

    backup_path: Path | None = None
    process: subprocess.Popen[bytes] | None = None
    read_fd: int | None = None
    write_fd: int | None = None
    try:
        config = load_protected_ssh_config(config_path)
        validate_runtime_state_path(config.scheduler.database_path)
        validate_fixture_config(config)
        if not config.scheduler.database_path.is_file():
            raise FixtureFaultRejected("process-kill fixture requires an existing state database")
        inspection = run_ssh_preflight(config=config, github_token=token)
        validate_fixture_preflight(
            inspection.plan,
            fault=FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL,
            issue_number=issue_number,
        )
        task = inspection.plan.task
        assert task is not None
        backup_path = _create_backup(
            config.scheduler.database_path,
            FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL,
        )

        read_fd, write_fd = os.pipe()
        argv = _child_argv(config_path, issue_number, write_fd)
        process = subprocess.Popen(
            argv,
            cwd=Path(__file__).resolve().parents[2],
            env=_child_environment(token),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(write_fd,),
            start_new_session=True,
        )
        os.close(write_fd)
        write_fd = None
        _wait_for_exact_handshake(read_fd, _handshake(task))
        exit_code = _kill_exact_child(process, argv)
        process = None

        validate_runtime_state_path(config.scheduler.database_path)
        with StateStore(config.scheduler.database_path, read_only=True) as store:
            if store.integrity_check() != "ok":
                raise SshRuntimeError("state database failed integrity check after process kill")
            if store.get_work_item_by_issue(FIXTURE_REPOSITORY, issue_number) is not None:
                raise FixtureFaultRejected(
                    "process kill occurred after unexpected WorkItem persistence"
                )
            if store.get_active_turn() is not None:
                raise FixtureFaultRejected("process kill left an unexpected active Turn")

        recovery = _await_orphan_recovery(
            config=config,
            token=token,
            issue_number=issue_number,
        )
        recovered_task = recovery.plan.task
        assert recovered_task is not None
        return 0, {
            "ok": True,
            "fixture_process_kill": True,
            "stage": FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL.value,
            "repository": FIXTURE_REPOSITORY,
            "issue_number": issue_number,
            "issue_node_id": recovered_task.issue_node_id,
            "terminated": True,
            "termination_signal": "SIGKILL",
            "child_exit_code": exit_code,
            "local_work_item_persisted": False,
            "runner_reached": False,
            "recovery_required": True,
            "recovery_action": recovery.plan.recovery_action.value,
            "status": recovery.plan.status.value,
            "backup_path": str(backup_path),
        }
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return 1, {
            "ok": False,
            "fixture_process_kill": True,
            "stage": FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL.value,
            "repository": FIXTURE_REPOSITORY,
            "issue_number": issue_number,
            "error": redact_text(str(exc), (token,)),
            "backup_path": None if backup_path is None else str(backup_path),
        }
    finally:
        _stop_child_if_running(process)
        for descriptor in (read_fd, write_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _emit(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    print(f"ok: {str(payload.get('ok', False)).lower()}")
    if "error" in payload:
        print(f"error: {payload['error']}", file=sys.stderr)
        return
    print(f"stage: {payload.get('stage')}")
    print(f"terminated: {str(payload.get('terminated', False)).lower()}")
    print(f"recovery_action: {payload.get('recovery_action')}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.internal_child:
        return _run_child(
            args.config,
            issue_number=args.issue,
            signal_fd=args.signal_fd,
        )
    if args.signal_fd is not None:
        _emit(
            {"ok": False, "error": "--signal-fd is internal-only"},
            as_json=args.json,
        )
        return 1
    code, payload = _run_parent(args.config, issue_number=args.issue)
    _emit(payload, as_json=args.json)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
