from __future__ import annotations

from contextlib import redirect_stderr
import io
import unittest
from unittest.mock import patch

from codex_dispatcher.runner_asset_reclamation import RunnerAssetReclamationError
from codex_dispatcher.runner_maintenance import _parser, _rollback_references


CURRENT = "a" * 40
PREVIOUS = "b" * 40
IMAGE = "ghcr.io/example/runner@sha256:" + "c" * 64


class RunnerMaintenanceTests(unittest.TestCase):
    def test_auto_plan_parser_has_no_apply_argument(self) -> None:
        parsed = _parser().parse_args(["reclamation-auto-plan"])
        self.assertEqual("reclamation-auto-plan", parsed.command)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            _parser().parse_args(["reclamation-auto-plan", "--apply"])

    def test_rollback_references_requires_exact_v2_release_owned_ledger(self) -> None:
        payload = {
            "schema_version": 2,
            "kind": "runner_reclamation_rollback_references",
            "current_release_commit": CURRENT,
            "immediate_rollback_release_commit": PREVIOUS,
            "current_image_ref": IMAGE,
            "protected_release_commits": [CURRENT, PREVIOUS],
            "protected_image_refs": [IMAGE],
        }
        with patch(
            "codex_dispatcher.runner_maintenance._read_json", return_value=payload
        ), patch(
            "codex_dispatcher.runner_maintenance.os.readlink",
            return_value=f"releases/{CURRENT}",
        ):
            commits, images = _rollback_references()
        self.assertEqual((PREVIOUS,), commits)
        self.assertEqual((IMAGE,), images)

        legacy = dict(payload)
        legacy["schema_version"] = 1
        with patch(
            "codex_dispatcher.runner_maintenance._read_json", return_value=legacy
        ), patch(
            "codex_dispatcher.runner_maintenance.os.readlink",
            return_value=f"releases/{CURRENT}",
        ), self.assertRaisesRegex(RunnerAssetReclamationError, "stale or invalid"):
            _rollback_references()


if __name__ == "__main__":
    unittest.main()
