"""Isolated four-trigger reclamation and system-channel projection canary."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Sequence

from codex_dispatcher.health_alert_delivery import (
    HealthAlertDeliveryCoordinator,
    health_alert_fingerprint,
)
from codex_dispatcher.lifecycle_health import LifecycleAlert
from codex_dispatcher.runner_asset_reclamation import (
    DockerImageAsset,
    ReleaseAsset,
    RunnerAssetSnapshot,
    plan_runner_asset_reclamation,
)
from codex_dispatcher.runner_reclamation_status import (
    MAXIMUM_RELEASE_COUNT,
    MINIMUM_AVAILABLE_BYTES,
    MINIMUM_RECLAIMABLE_BYTES,
    build_runner_reclamation_status,
)
from codex_dispatcher.slack_reporting import (
    SlackDeliveryReceipt,
    SlackOutboundMessage,
    SlackOutboundPublisher,
    validate_slack_channel_id,
)
from codex_dispatcher.state_store import StateStore


CANARY_ROOT = Path("/var/lib/codex-dispatcher/reclamation-canaries")
_FIXTURE_RE = re.compile(r"rc_[0-9a-f]{32}")

_CURRENT = "a" * 40
_ROLLBACK = "b" * 40
_TARGET = "c" * 40
_CONFIGURED_IMAGE = "ghcr.io/longwdl/codex-cloud-task-scheduler-runner@sha256:" + "d" * 64
_TARGET_IMAGE = "ghcr.io/longwdl/codex-cloud-task-scheduler-runner@sha256:" + "e" * 64


class ReclamationCanaryError(RuntimeError):
    """Raised before the isolated canary can prove all safety properties."""


def build_reclamation_trigger_canary(
    *, now: datetime | None = None
) -> dict[str, object]:
    """Build one real exact plan and four independent threshold envelopes."""
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("canary timestamp must be timezone-aware")
    plan = plan_runner_asset_reclamation(
        RunnerAssetSnapshot(
            current_release_commit=_CURRENT,
            rollback_release_commits=(_ROLLBACK,),
            configured_image=_CONFIGURED_IMAGE,
            rollback_images=(_CONFIGURED_IMAGE,),
            work_item_images=(),
            registry_work_item_ids=(),
            archived_work_item_ids=(),
            absent_work_item_ids=(),
            releases=(
                ReleaseAsset(_CURRENT, f"/srv/codex-runner/releases/{_CURRENT}", "1" * 64, 4096),
                ReleaseAsset(_ROLLBACK, f"/srv/codex-runner/releases/{_ROLLBACK}", "2" * 64, 4096),
                ReleaseAsset(_TARGET, f"/srv/codex-runner/releases/{_TARGET}", "3" * 64, 8192),
            ),
            images=(
                DockerImageAsset(
                    "sha256:" + "4" * 64,
                    (_CONFIGURED_IMAGE,),
                    (),
                    _CURRENT,
                    "oci_revision",
                    1024,
                    1024,
                    0,
                    "https://github.com/longwdl/codex_cloud_task_scheduler",
                    "codex-cloud-task-scheduler-runner",
                ),
                DockerImageAsset(
                    "sha256:" + "5" * 64,
                    (_TARGET_IMAGE,),
                    (),
                    _TARGET,
                    "oci_revision",
                    MINIMUM_RECLAIMABLE_BYTES,
                    MINIMUM_RECLAIMABLE_BYTES,
                    0,
                    "https://github.com/longwdl/codex_cloud_task_scheduler",
                    "codex-cloud-task-scheduler-runner",
                ),
            ),
        )
    )
    cases = (
        (
            "host_available_below_threshold",
            dict(
                host_available_bytes=MINIMUM_AVAILABLE_BYTES - 1,
                release_count=2,
                release_target_count=1,
                image_target_count=0,
                expected_total_bytes=1,
            ),
        ),
        (
            "release_count_above_limit",
            dict(
                host_available_bytes=MINIMUM_AVAILABLE_BYTES,
                release_count=MAXIMUM_RELEASE_COUNT + 1,
                release_target_count=1,
                image_target_count=0,
                expected_total_bytes=1,
            ),
        ),
        (
            "unreferenced_images_present",
            dict(
                host_available_bytes=MINIMUM_AVAILABLE_BYTES,
                release_count=2,
                release_target_count=0,
                image_target_count=1,
                expected_total_bytes=1,
            ),
        ),
        (
            "reclaimable_bytes_above_threshold",
            dict(
                host_available_bytes=MINIMUM_AVAILABLE_BYTES,
                release_count=2,
                release_target_count=1,
                image_target_count=0,
                expected_total_bytes=MINIMUM_RECLAIMABLE_BYTES,
            ),
        ),
    )
    statuses: list[dict[str, object]] = []
    for expected_reason, values in cases:
        status = build_runner_reclamation_status(
            **values,  # type: ignore[arg-type]
            plan_sha256=plan.plan_sha256,
            now=moment,
        )
        if status.trigger_reasons != (expected_reason,):
            raise ReclamationCanaryError("threshold canary reason is not isolated")
        statuses.append(
            {"case": expected_reason, "status": status.to_mapping()}
        )
    mapping = plan.to_mapping()
    if mapping.get("authorizes_apply") is not False:
        raise ReclamationCanaryError("canary plan unexpectedly authorizes deletion")
    return {
        "schema_version": 1,
        "kind": "runner_reclamation_threshold_canary",
        "plan": mapping,
        "threshold_cases": statuses,
        "authorizes_apply": False,
        "asset_writes": 0,
        "asset_deletions": 0,
    }


def run_reclamation_projection_canary(
    *,
    fixture_id: str,
    system_channel_id: str,
    issue_channel_id: str,
    publisher: SlackOutboundPublisher,
    receipt_root: Path,
    external_writes: bool,
    now: datetime | None = None,
) -> dict[str, object]:
    """Project one isolated alert/recovery episode and retain exact receipts."""
    if _FIXTURE_RE.fullmatch(fixture_id) is None:
        raise ValueError("fixture_id must match rc_<32 lowercase hex>")
    system_channel = validate_slack_channel_id(system_channel_id)
    issue_channel = validate_slack_channel_id(issue_channel_id)
    if system_channel == issue_channel:
        raise ValueError("system and Issue channels must differ")
    if not hasattr(publisher, "publish"):
        raise TypeError("publisher must implement Slack outbound publication")
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("canary timestamp must be timezone-aware")
    moment = moment.astimezone(timezone.utc)

    root = receipt_root / fixture_id
    receipt_path = root / "receipt.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        receipt = _read_json(receipt_path)
        _validate_projection_receipt(
            receipt,
            fixture_id,
            system_channel,
            issue_channel,
            external_writes=external_writes,
        )
        return receipt
    _prepare_root(receipt_root, root)

    intent_path = root / "intent.json"
    existing_intent = (
        _read_json(intent_path)
        if intent_path.exists() and not intent_path.is_symlink()
        else None
    )
    if existing_intent is not None:
        if (
            existing_intent.get("schema_version") != 1
            or existing_intent.get("kind")
            != "runner_reclamation_projection_canary_intent"
            or existing_intent.get("fixture_id") != fixture_id
            or existing_intent.get("system_channel_id") != system_channel
            or existing_intent.get("issue_channel_id") != issue_channel
            or existing_intent.get("authorizes_apply") is not False
        ):
            raise ReclamationCanaryError("existing canary intent conflicts with request")
        checked_value = existing_intent.get("checked_at")
        if not isinstance(checked_value, str):
            raise ReclamationCanaryError("existing canary intent timestamp is invalid")
        try:
            moment = datetime.fromisoformat(checked_value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReclamationCanaryError(
                "existing canary intent timestamp is invalid"
            ) from exc
        if moment.tzinfo is None:
            raise ReclamationCanaryError("existing canary intent lacks timezone")
        moment = moment.astimezone(timezone.utc)

    trigger = build_reclamation_trigger_canary(now=moment)
    plan = trigger["plan"]
    assert isinstance(plan, dict)
    plan_sha = plan.get("plan_sha256")
    assert isinstance(plan_sha, str)
    alert = LifecycleAlert("runner_reclamation_plan_ready", plan_sha256=plan_sha)
    alerts = (alert,)
    checked_at = moment.isoformat().replace("+00:00", "Z")
    recovered_at = (moment + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    fingerprint = health_alert_fingerprint(alerts)
    identity = "\x00".join((checked_at, fingerprint, ""))
    alert_key = f"slack-health:{sha256(identity.encode()).hexdigest()}:alert"
    intent = {
        "schema_version": 1,
        "kind": "runner_reclamation_projection_canary_intent",
        "fixture_id": fixture_id,
        "system_channel_id": system_channel,
        "issue_channel_id": issue_channel,
        "checked_at": checked_at,
        "recovered_at": recovered_at,
        "plan_sha256": plan_sha,
        "alert_fingerprint": fingerprint,
        "alert_delivery_key": alert_key,
        "recovery_delivery_key": f"{alert_key}:recovery",
        "authorizes_apply": False,
    }
    if existing_intent is not None or intent_path.is_symlink():
        if existing_intent != intent:
            raise ReclamationCanaryError("existing canary intent conflicts with request")
    else:
        _write_json(intent_path, intent)

    database = root / "state.db"
    with StateStore(database) as store:
        store.migrate()
        coordinator = HealthAlertDeliveryCoordinator(
            store=store,
            publisher=publisher,
            channel_id=system_channel,
        )
        alert_delivery = store.get_health_alert_delivery(alert_key)
        recovery_delivery = store.get_health_alert_delivery(f"{alert_key}:recovery")
        if alert_delivery is None or alert_delivery.message_ts is None:
            coordinator.reconcile(alerts, checked_at=checked_at, alerts_truncated=False)
        if recovery_delivery is None or recovery_delivery.message_ts is None:
            coordinator.reconcile((), checked_at=recovered_at, alerts_truncated=False)
        alert_delivery = store.get_health_alert_delivery(alert_key)
        recovery_delivery = store.get_health_alert_delivery(f"{alert_key}:recovery")
    if (
        alert_delivery is None
        or recovery_delivery is None
        or alert_delivery.message_ts is None
        or alert_delivery.permalink is None
        or recovery_delivery.message_ts is None
        or recovery_delivery.permalink is None
        or alert_delivery.channel_id != system_channel
        or recovery_delivery.channel_id != system_channel
        or recovery_delivery.thread_ts != alert_delivery.message_ts
    ):
        raise ReclamationCanaryError("canary Slack projection lacks exact receipts")

    receipt: dict[str, object] = {
        "schema_version": 1,
        "kind": "runner_reclamation_projection_canary",
        "status": "passed",
        "fixture_id": fixture_id,
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "system_channel_id": system_channel,
        "issue_channel_id": issue_channel,
        "issue_channel_writes": 0,
        "external_writes": external_writes,
        "online_state_modified": False,
        "authorizes_apply": False,
        "asset_deletions": 0,
        "plan_sha256": plan_sha,
        "threshold_cases": trigger["threshold_cases"],
        "alert": {
            "delivery_key": alert_delivery.delivery_key,
            "message_ts": alert_delivery.message_ts,
            "permalink": alert_delivery.permalink,
        },
        "recovery": {
            "delivery_key": recovery_delivery.delivery_key,
            "message_ts": recovery_delivery.message_ts,
            "thread_ts": recovery_delivery.thread_ts,
            "permalink": recovery_delivery.permalink,
        },
    }
    receipt["evidence_sha256"] = _canonical_sha256(receipt)
    _write_json(receipt_path, receipt)
    return receipt


class _FixturePublisher:
    def __init__(self) -> None:
        self.messages: list[SlackOutboundMessage] = []

    def publish(self, report: SlackOutboundMessage) -> SlackDeliveryReceipt:
        self.messages.append(report)
        message_ts = f"170000000{len(self.messages)}.000001"
        thread_ts = report.thread_ts or message_ts
        query = (
            ""
            if report.thread_ts is None
            else f"?thread_ts={thread_ts}&cid={report.channel_id}"
        )
        return SlackDeliveryReceipt(
            report.deduplication_key,
            report.channel_id,
            message_ts,
            thread_ts,
            f"https://fixture.slack.com/archives/{report.channel_id}/"
            f"p{message_ts.replace('.', '')}{query}",
        )


def run_local_reclamation_canary(
    *, fixture_id: str, system_channel_id: str, issue_channel_id: str
) -> dict[str, object]:
    publisher = _FixturePublisher()
    with tempfile.TemporaryDirectory() as raw:
        receipt = run_reclamation_projection_canary(
            fixture_id=fixture_id,
            system_channel_id=system_channel_id,
            issue_channel_id=issue_channel_id,
            publisher=publisher,
            receipt_root=Path(raw) / "receipts",
            external_writes=False,
            now=datetime(2026, 8, 24, tzinfo=timezone.utc),
        )
    if len(publisher.messages) != 2 or any(
        message.channel_id != system_channel_id for message in publisher.messages
    ):
        raise ReclamationCanaryError("isolated projection did not use only the system channel")
    return receipt


def _prepare_root(parent: Path, root: Path) -> None:
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent.chmod(0o700)
    metadata = parent.stat(follow_symlinks=False)
    if parent.is_symlink() or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise ReclamationCanaryError("canary receipt root is unsafe")
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = root.stat(follow_symlinks=False)
    if root.is_symlink() or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise ReclamationCanaryError("canary fixture root is unsafe")


def _write_json(path: Path, payload: dict[str, object]) -> None:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(raw + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict[str, object]:
    try:
        metadata = path.stat(follow_symlinks=False)
        raw = path.read_bytes()
    except OSError as exc:
        raise ReclamationCanaryError("canary receipt is unavailable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or not 0 < len(raw) <= 512 * 1024
    ):
        raise ReclamationCanaryError("canary receipt is unsafe")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReclamationCanaryError("canary receipt is malformed") from exc
    if not isinstance(payload, dict):
        raise ReclamationCanaryError("canary receipt must be an object")
    return payload


def _validate_projection_receipt(
    payload: dict[str, object],
    fixture_id: str,
    system_channel: str,
    issue_channel: str,
    *,
    external_writes: bool,
) -> None:
    evidence = payload.get("evidence_sha256")
    body = dict(payload)
    body.pop("evidence_sha256", None)
    if (
        payload.get("schema_version") != 1
        or payload.get("kind") != "runner_reclamation_projection_canary"
        or payload.get("status") != "passed"
        or payload.get("fixture_id") != fixture_id
        or payload.get("system_channel_id") != system_channel
        or payload.get("issue_channel_id") != issue_channel
        or payload.get("issue_channel_writes") != 0
        or payload.get("external_writes") is not external_writes
        or payload.get("online_state_modified") is not False
        or payload.get("authorizes_apply") is not False
        or payload.get("asset_deletions") != 0
        or not isinstance(evidence, str)
        or evidence != _canonical_sha256(body)
    ):
        raise ReclamationCanaryError("canary receipt is invalid")


def _canonical_sha256(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return sha256(raw.encode()).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _slack_token() -> str | None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if (
        token is not None
        and token.startswith("xoxb-")
        and 16 <= len(token) <= 512
        and not any(character.isspace() or ord(character) < 32 for character in token)
    ):
        return token
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reclamation-threshold-canary")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--fixture-id", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        from codex_dispatcher.ssh_runtime import load_protected_ssh_config

        config = load_protected_ssh_config(args.config)
        if config.slack_runtime is None:
            raise ReclamationCanaryError("Slack runtime is required")
        slack = config.slack_runtime
        if args.plan:
            payload = run_local_reclamation_canary(
                fixture_id=args.fixture_id,
                system_channel_id=slack.system_channel_id,
                issue_channel_id=slack.issue_channel_id,
            )
        else:
            if os.environ.get("CODEX_DISPATCHER_ENABLE_SLACK_FIXTURE_WRITES") != "1":
                raise ReclamationCanaryError(
                    "CODEX_DISPATCHER_ENABLE_SLACK_FIXTURE_WRITES=1 is required"
                )
            token = _slack_token()
            if token is None:
                raise ReclamationCanaryError("a recognized Slack bot token is required")
            from codex_dispatcher.slack_web_api import SlackWebApiPublisher

            payload = run_reclamation_projection_canary(
                fixture_id=args.fixture_id,
                system_channel_id=slack.system_channel_id,
                issue_channel_id=slack.issue_channel_id,
                publisher=SlackWebApiPublisher(
                    bot_token=token,
                    timeout_seconds=slack.request_timeout_seconds,
                ),
                receipt_root=CANARY_ROOT,
                external_writes=True,
            )
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, **payload}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
