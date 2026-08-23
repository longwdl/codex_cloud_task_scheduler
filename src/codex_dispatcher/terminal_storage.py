"""Derived one-value view over immutable terminal storage evidence."""

from __future__ import annotations

from enum import StrEnum

from codex_dispatcher.work_item_lifecycle import (
    WorkItemAbsenceReconciliation,
    WorkItemArchive,
    WorkItemArchiveStatus,
)


class TerminalStorageEffectiveState(StrEnum):
    RETAINED = "retained"
    ARCHIVE_PREPARED = "archive_prepared"
    ARCHIVE_AMBIGUOUS = "archive_ambiguous"
    ARCHIVE_BLOCKED = "archive_blocked"
    ARCHIVED = "archived"
    ABSENCE_RECONCILED = "absence_reconciled"
    EVIDENCE_CONFLICT = "evidence_conflict"


_ARCHIVE_STATES = {
    WorkItemArchiveStatus.PREPARED: TerminalStorageEffectiveState.ARCHIVE_PREPARED,
    WorkItemArchiveStatus.AMBIGUOUS: TerminalStorageEffectiveState.ARCHIVE_AMBIGUOUS,
    WorkItemArchiveStatus.BLOCKED: TerminalStorageEffectiveState.ARCHIVE_BLOCKED,
    WorkItemArchiveStatus.ARCHIVED: TerminalStorageEffectiveState.ARCHIVED,
}


def effective_terminal_storage_state(
    archive: WorkItemArchive | None,
    absence: WorkItemAbsenceReconciliation | None,
) -> TerminalStorageEffectiveState:
    """Return one effective state while preserving the independent raw ledgers."""
    if archive is not None and not isinstance(archive, WorkItemArchive):
        raise TypeError("archive must be a WorkItemArchive or None")
    if absence is not None and not isinstance(
        absence, WorkItemAbsenceReconciliation
    ):
        raise TypeError("absence must be a WorkItemAbsenceReconciliation or None")
    if absence is not None:
        if (
            archive is None
            or archive.work_item_id != absence.work_item_id
            or archive.expected_head_sha != absence.expected_head_sha
            or archive.status is WorkItemArchiveStatus.ARCHIVED
        ):
            return TerminalStorageEffectiveState.EVIDENCE_CONFLICT
        return TerminalStorageEffectiveState.ABSENCE_RECONCILED
    if archive is None:
        return TerminalStorageEffectiveState.RETAINED
    return _ARCHIVE_STATES[archive.status]
