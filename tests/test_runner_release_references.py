from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from codex_dispatcher.runner_release_references import (
    RunnerReleaseReferenceError,
    apply_release_references,
    commit_prepared_release_references,
    prepare_release_references,
    rollback_release_references,
)


CURRENT = "a" * 40
PREVIOUS = "b" * 40
STALE = "c" * 40
IMAGE = "ghcr.io/example/runner@sha256:" + "d" * 64


class RunnerReleaseReferenceTests(unittest.TestCase):
    def test_prepare_receipt_precedes_ledger_commit_and_is_abortable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            paths = self._fixture(Path(raw))
            before = paths["references"].read_bytes()
            arguments = self._apply_arguments(paths)
            prepared = prepare_release_references(**arguments)
            self.assertEqual("prepared", prepared["status"])
            self.assertEqual(before, paths["references"].read_bytes())
            receipt = json.loads(
                (paths["receipts"] / f"{CURRENT}.apply.json").read_text()
            )
            self.assertEqual("prepared", receipt["status"])

            committed = commit_prepared_release_references(
                references_path=paths["references"],
                receipt_directory=paths["receipts"],
                current_link=paths["current"],
                release_commit=CURRENT,
                trusted_owner_uid=os.geteuid(),
            )
            self.assertEqual("applied", committed["status"])
            self.assertNotEqual(before, paths["references"].read_bytes())

        with tempfile.TemporaryDirectory() as raw:
            paths = self._fixture(Path(raw))
            before = paths["references"].read_bytes()
            prepare_release_references(**self._apply_arguments(paths))
            paths["current"].unlink()
            paths["current"].symlink_to(f"releases/{PREVIOUS}")
            rollback_release_references(
                references_path=paths["references"],
                receipt_directory=paths["receipts"],
                current_link=paths["current"],
                release_commit=CURRENT,
                trusted_owner_uid=os.geteuid(),
            )
            self.assertEqual(before, paths["references"].read_bytes())

    def test_apply_repairs_stale_v1_and_rollback_restores_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = self._fixture(root)
            before = paths["references"].read_bytes()
            result = apply_release_references(
                **self._apply_arguments(paths),
                now=datetime(2026, 8, 24, tzinfo=timezone.utc),
            )
            self.assertEqual("applied", result["status"])
            self.assertEqual(2, result["state_writes"])
            ledger = json.loads(paths["references"].read_text())
            self.assertEqual(2, ledger["schema_version"])
            self.assertEqual(CURRENT, ledger["current_release_commit"])
            self.assertEqual(PREVIOUS, ledger["immediate_rollback_release_commit"])
            self.assertEqual([CURRENT, PREVIOUS], ledger["protected_release_commits"])
            self.assertEqual([IMAGE], ledger["protected_image_refs"])
            receipt = json.loads(
                (paths["receipts"] / f"{CURRENT}.apply.json").read_text()
            )
            self.assertEqual("applied", receipt["status"])
            self.assertEqual(STALE, receipt["before_references"]["current_release_commit"])

            paths["current"].unlink()
            paths["current"].symlink_to(f"releases/{PREVIOUS}")
            rollback = rollback_release_references(
                references_path=paths["references"],
                receipt_directory=paths["receipts"],
                current_link=paths["current"],
                release_commit=CURRENT,
                trusted_owner_uid=os.geteuid(),
                now=datetime(2026, 8, 25, tzinfo=timezone.utc),
            )
            self.assertEqual("rolled_back", rollback["status"])
            self.assertEqual(before, paths["references"].read_bytes())
            self.assertTrue((paths["receipts"] / f"{CURRENT}.rollback.json").exists())

    def test_apply_and_rollback_are_idempotent_only_for_same_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            paths = self._fixture(Path(raw))
            arguments = self._apply_arguments(paths)
            moment = datetime(2026, 8, 24, tzinfo=timezone.utc)
            apply_release_references(**arguments, now=moment)
            replay = apply_release_references(**arguments, now=moment)
            self.assertEqual(0, replay["state_writes"])
            paths["current"].unlink()
            paths["current"].symlink_to(f"releases/{PREVIOUS}")
            rollback_args = {
                "references_path": paths["references"],
                "receipt_directory": paths["receipts"],
                "current_link": paths["current"],
                "release_commit": CURRENT,
                "trusted_owner_uid": os.geteuid(),
                "now": moment,
            }
            rollback_release_references(**rollback_args)
            rollback_args["now"] = datetime(2026, 8, 25, tzinfo=timezone.utc)
            replay = rollback_release_references(**rollback_args)
            self.assertEqual(0, replay["state_writes"])

    def test_rollback_refuses_changed_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            paths = self._fixture(Path(raw))
            apply_release_references(
                **self._apply_arguments(paths),
                now=datetime(2026, 8, 24, tzinfo=timezone.utc),
            )
            changed = json.loads(paths["references"].read_text())
            changed["protected_release_commits"].reverse()
            paths["references"].write_text(json.dumps(changed) + "\n")
            paths["current"].unlink()
            paths["current"].symlink_to(f"releases/{PREVIOUS}")
            with self.assertRaisesRegex(
                RunnerReleaseReferenceError, "changed after release"
            ):
                rollback_release_references(
                    references_path=paths["references"],
                    receipt_directory=paths["receipts"],
                    current_link=paths["current"],
                    release_commit=CURRENT,
                    trusted_owner_uid=os.geteuid(),
                )

    def test_apply_rejects_non_digest_config_image(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            paths = self._fixture(Path(raw))
            paths["config"].write_text(
                json.dumps({"docker_runtime": {"image": "example/runner:latest"}})
            )
            with self.assertRaisesRegex(
                RunnerReleaseReferenceError, "exact digest"
            ):
                apply_release_references(**self._apply_arguments(paths))

    def _fixture(self, root: Path) -> dict[str, Path]:
        receipts = root / "receipts"
        receipts.mkdir()
        references = root / "references.json"
        references.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "runner_reclamation_rollback_references",
                    "current_release_commit": STALE,
                    "protected_release_commits": [STALE, PREVIOUS],
                    "protected_image_refs": [IMAGE],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        config = root / "config.json"
        config.write_text(json.dumps({"docker_runtime": {"image": IMAGE}}) + "\n")
        current = root / "current"
        current.symlink_to(f"releases/{CURRENT}")
        return {
            "receipts": receipts,
            "references": references,
            "config": config,
            "current": current,
        }

    def _apply_arguments(self, paths: dict[str, Path]) -> dict[str, object]:
        return {
            "references_path": paths["references"],
            "receipt_directory": paths["receipts"],
            "runner_config": paths["config"],
            "current_link": paths["current"],
            "release_commit": CURRENT,
            "previous_release_commit": PREVIOUS,
            "trusted_owner_uid": os.geteuid(),
        }


if __name__ == "__main__":
    unittest.main()
