"""Read-only lifecycle and systemd health evidence for unattended operation."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from codex_dispatcher.config import Config
from codex_dispatcher.state_store import StateStore
from codex_dispatcher.work_item_lifecycle import WorkItemArchiveStatus
from codex_dispatcher.work_items import WorkItem, WorkItemState


MAX_BLOCKED_AGE_SECONDS = 24 * 60 * 60
MAX_ARCHIVE_PENDING_AGE_SECONDS = 15 * 60
ACTIVE_TURN_GRACE_SECONDS = 5 * 60
MAX_REPORTED_ALERTS = 50

SYSTEMD_TIMER_UNITS = (
    "codex-dispatcher.timer",
    "codex-dispatcher-backup.timer",
    "codex-dispatcher-health.timer",
)
SYSTEMD_SERVICE_UNITS = (
    "codex-dispatcher.service",
    "codex-dispatcher-backup.service",
)


@dataclass(frozen=True, slots=True)
class LifecycleAlert:
    code: str
    work_item_id: str | None = None
    repository: str | None = None
    issue_number: int | None = None
    age_seconds: int | None = None
    unit: str | None = None

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {"code": self.code}
        for name in (
            "work_item_id",
            "repository",
            "issue_number",
            "age_seconds",
            "unit",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload


@dataclass(frozen=True, slots=True)
class LifecycleHealthSnapshot:
    checked_at: str
    integrity: str
    foreign_key_violations: int
    work_items_total: int
    active_turns: int
    blocked_work_items: int
    pending_archives: int
    ambiguous_archives: int
    blocked_archives: int
    archived_work_items: int
    absence_reconciliations: int
    alerts: tuple[LifecycleAlert, ...]
    alerts_truncated: bool

    @property
    def ok(self) -> bool:
        return (
            self.integrity == "ok"
            and self.foreign_key_violations == 0
            and not self.alerts
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "lifecycle_health": True,
            "checked_at": self.checked_at,
            "integrity": self.integrity,
            "foreign_key_violations": self.foreign_key_violations,
            "work_items_total": self.work_items_total,
            "active_turns": self.active_turns,
            "blocked_work_items": self.blocked_work_items,
            "pending_archives": self.pending_archives,
            "ambiguous_archives": self.ambiguous_archives,
            "blocked_archives": self.blocked_archives,
            "archived_work_items": self.archived_work_items,
            "absence_reconciliations": self.absence_reconciliations,
            "alert_count": len(self.alerts),
            "alerts_truncated": self.alerts_truncated,
            "alerts": [alert.to_mapping() for alert in self.alerts],
        }


@dataclass(frozen=True, slots=True)
class SystemdUnitState:
    unit: str
    load_state: str
    active_state: str
    unit_file_state: str
    result: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "unit": self.unit,
            "load_state": self.load_state,
            "active_state": self.active_state,
            "unit_file_state": self.unit_file_state,
            "result": self.result,
        }


SystemdReader = Callable[[str], SystemdUnitState]


def inspect_lifecycle_health(
    config: Config,
    store: StateStore,
    *,
    now: datetime | None = None,
) -> LifecycleHealthSnapshot:
    """Return bounded health evidence without mutating SQLite or remote state."""
    if config.ssh_runtime is None:
        raise ValueError("lifecycle health requires ssh_runtime")
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("health check timestamp must be timezone-aware")
    moment = moment.astimezone(timezone.utc)

    integrity = store.integrity_check()
    foreign_key_violations = store.foreign_key_violation_count()
    work_items = store.list_work_items()
    active_turn = store.get_active_turn()
    dispositions = {
        disposition.work_item_id: disposition
        for disposition in store.list_work_item_dispositions()
    }

    alerts: list[LifecycleAlert] = []
    blocked_work_items = 0
    pending_archives = 0
    ambiguous_archives = 0
    blocked_archives = 0
    archived_work_items = 0
    absence_reconciliations = 0

    if integrity != "ok":
        alerts.append(LifecycleAlert("database_integrity_failed"))
    if foreign_key_violations:
        alerts.append(LifecycleAlert("database_foreign_key_violation"))

    for work_item in work_items:
        archive = store.get_work_item_archive(work_item.work_item_id)
        absence = store.get_work_item_absence_reconciliation(work_item.work_item_id)
        disposition = dispositions.get(work_item.work_item_id)
        terminal_runner_evidence = (
            absence is not None
            or (
                archive is not None
                and archive.status is WorkItemArchiveStatus.ARCHIVED
            )
        )

        if absence is not None:
            absence_reconciliations += 1
        if archive is not None:
            if archive.status is WorkItemArchiveStatus.ARCHIVED:
                archived_work_items += 1
            elif absence is None:
                pending_archives += 1
                archive_age = _age_seconds(moment, archive.updated_at)
                if archive.status is WorkItemArchiveStatus.AMBIGUOUS:
                    ambiguous_archives += 1
                    alerts.append(_item_alert("archive_ambiguous", work_item, archive_age))
                elif archive.status is WorkItemArchiveStatus.BLOCKED:
                    blocked_archives += 1
                    alerts.append(_item_alert("archive_blocked", work_item, archive_age))
                elif archive_age > MAX_ARCHIVE_PENDING_AGE_SECONDS:
                    alerts.append(
                        _item_alert("archive_pending_too_long", work_item, archive_age)
                    )

        if work_item.state is WorkItemState.BLOCKED and disposition is None:
            blocked_work_items += 1
            blocked_age = _age_seconds(moment, work_item.updated_at)
            if blocked_age > MAX_BLOCKED_AGE_SECONDS:
                alerts.append(_item_alert("work_item_blocked_too_long", work_item, blocked_age))

        if terminal_runner_evidence:
            continue
        if disposition is not None and archive is None:
            if moment >= _timestamp(disposition.eligible_at):
                alerts.append(_item_alert("disposition_archive_overdue", work_item))
            continue
        retention = config.ssh_runtime.completed_retention_seconds
        if (
            work_item.state is WorkItemState.COMPLETED
            and disposition is None
            and archive is None
            and retention is not None
        ):
            completed_at = _timestamp(store.get_work_item_completed_at(work_item.work_item_id))
            eligible_at = completed_at + timedelta(seconds=retention)
            if moment >= eligible_at:
                alerts.append(
                    _item_alert(
                        "completed_archive_overdue",
                        work_item,
                        int((moment - eligible_at).total_seconds()),
                    )
                )

    if active_turn is not None:
        active_age = _age_seconds(moment, active_turn.updated_at)
        maximum = (
            config.ssh_runtime.operation_timeout_seconds
            + ACTIVE_TURN_GRACE_SECONDS
        )
        if active_age > maximum:
            work_item = store.get_work_item(active_turn.work_item_id)
            if work_item is None:
                alerts.append(
                    LifecycleAlert(
                        "active_turn_work_item_missing",
                        work_item_id=active_turn.work_item_id,
                        age_seconds=active_age,
                    )
                )
            else:
                alerts.append(_item_alert("active_turn_too_long", work_item, active_age))

    alerts.sort(
        key=lambda item: (
            item.code,
            item.repository or "",
            item.issue_number or 0,
            item.work_item_id or "",
            item.unit or "",
        )
    )
    truncated = len(alerts) > MAX_REPORTED_ALERTS
    return LifecycleHealthSnapshot(
        checked_at=moment.isoformat(),
        integrity=integrity,
        foreign_key_violations=foreign_key_violations,
        work_items_total=len(work_items),
        active_turns=1 if active_turn is not None else 0,
        blocked_work_items=blocked_work_items,
        pending_archives=pending_archives,
        ambiguous_archives=ambiguous_archives,
        blocked_archives=blocked_archives,
        archived_work_items=archived_work_items,
        absence_reconciliations=absence_reconciliations,
        alerts=tuple(alerts[:MAX_REPORTED_ALERTS]),
        alerts_truncated=truncated,
    )


def inspect_systemd_health(
    *, reader: SystemdReader | None = None
) -> tuple[tuple[SystemdUnitState, ...], tuple[LifecycleAlert, ...]]:
    """Inspect a fixed system unit allowlist without invoking a shell."""
    read = _read_systemd_unit if reader is None else reader
    states: list[SystemdUnitState] = []
    alerts: list[LifecycleAlert] = []
    for unit in (*SYSTEMD_TIMER_UNITS, *SYSTEMD_SERVICE_UNITS):
        state = read(unit)
        states.append(state)
        if state.load_state != "loaded":
            alerts.append(LifecycleAlert("systemd_unit_not_loaded", unit=unit))
            continue
        if unit in SYSTEMD_TIMER_UNITS and (
            state.active_state != "active"
            or state.unit_file_state not in {"enabled", "enabled-runtime"}
        ):
            alerts.append(LifecycleAlert("systemd_timer_not_active", unit=unit))
        if unit in SYSTEMD_SERVICE_UNITS and (
            state.active_state == "failed"
            or state.result not in {"", "success"}
        ):
            alerts.append(LifecycleAlert("systemd_service_failed", unit=unit))
    return tuple(states), tuple(alerts)


def _read_systemd_unit(unit: str) -> SystemdUnitState:
    if unit not in {*SYSTEMD_TIMER_UNITS, *SYSTEMD_SERVICE_UNITS}:
        raise ValueError("systemd unit is outside the health allowlist")
    result = subprocess.run(
        (
            "/usr/bin/systemctl",
            "show",
            unit,
            "--no-pager",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=UnitFileState",
            "--property=Result",
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or len(result.stdout) > 4096 or len(result.stderr) > 4096:
        raise RuntimeError(f"systemd unit state unavailable: {unit}")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            raise RuntimeError(f"systemd returned malformed state: {unit}")
        key, value = line.split("=", 1)
        if key in values:
            raise RuntimeError(f"systemd returned duplicate state: {unit}")
        values[key] = value
    expected = {"LoadState", "ActiveState", "UnitFileState", "Result"}
    if set(values) != expected:
        raise RuntimeError(f"systemd returned incomplete state: {unit}")
    return SystemdUnitState(
        unit=unit,
        load_state=values["LoadState"],
        active_state=values["ActiveState"],
        unit_file_state=values["UnitFileState"],
        result=values["Result"],
    )


def _item_alert(
    code: str, work_item: WorkItem, age_seconds: int | None = None
) -> LifecycleAlert:
    return LifecycleAlert(
        code,
        work_item_id=work_item.work_item_id,
        repository=work_item.repository,
        issue_number=work_item.issue_number,
        age_seconds=age_seconds,
    )


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("persisted health timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted health timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _age_seconds(now: datetime, value: str) -> int:
    return max(0, int((now - _timestamp(value)).total_seconds()))
