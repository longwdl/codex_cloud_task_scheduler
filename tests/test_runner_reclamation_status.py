from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from codex_dispatcher.runner_reclamation_status import (
    MAXIMUM_RELEASE_COUNT,
    MINIMUM_AVAILABLE_BYTES,
    MINIMUM_RECLAIMABLE_BYTES,
    RunnerReclamationStatus,
    RunnerReclamationStatusError,
    build_runner_reclamation_status,
    read_runner_reclamation_status,
)


class RunnerReclamationStatusTests(unittest.TestCase):
    def test_fixed_thresholds_generate_exact_plan_reasons(self) -> None:
        status = build_runner_reclamation_status(
            host_available_bytes=MINIMUM_AVAILABLE_BYTES - 1,
            release_count=MAXIMUM_RELEASE_COUNT + 1,
            release_target_count=3,
            image_target_count=1,
            expected_total_bytes=MINIMUM_RECLAIMABLE_BYTES,
            plan_sha256="a" * 64,
            now=datetime(2026, 8, 24, tzinfo=timezone.utc),
        )
        self.assertEqual(
            (
                "host_available_below_threshold",
                "reclaimable_bytes_above_threshold",
                "release_count_above_limit",
                "unreferenced_images_present",
            ),
            status.trigger_reasons,
        )
        self.assertEqual("a" * 64, status.plan_sha256)
        self.assertEqual(status, RunnerReclamationStatus.from_mapping(status.to_mapping()))

    def test_no_threshold_writes_no_plan_identity(self) -> None:
        status = build_runner_reclamation_status(
            host_available_bytes=MINIMUM_AVAILABLE_BYTES,
            release_count=MAXIMUM_RELEASE_COUNT,
            release_target_count=1,
            image_target_count=0,
            expected_total_bytes=MINIMUM_RECLAIMABLE_BYTES - 1,
            plan_sha256="b" * 64,
            now=datetime(2026, 8, 24, tzinfo=timezone.utc),
        )
        self.assertEqual((), status.trigger_reasons)
        self.assertIsNone(status.plan_sha256)

    def test_reader_rejects_group_writable_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "status.json"
            status = build_runner_reclamation_status(
                host_available_bytes=MINIMUM_AVAILABLE_BYTES,
                release_count=2,
                release_target_count=0,
                image_target_count=0,
                expected_total_bytes=0,
                plan_sha256="c" * 64,
            )
            path.write_text(json.dumps(status.to_mapping()) + "\n")
            path.chmod(0o660)
            with self.assertRaisesRegex(RunnerReclamationStatusError, "unsafe"):
                read_runner_reclamation_status(path, trusted_owner_uid=os.geteuid())

    def test_status_rejects_plan_without_trigger(self) -> None:
        with self.assertRaisesRegex(
            RunnerReclamationStatusError, "cannot publish a plan"
        ):
            RunnerReclamationStatus(
                checked_at="2026-08-24T00:00:00Z",
                host_available_bytes=MINIMUM_AVAILABLE_BYTES,
                release_count=2,
                release_target_count=0,
                image_target_count=0,
                expected_total_bytes=0,
                trigger_reasons=(),
                plan_sha256="d" * 64,
            )


if __name__ == "__main__":
    unittest.main()
