from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from codex_dispatcher.runner_policy import PolicyBundle, PolicyBundleError


_FILES = {
    "config.toml": (
        b'model = "gpt-5.6-sol"\nmodel_reasoning_effort = "xhigh"\n'
    ),
    "requirements.toml": b'allowed_web_search_modes = ["disabled"]\n',
    "agents/spark-worker.toml": (
        b'name = "spark_worker"\nmodel = "gpt-5.3-codex-spark"\n'
        b'model_reasoning_effort = "medium"\n'
    ),
    "agents/luna-worker.toml": (
        b'name = "luna_worker"\nmodel = "gpt-5.6-luna"\n'
        b'model_reasoning_effort = "low"\n'
    ),
    "agents/terra-worker.toml": (
        b'name = "terra_worker"\nmodel = "gpt-5.6-terra"\n'
        b'model_reasoning_effort = "medium"\n'
    ),
    "agents/sol-specialist.toml": (
        b'name = "sol_specialist"\nmodel = "gpt-5.6-sol"\n'
        b'model_reasoning_effort = "xhigh"\n'
    ),
}


def make_bundle(root: Path) -> tuple[Path, str]:
    bundle = root / "policy"
    agents = bundle / "agents"
    agents.mkdir(parents=True, mode=0o700)
    file_hashes: dict[str, str] = {}
    for relative, contents in _FILES.items():
        target = bundle / relative
        target.write_bytes(contents)
        target.chmod(0o600)
        file_hashes[relative] = sha256(contents).hexdigest()
    digest = sha256(
        json.dumps(file_hashes, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "codex_version": "0.147.0",
                "policy_digest": digest,
                "files": file_hashes,
            }
        ),
        encoding="utf-8",
    )
    (bundle / "manifest.json").chmod(0o600)
    bundle.chmod(0o700)
    agents.chmod(0o700)
    return bundle, digest


class PolicyBundleTests(unittest.TestCase):
    def test_validates_exact_hash_pinned_bundle_and_revalidates_drift(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root, digest = make_bundle(Path(temp_dir))
            bundle = PolicyBundle.load(root, digest)
            self.assertEqual(root, bundle.root)
            self.assertEqual(root / "agents", bundle.agents_path)
            runtime = bundle.runtime_policy("implementation")
            self.assertEqual("gpt-5.6-sol", runtime.primary_model)
            self.assertEqual("xhigh", runtime.primary_reasoning_effort)
            self.assertEqual(
                {"luna_worker", "sol_specialist", "spark_worker", "terra_worker"},
                set(runtime.profiles_by_name),
            )
            legacy_audit = bundle.runtime_policy("audit")
            self.assertIsNone(legacy_audit.config_profile)
            self.assertEqual("xhigh", legacy_audit.primary_reasoning_effort)

            (root / "config.toml").write_text('model = "other"\n', encoding="utf-8")
            with self.assertRaisesRegex(PolicyBundleError, "digest"):
                bundle.validate()

    def test_rejects_unknown_missing_and_symlinked_files(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root, digest = make_bundle(Path(temp_dir))
            (root / "extra.toml").write_text("x = 1\n", encoding="utf-8")
            (root / "extra.toml").chmod(0o600)
            with self.assertRaisesRegex(PolicyBundleError, "unknown"):
                PolicyBundle.load(root, digest)

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root, digest = make_bundle(Path(temp_dir))
            (root / "agents" / "luna-worker.toml").unlink()
            with self.assertRaisesRegex(PolicyBundleError, "unknown"):
                PolicyBundle.load(root, digest)

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root, digest = make_bundle(Path(temp_dir))
            target = root / "config.toml"
            replacement = root / "replacement.toml"
            target.replace(replacement)
            target.symlink_to(replacement)
            with self.assertRaisesRegex(PolicyBundleError, "unknown|trusted"):
                PolicyBundle.load(root, digest)

    def test_rejects_writable_paths_and_mismatched_digest(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root, digest = make_bundle(Path(temp_dir))
            (root / "agents").chmod(0o770)
            with self.assertRaisesRegex(PolicyBundleError, "protected"):
                PolicyBundle.load(root, digest)

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root, digest = make_bundle(Path(temp_dir))
            (root / "requirements.toml").chmod(0o622)
            with self.assertRaisesRegex(PolicyBundleError, "protected"):
                PolicyBundle.load(root, digest)

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root, _ = make_bundle(Path(temp_dir))
            with self.assertRaisesRegex(PolicyBundleError, "does not match"):
                PolicyBundle.load(root, "0" * 64)

    def test_repository_artifact_has_correct_manifest_digest(self) -> None:
        root = Path(__file__).resolve().parents[1] / "config" / "runner-codex-policy"
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        bundle = PolicyBundle.load(root, manifest["policy_digest"])
        self.assertEqual("gpt-6-sol", (root / "config.toml").read_text().splitlines()[0].split(" = ")[1].strip('"'))
        self.assertEqual("0.147.0", manifest["codex_version"])
        self.assertEqual(2, manifest["schema_version"])
        self.assertEqual(6, len(manifest["files"]))
        implementation = bundle.runtime_policy("implementation")
        repair = bundle.runtime_policy("ci_repair")
        audit = bundle.runtime_policy("audit")
        self.assertEqual(("gpt-6-sol", "medium", None), (implementation.primary_model, implementation.primary_reasoning_effort, implementation.config_profile))
        self.assertEqual(("gpt-6-sol", "high", "repair"), (repair.primary_model, repair.primary_reasoning_effort, repair.config_profile))
        self.assertEqual(("gpt-6-sol", "high", "audit"), (audit.primary_model, audit.primary_reasoning_effort, audit.config_profile))
        self.assertFalse(audit.delegation_allowed)
        self.assertEqual({"luna_worker", "sol_specialist"}, set(implementation.profiles_by_name))
        with self.assertRaisesRegex(PolicyBundleError, "role"):
            bundle.runtime_policy("salvage")
        bundle.validate()

    def test_v2_rejects_digest_valid_policy_drift(self) -> None:
        source = Path(__file__).resolve().parents[1] / "config" / "runner-codex-policy"
        cases = (
            ("repair.config.toml", 'model_reasoning_effort = "low"\n', "root model or effort"),
            ("agents/luna-worker.toml", 'model = "gpt-6-sol"\n', "agent profiles"),
            ("audit.config.toml", "enabled = true\n", "disable delegation"),
        )
        for relative_path, replacement, error in cases:
            with self.subTest(relative_path=relative_path):
                with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
                    root = Path(temp_dir) / "policy"
                    shutil.copytree(source, root)
                    target = root / relative_path
                    contents = target.read_text(encoding="utf-8")
                    if relative_path == "repair.config.toml":
                        contents = contents.replace('model_reasoning_effort = "high"\n', replacement)
                    elif relative_path == "agents/luna-worker.toml":
                        contents = contents.replace('model = "gpt-6-luna"\n', replacement)
                    else:
                        contents = contents.replace("enabled = false\n", replacement)
                    target.write_text(contents, encoding="utf-8")
                    manifest_path = root / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    hashes = {
                        path: sha256((root / path).read_bytes()).hexdigest()
                        for path in manifest["files"]
                    }
                    digest = sha256(
                        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("ascii")
                    ).hexdigest()
                    manifest["files"] = hashes
                    manifest["policy_digest"] = digest
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    bundle = PolicyBundle.load(root, digest)
                    role = "ci_repair" if relative_path == "repair.config.toml" else "audit"
                    with self.assertRaisesRegex(PolicyBundleError, error):
                        bundle.runtime_policy(role)


if __name__ == "__main__":
    unittest.main()
