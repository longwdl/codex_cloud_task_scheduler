from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch

from codex_dispatcher.runner_asset_reclamation import (
    DockerImageAsset,
    RunnerAssetReclamationError,
    RunnerAssetSnapshot,
    _effective_uid,
    apply_runner_asset_reclamation,
    collect_runner_asset_snapshot,
    delete_exact_release,
    docker_image_assets_from_json,
    inspect_release_assets,
    plan_runner_asset_reclamation,
)


CURRENT_COMMIT = "d" * 40
ROLLBACK_COMMIT = "c" * 40
OLD_COMMIT = "a" * 40
OTHER_OLD_COMMIT = "b" * 40
CURRENT_IMAGE = "registry.invalid/runner@sha256:" + "1" * 64
OLD_IMAGE = "registry.invalid/runner@sha256:" + "2" * 64
WORK_ITEM_IMAGE = "registry.invalid/runner@sha256:" + "3" * 64
WORK_ITEM = "wi_" + "4" * 24


def image(reference: str, image_id: str, *, unique: int = 100) -> DockerImageAsset:
    return DockerImageAsset(
        image_id="sha256:" + image_id * 64,
        repo_digests=(reference,),
        repo_tags=(),
        source_commit="e" * 40,
        provenance_kind="publication_run",
        size_bytes=1000,
        unique_size_bytes=unique,
        container_count=0,
        source_label="https://github.com/longwdl/codex_cloud_task_scheduler",
        title_label="codex-cloud-task-scheduler-runner",
    )


