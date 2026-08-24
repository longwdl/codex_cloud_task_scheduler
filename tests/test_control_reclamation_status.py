from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from codex_dispatcher.control_reclamation_status import (
    ControlReclamationStatus,
    ControlReclamationStatusError,
    build_control_reclamation_status,
    load_control_reclamation_status,
)


COMMIT = "a" * 40
NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)


class ControlReclamationStatusTests(unittest.TestCase):
    def test_builds_exact_triggers_and_round_trips(self) -> None:
        status = build_control_reclamation_status(
            current_release_commit=COMMIT,
            host_available_bytes=7 * 1024**3,
            release_count=5,
            recovery_root_count=5,
            target_count=9,
            bundle_target_count=1,
            unconfirmed_bundle_count=1,
            expected_total_bytes=2 * 1024**3,
            plan_sha256="b" * 64,
            now=NOW,
        )

        self.assertEqual(
            (
                "host_available_below_threshold",
                "offhost_confirmation_missing",
                "reclaimable_bytes_above_threshold",
                "recovery_root_count_above_limit",
                "release_count_above_limit",
                "verified_bundle_targets_present",
            ),
            status.trigger_reasons,
        )
        self.assertEqual(status, ControlReclamationStatus.from_mapping(status.to_mapping()))

    def test_load_requires_protected_root_owned_file(self) -> None:
        status = build_control_reclamation_status(
            current_release_commit=COMMIT,
            host_available_bytes=9 * 1024**3,
            release_count=2,
            recovery_root_count=1,
            target_count=0,
            bundle_target_count=0,
            unconfirmed_bundle_count=0,
            expected_total_bytes=0,
            plan_sha256="c" * 64,
            now=NOW,
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "latest.json"
            path.write_text(json.dumps(status.to_mapping()) + "\n", encoding="utf-8")
            path.chmod(0o640)

            self.assertEqual(
                status,
                load_control_reclamation_status(path, trusted_owner_uid=os.geteuid()),
            )
            path.chmod(0o660)
            with self.assertRaises(ControlReclamationStatusError):
                load_control_reclamation_status(path, trusted_owner_uid=os.geteuid())


if __name__ == "__main__":
    unittest.main()
