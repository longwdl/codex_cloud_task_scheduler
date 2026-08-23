from __future__ import annotations

import unittest

from codex_dispatcher.terminal_storage import (
    TerminalStorageEffectiveState,
    effective_terminal_storage_state,
)
from codex_dispatcher.work_item_lifecycle import (
    WorkItemAbsenceReconciliation,
    WorkItemArchive,
    WorkItemArchiveStatus,
)


WORK_ITEM_ID = "wi_" + "1" * 24
HEAD = "b" * 40
AT = "2026-08-23T00:00:00+00:00"


def _archive(status: WorkItemArchiveStatus) -> WorkItemArchive:
    archived = status is WorkItemArchiveStatus.ARCHIVED
    return WorkItemArchive(
        work_item_id=WORK_ITEM_ID,
        status=status,
        expected_head_sha=HEAD,
        eligible_at=AT,
        request_sha256="c" * 64,
        response_json="{}" if archived else None,
        response_sha256="d" * 64 if archived else None,
        reclaimed_bytes=1 if archived else None,
        runner_archived_at=AT if archived else None,
        error_code="archive_blocked" if status is WorkItemArchiveStatus.BLOCKED else None,
        created_at=AT,
        updated_at=AT,
    )


def _absence(
    *, work_item_id: str = WORK_ITEM_ID, head: str = HEAD
) -> WorkItemAbsenceReconciliation:
    return WorkItemAbsenceReconciliation(
        work_item_id=work_item_id,
        expected_head_sha=head,
        evidence_sha256="e" * 64,
        observed_by="operator",
        observed_at=AT,
        created_at=AT,
    )


class TerminalStorageEffectiveStateTests(unittest.TestCase):
    def test_raw_archive_states_are_projected_without_mutation(self) -> None:
        self.assertIs(
            TerminalStorageEffectiveState.RETAINED,
            effective_terminal_storage_state(None, None),
        )
        expected = {
            WorkItemArchiveStatus.PREPARED: TerminalStorageEffectiveState.ARCHIVE_PREPARED,
            WorkItemArchiveStatus.AMBIGUOUS: TerminalStorageEffectiveState.ARCHIVE_AMBIGUOUS,
            WorkItemArchiveStatus.BLOCKED: TerminalStorageEffectiveState.ARCHIVE_BLOCKED,
            WorkItemArchiveStatus.ARCHIVED: TerminalStorageEffectiveState.ARCHIVED,
        }
        for raw, effective in expected.items():
            with self.subTest(raw=raw.value):
                self.assertIs(
                    effective,
                    effective_terminal_storage_state(_archive(raw), None),
                )

    def test_permanent_absence_proof_overrides_unfinished_archive_state(self) -> None:
        for raw in (
            WorkItemArchiveStatus.PREPARED,
            WorkItemArchiveStatus.AMBIGUOUS,
            WorkItemArchiveStatus.BLOCKED,
        ):
            with self.subTest(raw=raw.value):
                self.assertIs(
                    TerminalStorageEffectiveState.ABSENCE_RECONCILED,
                    effective_terminal_storage_state(_archive(raw), _absence()),
                )

    def test_contradictory_absence_evidence_fails_closed(self) -> None:
        cases = (
            (None, _absence()),
            (
                _archive(WorkItemArchiveStatus.PREPARED),
                _absence(work_item_id="wi_" + "2" * 24),
            ),
            (_archive(WorkItemArchiveStatus.PREPARED), _absence(head="f" * 40)),
            (_archive(WorkItemArchiveStatus.ARCHIVED), _absence()),
        )
        for archive, absence in cases:
            with self.subTest(archive=archive, absence=absence):
                self.assertIs(
                    TerminalStorageEffectiveState.EVIDENCE_CONFLICT,
                    effective_terminal_storage_state(archive, absence),
                )


if __name__ == "__main__":
    unittest.main()
