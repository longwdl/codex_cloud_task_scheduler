from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_dispatcher.control_host_reclamation import plan_control_host_reclamation


CURRENT = "c" * 40
ROLLBACK = "b" * 40
OLD = "a" * 40


class ControlHostReclamationTests(unittest.TestCase):
    def test_plan_protects_current_rollback_and_latest_current_dr(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
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
            failed = dr / "20260823T100000Z-old"
            failed.mkdir(mode=0o700)
            (failed / "failed-receipt.json").write_text("{}", encoding="utf-8")
            inputs = root / "inputs"
            inputs.mkdir(mode=0o700)
            (inputs / f"runner-{CURRENT}.json").write_text("{}", encoding="utf-8")
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
            self.assertGreater(plan.expected_total_bytes, 0)
            self.assertFalse(plan.to_mapping()["authorizes_apply"])


if __name__ == "__main__":
    unittest.main()