class RunnerAssetReclamationTests(unittest.TestCase):
    def test_work_item_inspection_drops_and_restores_effective_uid(self) -> None:
        with (
            patch(
                "codex_dispatcher.runner_asset_reclamation.os.geteuid",
                return_value=0,
            ),
            patch(
                "codex_dispatcher.runner_asset_reclamation.os.seteuid"
            ) as set_effective_uid,
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture interruption"):
                with _effective_uid(1002):
                    set_effective_uid.assert_called_once_with(1002)
                    raise RuntimeError("fixture interruption")

        self.assertEqual(
            [call(1002), call(0)],
            set_effective_uid.call_args_list,
        )

        with patch(
            "codex_dispatcher.runner_asset_reclamation.os.geteuid",
            return_value=1001,
        ):
            with self.assertRaisesRegex(
                RunnerAssetReclamationError,
                "root or the trusted owner",
            ):
                with _effective_uid(1002):
                    self.fail("untrusted user entered WorkItem inspection")

    def _releases(self, root: Path) -> tuple:
        releases = root / "releases"
        releases.mkdir(mode=0o700)
        for commit in (OLD_COMMIT, OTHER_OLD_COMMIT, ROLLBACK_COMMIT, CURRENT_COMMIT):
            directory = releases / commit
            directory.mkdir(mode=0o700)
            (directory / "release.txt").write_text(commit, encoding="utf-8")
        return inspect_release_assets(releases)

    def _snapshot(self, root: Path) -> RunnerAssetSnapshot:
        return RunnerAssetSnapshot(
            current_release_commit=CURRENT_COMMIT,
            rollback_release_commits=(ROLLBACK_COMMIT,),
            configured_image=CURRENT_IMAGE,
            rollback_images=(),
            work_item_images=(WORK_ITEM_IMAGE,),
            registry_work_item_ids=(WORK_ITEM,),
            archived_work_item_ids=(WORK_ITEM,),
            absent_work_item_ids=(),
            releases=self._releases(root),
            images=(
                image(CURRENT_IMAGE, "1"),
                image(OLD_IMAGE, "2", unique=350),
                image(WORK_ITEM_IMAGE, "3"),
            ),
        )

    def test_plan_keeps_current_rollback_config_and_work_item_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = self._snapshot(Path(temp_dir))

            plan = plan_runner_asset_reclamation(snapshot)

            self.assertEqual(
                (OLD_COMMIT, OTHER_OLD_COMMIT),
                tuple(item.commit for item in plan.release_targets),
            )
            self.assertEqual(("sha256:" + "2" * 64,), tuple(
                item.image_id for item in plan.image_targets
            ))
            self.assertEqual(350, plan.expected_image_unique_bytes)
            self.assertGreater(plan.expected_release_bytes, 0)
            self.assertEqual(64, len(plan.plan_sha256))
            self.assertFalse(plan.to_mapping()["authorizes_apply"])

    def test_plan_keeps_image_referenced_by_rollback_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = replace(
                self._snapshot(Path(temp_dir)),
                rollback_images=(OLD_IMAGE,),
            )

            plan = plan_runner_asset_reclamation(snapshot)

            self.assertEqual((), plan.image_targets)
            self.assertIn(OLD_IMAGE, plan.protected_images)

    def test_apply_reinspects_writes_intent_and_replays_permanent_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot = self._snapshot(root)
            plan = plan_runner_asset_reclamation(snapshot)
            receipts = root / "receipts"
            receipts.mkdir(mode=0o700)
            releases: list[str] = []
            images: list[str] = []

            result = apply_runner_asset_reclamation(
                approved_plan=plan,
                reinspected_snapshot=snapshot,
                receipt_directory=receipts,
                delete_release=lambda item: (releases.append(item.commit), 10)[1],
                delete_image=lambda item: (images.append(item.image_id), 20)[1],
                now=datetime(2026, 8, 23, tzinfo=timezone.utc),
            )

            self.assertEqual("reclaimed", result["status"])
            self.assertEqual([OLD_COMMIT, OTHER_OLD_COMMIT], releases)
            self.assertEqual(["sha256:" + "2" * 64], images)
            self.assertEqual(40, result["reclaimed_bytes"])
            receipt = receipts / f"{plan.plan_sha256}.json"
            self.assertEqual(0, receipt.stat().st_mode & 0o077)
            replay = apply_runner_asset_reclamation(
                approved_plan=plan,
                reinspected_snapshot=snapshot,
                receipt_directory=receipts,
                delete_release=lambda item: self.fail("release replayed"),
                delete_image=lambda item: self.fail("image replayed"),
            )
            self.assertEqual(result, replay)

    def test_apply_rejects_snapshot_drift_before_any_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot = self._snapshot(root)
            plan = plan_runner_asset_reclamation(snapshot)
            drifted = replace(
                snapshot,
                rollback_release_commits=(ROLLBACK_COMMIT, OLD_COMMIT),
            )
            receipts = root / "receipts"
            receipts.mkdir(mode=0o700)
            called: list[str] = []

            with self.assertRaisesRegex(
                RunnerAssetReclamationError, "changed after plan"
            ):
                apply_runner_asset_reclamation(
                    approved_plan=plan,
                    reinspected_snapshot=drifted,
                    receipt_directory=receipts,
                    delete_release=lambda item: (called.append(item.commit), 0)[1],
                    delete_image=lambda item: (called.append(item.image_id), 0)[1],
                )
            self.assertEqual([], called)
            self.assertEqual([], list(receipts.iterdir()))

    def test_collector_treats_archived_registry_as_a_tombstone_not_image_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            releases = root / "releases"
            current_release = releases / CURRENT_COMMIT
            current_release.mkdir(mode=0o700, parents=True)
            (current_release / "release.txt").write_text("current", encoding="utf-8")
            current = root / "current"
            current.symlink_to(f"releases/{CURRENT_COMMIT}")
            work_items = root / "work-items"
            for name in (".registry", ".archives", ".absences"):
                (work_items / name).mkdir(mode=0o700, parents=True)
            registry = {
                "version": 1,
                "work_item_id": WORK_ITEM,
                "repository": "owner/repo",
                "issue_number": 1,
            }
            (work_items / ".registry" / f"{WORK_ITEM}.json").write_text(
                json.dumps(registry), encoding="utf-8"
            )
            (work_items / ".archives" / f"{WORK_ITEM}.json").write_text(
                json.dumps({"work_item_id": WORK_ITEM}), encoding="utf-8"
            )
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "execution_mode": "rootless_docker",
                        "work_items_root": str(work_items),
                        "docker_runtime": {"image": CURRENT_IMAGE},
                    }
                ),
                encoding="utf-8",
            )

            snapshot = collect_runner_asset_snapshot(
                config_path=config,
                releases_root=releases,
                current_link=current,
                rollback_release_commits=(),
                rollback_image_refs=(),
                images=(image(CURRENT_IMAGE, "1"),),
                trusted_work_items_owner_uid=work_items.stat().st_uid,
            )

            self.assertEqual((WORK_ITEM,), snapshot.registry_work_item_ids)
            self.assertEqual((WORK_ITEM,), snapshot.archived_work_item_ids)
            self.assertEqual((), snapshot.work_item_images)
            self.assertEqual((), snapshot.blocked_reasons)

            with self.assertRaisesRegex(
                RunnerAssetReclamationError, "owned and protected"
            ):
                collect_runner_asset_snapshot(
                    config_path=config,
                    releases_root=releases,
                    current_link=current,
                    rollback_release_commits=(),
                    rollback_image_refs=(),
                    images=(image(CURRENT_IMAGE, "1"),),
                    trusted_work_items_owner_uid=work_items.stat().st_uid + 1,
                )

    def test_exact_release_delete_revalidates_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            releases = self._releases(root)
            target = next(item for item in releases if item.commit == OLD_COMMIT)
            (Path(target.path) / "release.txt").write_text("changed", encoding="utf-8")

            with self.assertRaisesRegex(RunnerAssetReclamationError, "changed"):
                delete_exact_release(target)
            self.assertTrue(Path(target.path).is_dir())

    def test_docker_parser_accepts_one_unambiguous_short_system_df_id(self) -> None:
        image_id = "sha256:" + "f" * 64
        assets = docker_image_assets_from_json(
            inspections=(
                {
                    "Id": image_id,
                    "RepoDigests": [CURRENT_IMAGE],
                    "RepoTags": [],
                    "Size": 1000,
                    "Config": {
                        "Labels": {
                            "org.opencontainers.image.source": (
                                "https://github.com/longwdl/codex_cloud_task_scheduler"
                            ),
                            "org.opencontainers.image.title": (
                                "codex-cloud-task-scheduler-runner"
                            ),
                            "org.opencontainers.image.revision": "e" * 40,
                        }
                    },
                },
            ),
            disk_usage={
                "Images": [
                    {
                        "ID": "f" * 12,
                        "UniqueSize": "900B",
                        "Containers": "0",
                    }
                ]
            },
            provenance={},
        )

        self.assertEqual(900, assets[0].unique_size_bytes)

    def test_docker_parser_ignores_unaddressable_labeled_build_intermediate(
        self,
    ) -> None:
        intermediate_id = "sha256:" + "a" * 64
        assets = docker_image_assets_from_json(
            inspections=(
                {
                    "Id": intermediate_id,
                    "RepoDigests": [],
                    "RepoTags": [],
                    "Size": 1000,
                    "Config": {
                        "Labels": {
                            "org.opencontainers.image.source": (
                                "https://github.com/longwdl/codex_cloud_task_scheduler"
                            ),
                            "org.opencontainers.image.title": (
                                "codex-cloud-task-scheduler-runner"
                            ),
                        }
                    },
                },
            ),
            disk_usage={
                "Images": [
                    {
                        "ID": "a" * 12,
                        "UniqueSize": "900B",
                        "Containers": "0",
                    }
                ]
            },
            provenance={},
        )

        self.assertEqual((), assets)


if __name__ == "__main__":
    unittest.main()
