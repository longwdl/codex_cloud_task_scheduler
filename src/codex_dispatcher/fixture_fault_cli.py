"""Standalone, triple-opt-in entry point for the dedicated live Fixture."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from codex_dispatcher.fixture_faults import (
    FIXTURE_REPOSITORY,
    FIXTURE_SLACK_WORKSPACE_ID,
    FixtureFaultInjection,
    FixtureFaultPoint,
    FixtureFaultRejected,
    FixtureProcessInterrupted,
    FixtureReceiptLost,
    validate_fixture_config,
    validate_fixture_preflight,
    validate_fixture_slack_config,
)
from codex_dispatcher.redaction import redact_text
from codex_dispatcher.ssh_runtime import (
    SshRuntimeError,
    build_ssh_fixture_fault_sweep,
    load_protected_ssh_config,
    run_ssh_preflight,
    validate_runtime_state_path,
)
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.slack_reporting import SlackDeliveryState, SlackReportKind
from codex_dispatcher.slack_live_fixture import verify_slack_workspace


_FAULT_ENV = "CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS"
_WRITE_ENV = "CODEX_DISPATCHER_ENABLE_SSH_WRITES"
_SLACK_WRITE_ENV = "CODEX_DISPATCHER_ENABLE_SLACK_WRITES"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m codex_dispatcher.fixture_fault_cli",
        description="Inject one guarded failure in the fixed private Fixture.",
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument(
        "--fault",
        required=True,
        choices=tuple(
            point.value
            for point in FixtureFaultPoint
            if point is not FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        required=True,
        help="Required acknowledgement that this performs a real Fixture write.",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def _github_token() -> str | None:
    for variable in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(variable)
        if token is not None and token.startswith(("github_pat_", "ghp_")):
            return token
    return None


def _slack_bot_token() -> str | None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if (
        token is not None
        and token.startswith("xoxb-")
        and 16 <= len(token) <= 512
        and not any(character.isspace() or ord(character) < 32 for character in token)
    ):
        return token
    return None


def _create_backup(database_path: Path, fault: FixtureFaultPoint) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f"state.pre-{fault.value}-",
        suffix=".db",
        dir=database_path.parent,
    )
    os.close(descriptor)
    backup_path = Path(raw_path)
    os.chmod(backup_path, 0o600)
    try:
        with StateStore(database_path, read_only=True) as source:
            if source.integrity_check() != "ok":
                raise SshRuntimeError("state database integrity check failed before backup")
            source.backup(backup_path)
        with StateStore(backup_path, read_only=True) as backup:
            if backup.integrity_check() != "ok":
                raise SshRuntimeError("Fixture fault backup integrity check failed")
    except Exception:
        backup_path.unlink(missing_ok=True)
        raise
    return backup_path


def _run(
    config_path: Path,
    *,
    issue_number: int,
    fault: FixtureFaultPoint,
) -> tuple[int, dict[str, object]]:
    if fault is FixtureFaultPoint.CLAIM_ACQUIRED_PROCESS_KILL:
        return 1, {
            "ok": False,
            "error": "claim-acquired process kill requires fixture_process_cli",
        }
    if os.environ.get(_WRITE_ENV) != "1":
        return 1, {"ok": False, "error": f"{_WRITE_ENV}=1 is required"}
    if os.environ.get(_FAULT_ENV) != FIXTURE_REPOSITORY:
        return 1, {
            "ok": False,
            "error": f"{_FAULT_ENV} must equal the fixed Fixture repository",
        }
    token = _github_token()
    if token is None:
        return 1, {"ok": False, "error": "a recognized explicit GitHub token is required"}
    if type(issue_number) is not int or issue_number <= 0:
        return 1, {"ok": False, "error": "fixture issue number must be positive"}

    backup_path: Path | None = None
    injection: FixtureFaultInjection | None = None
    slack_token: str | None = None
    try:
        config = load_protected_ssh_config(config_path)
        validate_runtime_state_path(config.scheduler.database_path)
        validate_fixture_config(config)
        if fault in {
            FixtureFaultPoint.SLACK_ROOT_RECEIPT,
            FixtureFaultPoint.SLACK_TERMINAL_RECEIPT,
        }:
            validate_fixture_slack_config(config)
        if config.slack_runtime is not None:
            if os.environ.get(_SLACK_WRITE_ENV) != "1":
                raise FixtureFaultRejected(
                    f"{_SLACK_WRITE_ENV}=1 is required for configured Slack output"
                )
            slack_token = _slack_bot_token()
            if slack_token is None:
                raise FixtureFaultRejected(
                    "a recognized explicit Slack bot token is required"
                )
            if fault in {
                FixtureFaultPoint.SLACK_ROOT_RECEIPT,
                FixtureFaultPoint.SLACK_TERMINAL_RECEIPT,
            }:
                verify_slack_workspace(
                    bot_token=slack_token,
                    workspace_id=FIXTURE_SLACK_WORKSPACE_ID,
                    timeout_seconds=config.slack_runtime.request_timeout_seconds,
                )
        if not config.scheduler.database_path.is_file():
            raise FixtureFaultRejected("Fixture fault requires an existing state database")

        inspection = run_ssh_preflight(config=config, github_token=token)
        with StateStore(config.scheduler.database_path, read_only=True) as proof_store:
            validate_fixture_preflight(
                inspection.plan,
                fault=fault,
                issue_number=issue_number,
                store=proof_store,
            )
        backup_path = _create_backup(config.scheduler.database_path, fault)
        injection = FixtureFaultInjection(fault=fault, issue_number=issue_number)
        result = None
        interruption: str | None = None
        observed_work_item = None
        observed_turn = None
        with StateStore(config.scheduler.database_path) as store:
            store.migrate()
            if store.integrity_check() != "ok":
                raise SshRuntimeError("state database integrity check failed")
            sweep = build_ssh_fixture_fault_sweep(
                config=config,
                store=store,
                github_token=token,
                injection=injection,
                slack_token=slack_token,
            )
            try:
                result = sweep.run_once()
            except FixtureReceiptLost:
                interruption = "receipt_lost"
            except FixtureProcessInterrupted:
                interruption = "process_interrupted"
            observed_work_item = store.get_work_item_by_issue(
                FIXTURE_REPOSITORY,
                issue_number,
            )
            observed_turn = store.get_active_turn()
            observed_turns = (
                ()
                if observed_work_item is None
                else store.list_turns(observed_work_item.work_item_id)
            )
            observed_slack_delivery = (
                None
                if injection.slack_receipt is None
                else store.get_slack_delivery(
                    injection.slack_receipt.deduplication_key
                )
            )
            observed_slack_root = (
                None
                if observed_work_item is None
                else store.get_slack_delivery(
                    f"slack:{observed_work_item.work_item_id}:root"
                )
            )

        if fault in {
            FixtureFaultPoint.RECORDED_PUBLICATION_RECOVERY,
            FixtureFaultPoint.START_STATUS_RECOVERY,
        }:
            if injection.triggered or interruption is not None:
                raise FixtureFaultRejected(
                    "guarded Fixture recovery unexpectedly triggered a fault"
                )
            if result is None or result.status.value != "review":
                raise FixtureFaultRejected(
                    "guarded Fixture recovery did not finish in review"
                )
            if (
                fault is FixtureFaultPoint.START_STATUS_RECOVERY
                and tuple(operation.value for operation in injection.recovery_operations)
                != ("status", "export")
            ):
                raise FixtureFaultRejected(
                    "START recovery did not use exact STATUS then EXPORT operations"
                )
            return 0, {
                "ok": True,
                "fixture_fault": True,
                "fault": fault.value,
                "fault_triggered": False,
                "recovery_guarded": True,
                "recovery_required": False,
                "repository": FIXTURE_REPOSITORY,
                "issue_number": issue_number,
                "work_item_id": result.work_item_id,
                "turn_id": result.turn_id,
                "status": result.status.value,
                "runner_operations": [
                    operation.value for operation in injection.recovery_operations
                ],
                "backup_path": str(backup_path),
            }

        if not injection.triggered:
            raise FixtureFaultRejected("requested Fixture fault point was not reached")
        if fault is FixtureFaultPoint.PUBLICATION_RECORDED:
            if (
                interruption != "process_interrupted"
                or observed_work_item is None
                or observed_work_item.state.value != "running"
                or observed_work_item.last_published_sha is None
                or observed_turn is None
                or observed_turn.work_item_id != observed_work_item.work_item_id
                or observed_turn.state.value != "checkpointing"
                or observed_turn.output_head_sha
                != observed_work_item.last_published_sha
            ):
                raise FixtureFaultRejected(
                    "publication-recorded fault did not preserve the exact recovery state"
                )
            status = interruption
            work_item_id = observed_work_item.work_item_id
            turn_id = observed_turn.turn_id
        elif fault is FixtureFaultPoint.PUBLISHER_RECEIPT:
            if result is None or result.status.value != "awaiting_publication":
                raise FixtureFaultRejected(
                    "Publisher receipt fault did not enter publication recovery"
                )
            status = result.status.value
            work_item_id = result.work_item_id
            turn_id = result.turn_id
        elif fault in {
            FixtureFaultPoint.START_RECEIPT,
            FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL,
        }:
            if (
                result is None
                or result.status.value != "runner_active"
                or observed_work_item is None
                or observed_work_item.state.value != "running"
                or observed_work_item.codex_session_id is not None
                or observed_turn is None
                or observed_turn.work_item_id != observed_work_item.work_item_id
                or observed_turn.state.value != "reconciling"
                or observed_turn.output_head_sha is not None
            ):
                raise FixtureFaultRejected(
                    "START receipt fault did not preserve one ambiguous active Turn"
                )
            if fault is FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL and (
                injection.ssh_process_pid is None
                or injection.ssh_process_group_id != injection.ssh_process_pid
                or injection.ssh_session_id != injection.ssh_process_pid
                or injection.ssh_status_state is None
                or injection.ssh_status_attempts <= 0
                or injection.ssh_interrupt_rejection is not None
            ):
                raise FixtureFaultRejected(
                    "SSH transport fault did not prove one exact terminated client"
                )
            status = result.status.value
            work_item_id = observed_work_item.work_item_id
            turn_id = observed_turn.turn_id
        elif fault is FixtureFaultPoint.SLACK_ROOT_RECEIPT:
            if (
                result is not None
                or interruption != "receipt_lost"
                or observed_work_item is None
                or observed_work_item.state.value != "ready"
                or observed_work_item.codex_session_id is not None
                or observed_work_item.last_published_sha is not None
                or observed_work_item.pr_number is not None
                or observed_work_item.slack_thread_ts is not None
                or observed_turn is not None
                or observed_turns
                or observed_slack_root is None
                or observed_slack_root.kind is not SlackReportKind.ROOT
                or observed_slack_root.state is not SlackDeliveryState.PREPARED
                or injection.slack_receipt is None
                or observed_slack_delivery != observed_slack_root
            ):
                raise FixtureFaultRejected(
                    "Slack root receipt fault did not preserve exact unstarted recovery"
                )
            status = interruption
            work_item_id = observed_work_item.work_item_id
            turn_id = None
        elif fault is FixtureFaultPoint.SLACK_TERMINAL_RECEIPT:
            terminal_turn = observed_turns[-1] if len(observed_turns) == 1 else None
            if (
                result is not None
                or interruption != "receipt_lost"
                or observed_work_item is None
                or observed_work_item.state.value != "review"
                or observed_work_item.codex_session_id is None
                or observed_work_item.last_published_sha is None
                or observed_work_item.pr_number is None
                or observed_work_item.slack_thread_ts is None
                or observed_turn is not None
                or terminal_turn is None
                or terminal_turn.state.value != "finished"
                or terminal_turn.result_status != "completed"
                or terminal_turn.output_head_sha
                != observed_work_item.last_published_sha
                or observed_slack_root is None
                or observed_slack_root.state is not SlackDeliveryState.DELIVERED
                or observed_slack_delivery is None
                or observed_slack_delivery.kind is not SlackReportKind.RESULT
                or observed_slack_delivery.state is not SlackDeliveryState.PREPARED
                or injection.slack_receipt is None
            ):
                raise FixtureFaultRejected(
                    "Slack terminal receipt fault did not preserve exact projection recovery"
                )
            status = interruption
            work_item_id = observed_work_item.work_item_id
            turn_id = terminal_turn.turn_id
        elif fault in {
            FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT,
            FixtureFaultPoint.COMPLETION_LABEL_RECEIPT,
        }:
            terminal_turn = observed_turns[-1] if len(observed_turns) == 1 else None
            label_expected = fault is FixtureFaultPoint.COMPLETION_LABEL_RECEIPT
            if (
                result is not None
                or interruption != "receipt_lost"
                or observed_work_item is None
                or observed_work_item.state.value != "completed"
                or observed_work_item.last_published_sha is None
                or observed_work_item.pr_number is None
                or observed_turn is not None
                or terminal_turn is None
                or terminal_turn.state.value != "finished"
                or terminal_turn.result_status != "completed"
                or terminal_turn.output_head_sha
                != observed_work_item.last_published_sha
                or not injection.completion_identity_validated
                or not injection.completion_comment_projected
                or injection.completion_label_projected is not label_expected
            ):
                raise FixtureFaultRejected(
                    "completion receipt fault did not preserve exact projection recovery"
                )
            status = interruption
            work_item_id = observed_work_item.work_item_id
            turn_id = terminal_turn.turn_id
        else:
            if result is not None or interruption != "receipt_lost":
                raise FixtureFaultRejected("Fixture receipt loss unexpectedly returned a sweep result")
            status = "receipt_lost"
            work_item_id = inspection.plan.work_item.work_item_id
            turn_id = None
        payload: dict[str, object] = {
            "ok": True,
            "fixture_fault": True,
            "fault": fault.value,
            "fault_triggered": True,
            "recovery_required": True,
            "repository": FIXTURE_REPOSITORY,
            "issue_number": issue_number,
            "work_item_id": work_item_id,
            "turn_id": turn_id,
            "status": status,
            "backup_path": str(backup_path),
        }
        if fault is FixtureFaultPoint.SSH_TRANSPORT_PROCESS_KILL:
            payload.update(
                {
                    "termination_signal": "SIGKILL",
                    "ssh_process_pid": injection.ssh_process_pid,
                    "ssh_process_group_id": injection.ssh_process_group_id,
                    "ssh_session_id": injection.ssh_session_id,
                    "status_proof_state": injection.ssh_status_state.value,
                    "status_proof_error_code": injection.ssh_status_error_code,
                    "status_proof_attempts": injection.ssh_status_attempts,
                    "local_work_item_persisted": True,
                    "turn_state": observed_turn.state.value,
                }
            )
        if fault in {
            FixtureFaultPoint.SLACK_ROOT_RECEIPT,
            FixtureFaultPoint.SLACK_TERMINAL_RECEIPT,
        }:
            slack_receipt = injection.slack_receipt
            if slack_receipt is None:
                raise FixtureFaultRejected(
                    "Slack receipt fault has no exact discarded receipt"
                )
            payload.update(
                {
                    "slack_receipt_discarded": True,
                    "slack_message_ts": slack_receipt.message_ts,
                    "slack_thread_ts": slack_receipt.thread_ts,
                    "slack_permalink": slack_receipt.permalink,
                    "slack_outbox_state": "prepared",
                }
            )
        if fault in {
            FixtureFaultPoint.COMPLETION_COMMENT_RECEIPT,
            FixtureFaultPoint.COMPLETION_LABEL_RECEIPT,
        }:
            payload.update(
                {
                    "completion_identity_validated": True,
                    "completion_comment_projected": True,
                    "completion_label_projected": (
                        fault is FixtureFaultPoint.COMPLETION_LABEL_RECEIPT
                    ),
                    "work_item_state": "completed",
                }
            )
        return 0, payload
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        return 1, {
            "ok": False,
            "fixture_fault": True,
            "fault": fault.value,
            "fault_triggered": False if injection is None else injection.triggered,
            "repository": FIXTURE_REPOSITORY,
            "issue_number": issue_number,
            "error": redact_text(
                str(exc),
                (token,) if slack_token is None else (token, slack_token),
            ),
            "ssh_interrupt_rejection": (
                None if injection is None else injection.ssh_interrupt_rejection
            ),
            "backup_path": None if backup_path is None else str(backup_path),
        }


def _emit(payload: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    print(f"ok: {str(payload.get('ok', False)).lower()}")
    if "error" in payload:
        print(f"error: {payload['error']}", file=sys.stderr)
    else:
        print(f"fault: {payload.get('fault')}")
        print(f"status: {payload.get('status')}")
        print(f"recovery_required: {str(payload.get('recovery_required', False)).lower()}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    code, payload = _run(
        args.config,
        issue_number=args.issue,
        fault=FixtureFaultPoint(args.fault),
    )
    _emit(payload, as_json=args.json)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
