from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from codex_dispatcher.control_host_reclamation import (
    ControlHostReclamationError,
    ControlHostReclamationPlan,
    apply_control_host_reclamation,
    confirm_offhost_bundle,
    plan_control_host_reclamation,
)


CURRENT = "c" * 40
ROLLBACK = "b" * 40
OLD = "a" * 40


class ControlHostReclamationTests(unittest.TestCase):
    def _release_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        releases = root / "releases"
        releases.mkdir(mode=0o700)
        for commit in (OLD, ROLLBACK, CURRENT):
            release = releases / commit
            release.mkdir(mode=0o700)
            (release / "release.txt").write_text(commit, encoding="utf-8")
        current = root / "current"
        current.symlink_to(f"releases/{CURRENT}")
        receipt = root / "release-receipt.json"
        receipt.write_text(
            json.dumps(
                {
                    "kind": "codex_dispatcher_release",
                    "operation": "apply",
                    "status": "committed",
                    "committed": True,
                    "release_commit": CURRENT,
                    "previous_control": f"/opt/codex-dispatcher/releases/{ROLLBACK}",
                }
            ),
            encoding="utf-8",
        )
        return releases, current, receipt

    def test_plan_protects_current_rollback_and_their_latest_dr(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            releases, current, receipt = self._release_fixture(root)
            dr = root / "dr"
            latest = dr / "20260824T100000Z-current"
            latest.mkdir(mode=0o700, parents=True)
            (latest / "receipt.json").write_text(
                json.dumps(
                    {
                        "kind": "schema18_disaster_recovery_drill",
                        "status": "passed",
                        "release_commit": CURRENT,
                        "online_state_modified": False,
                    }
                ),
                encoding="utf-8",
            )
            rollback_latest = dr / "20260824T090000Z-rollback"
            rollback_latest.mkdir(mode=0o700)
            (rollback_latest / "receipt.json").write_text(
                json.dumps(
                    {
                        "kind": "schema18_disaster_recovery_drill",
                        "status": "passed",
                        "release_commit": ROLLBACK,
                        "online_state_modified": False,
                    }
                ),
                encoding="utf-8",
            )
            failed = dr / "20260823T100000Z-old"
            failed.mkdir(mode=0o700)
            (failed / "failed-receipt.json").write_text("{}", encoding="utf-8")
            (failed / "current").symlink_to(f"releases/{OLD}")
            inputs = root / "inputs"
            inputs.mkdir(mode=0o700)
            (inputs / f"runner-{CURRENT}.json").write_text("{}", encoding="utf-8")
            (inputs / f"runner-{ROLLBACK}.json").write_text("{}", encoding="utf-8")
            (inputs / f"runner-{OLD}.json").write_text("{}", encoding="utf-8")

            plan = plan_control_host_reclamation(
                releases_root=releases,
                current_link=current,
                current_release_receipt=receipt,
                disaster_recovery_roots=dr,
                disaster_recovery_inputs=inputs,
            )

            targets = {(target.kind, target.identity) for target in plan.targets}
            self.assertIn(("release_tree", OLD), targets)
            self.assertIn(("disaster_recovery_root", failed.name), targets)
            self.assertIn(("disaster_recovery_input", f"runner-{OLD}.json"), targets)
            self.assertNotIn(("release_tree", CURRENT), targets)
            self.assertNotIn(("release_tree", ROLLBACK), targets)
            self.assertIn(str(latest), plan.protected_paths)
            self.assertIn(str(rollback_latest), plan.protected_paths)
            self.assertIn(
                str(inputs / f"runner-{ROLLBACK}.json"), plan.protected_paths
            )
            self.assertGreater(plan.expected_total_bytes, 0)
            self.assertFalse(plan.to_mapping()["authorizes_apply"])

            (failed / "current").unlink()
            (failed / "current").symlink_to("../../etc")
            with self.assertRaisesRegex(
                ControlHostReclamationError, "unsafe symlink"
            ):
                plan_control_host_reclamation(
                    releases_root=releases,
                    current_link=current,
                    current_release_receipt=receipt,
                    disaster_recovery_roots=dr,
                    disaster_recovery_inputs=inputs,
                )

    def test_plan_targets_only_confirmed_old_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            releases, current, receipt = self._release_fixture(root)
            dr = root / "dr"
            latest = dr / "latest"
            latest.mkdir(mode=0o700, parents=True)
            inputs = root / "inputs"
            inputs.mkdir(mode=0o700)
            bundles = root / "bundles"
            confirmations = root / "confirmations"
            bundles.mkdir(mode=0o700)
            confirmations.mkdir(mode=0o700)

            manifests: dict[str, str] = {}
            for bundle_id, commit in (
                ("current-reimported", CURRENT),
                ("old-confirmed", OLD),
                ("old-unconfirmed", "d" * 40),
            ):
                bundle = bundles / bundle_id
                bundle.mkdir(mode=0o700)
                (bundle / "state.db").write_bytes(b"sqlite")
                manifest = bundle / "manifest.json"
                manifest.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "codex_dispatcher_disaster_recovery_bundle",
                            "release_commit": commit,
                        }
                    ),
                    encoding="utf-8",
                )
                manifests[bundle_id] = sha256(manifest.read_bytes()).hexdigest()
            (latest / "receipt.json").write_text(
                json.dumps(
                    {
                        "kind": "schema18_disaster_recovery_drill",
                        "status": "passed",
                        "release_commit": CURRENT,
                        "online_state_modified": False,
                        "source_backup": str(bundles / "current-reimported" / "state.db"),
                    }
                ),
                encoding="utf-8",
            )
            confirmed = manifests["old-confirmed"]
            (confirmations / f"{confirmed}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "control_bundle_offhost_confirmation",
                        "status": "confirmed",
                        "bundle_id": "old-confirmed",
                        "manifest_sha256": confirmed,
                        "off_host_copy_id": "fixture/offhost/old-confirmed",
                        "confirmed_at": "2026-08-24T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )

            plan = plan_control_host_reclamation(
                releases_root=releases,
                current_link=current,
                current_release_receipt=receipt,
                disaster_recovery_roots=dr,
                disaster_recovery_inputs=inputs,
                disaster_recovery_bundles=bundles,
                bundle_confirmation_root=confirmations,
            )

            bundle_targets = {
                target.identity
                for target in plan.targets
                if target.kind == "disaster_recovery_bundle"
            }
            self.assertEqual({"old-confirmed"}, bundle_targets)
            self.assertIn(str(bundles / "current-reimported"), plan.protected_paths)
            self.assertIn(
                str(bundles / "old-unconfirmed"), plan.unconfirmed_bundle_paths
            )
            self.assertEqual(plan, ControlHostReclamationPlan.from_mapping(plan.to_mapping()))

    def test_apply_rechecks_and_deletes_only_exact_direct_children(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            releases, current, receipt = self._release_fixture(root)
            dr = root / "dr"
            inputs = root / "inputs"
            dr.mkdir(mode=0o700)
            inputs.mkdir(mode=0o700)
            plan = plan_control_host_reclamation(
                releases_root=releases,
                current_link=current,
                current_release_receipt=receipt,
                disaster_recovery_roots=dr,
                disaster_recovery_inputs=inputs,
            )
            receipts = root / "receipts"
            receipts.mkdir(mode=0o700)

            result = apply_control_host_reclamation(
                approved_plan=plan,
                observed_plan=plan,
                receipt_directory=receipts,
                allowed_roots={"release_tree": releases},
                now=datetime(2026, 8, 24, tzinfo=timezone.utc),
                trusted_receipt_owner_uid=os.geteuid(),
            )

            self.assertEqual("reclaimed", result["status"])
            self.assertFalse((releases / OLD).exists())
            self.assertTrue((releases / CURRENT).is_dir())
            self.assertTrue((releases / ROLLBACK).is_dir())
            self.assertEqual([str(releases / OLD)], result["completed_paths"])
            receipt_path = receipts / f"{plan.plan_sha256}.json"
            self.assertEqual(0o600, receipt_path.stat().st_mode & 0o777)

    def test_apply_fails_before_delete_when_inventory_drifted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            releases, current, receipt = self._release_fixture(root)
            dr = root / "dr"
            inputs = root / "inputs"
            dr.mkdir(mode=0o700)
            inputs.mkdir(mode=0o700)
            approved = plan_control_host_reclamation(
                releases_root=releases,
                current_link=current,
                current_release_receipt=receipt,
                disaster_recovery_roots=dr,
                disaster_recovery_inputs=inputs,
            )
            (releases / OLD / "release.txt").write_text("changed", encoding="utf-8")
            observed = plan_control_host_reclamation(
                releases_root=releases,
                current_link=current,
                current_release_receipt=receipt,
                disaster_recovery_roots=dr,
                disaster_recovery_inputs=inputs,
            )
            receipts = root / "receipts"
            receipts.mkdir(mode=0o700)

            with self.assertRaisesRegex(ControlHostReclamationError, "changed"):
                apply_control_host_reclamation(
                    approved_plan=approved,
                    observed_plan=observed,
                    receipt_directory=receipts,
                    allowed_roots={"release_tree": releases},
                    trusted_receipt_owner_uid=os.geteuid(),
                )
            self.assertTrue((releases / OLD).is_dir())

    def test_offhost_confirmation_is_integral_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bundles = root / "bundles"
            confirmations = root / "confirmations"
            bundle = bundles / "bundle-1"
            bundle.mkdir(mode=0o700, parents=True)
            confirmations.mkdir(mode=0o700)
            manifest = bundle / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "codex_dispatcher_disaster_recovery_bundle",
                        "release_commit": CURRENT,
                    }
                ),
                encoding="utf-8",
            )
            digest = sha256(manifest.read_bytes()).hexdigest()
            loaded = SimpleNamespace(manifest_sha256=digest)

            with patch(
                "codex_dispatcher.disaster_recovery.load_disaster_recovery_bundle",
                return_value=loaded,
            ):
                first = confirm_offhost_bundle(
                    bundle_id="bundle-1",
                    manifest_sha256=digest,
                    off_host_copy_id="fixture/offhost/bundle-1",
                    bundle_root=bundles,
                    confirmation_root=confirmations,
                    now=datetime(2026, 8, 24, tzinfo=timezone.utc),
                    trusted_confirmation_owner_uid=os.geteuid(),
                )
                second = confirm_offhost_bundle(
                    bundle_id="bundle-1",
                    manifest_sha256=digest,
                    off_host_copy_id="fixture/offhost/bundle-1",
                    bundle_root=bundles,
                    confirmation_root=confirmations,
                    now=datetime(2026, 8, 25, tzinfo=timezone.utc),
                    trusted_confirmation_owner_uid=os.geteuid(),
                )

            self.assertEqual(1, first["state_writes"])
            self.assertEqual(0, second["state_writes"])
            self.assertEqual(first["confirmed_at"], second["confirmed_at"])
            path = Path(str(first["receipt_path"]))
            self.assertEqual(os.geteuid(), path.stat().st_uid)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
