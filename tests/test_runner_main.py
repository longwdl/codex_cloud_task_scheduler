from __future__ import annotations

import io
from hashlib import sha256
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.runner_main import (
    RunnerConfigurationError,
    build_runner_service,
    load_runner_configuration,
    run,
)
from codex_dispatcher.executors.codex_docker import ROOTLESS_HOST_PROXY_URL


def policy_bundle(root: Path) -> tuple[Path, str]:
    policy = root / "policy"
    agents = policy / "agents"
    agents.mkdir(parents=True, mode=0o700)
    files = {
        "config.toml": b'model = "gpt-5.6-sol"\n',
        "requirements.toml": b'allowed_web_search_modes = ["disabled"]\n',
        "agents/spark-worker.toml": b'name = "spark_worker"\n',
        "agents/luna-worker.toml": b'name = "luna_worker"\n',
        "agents/terra-worker.toml": b'name = "terra_worker"\n',
        "agents/sol-specialist.toml": b'name = "sol_specialist"\n',
    }
    hashes: dict[str, str] = {}
    for relative, contents in files.items():
        path = policy / relative
        path.write_bytes(contents)
        path.chmod(0o600)
        hashes[relative] = sha256(contents).hexdigest()
    digest = sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "codex_version": "0.147.0",
        "policy_digest": digest,
        "files": hashes,
    }
    (policy / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (policy / "manifest.json").chmod(0o600)
    policy.chmod(0o700)
    agents.chmod(0o700)
    return policy, digest


def protected_file(path: Path, content: str, *, executable: bool = False) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700 if executable else 0o600)


def config(root: Path) -> Path:
    git = root / "git"
    codex = root / "codex"
    schema = root / "schema.json"
    protected_file(git, "#!/bin/sh\nexit 0\n", executable=True)
    protected_file(codex, "#!/bin/sh\nexit 0\n", executable=True)
    protected_file(
        root / "codex-code-mode-host",
        "#!/bin/sh\nexit 0\n",
        executable=True,
    )
    protected_file(schema, "{}\n")
    codex_home = root / "codex-home"
    codex_home.mkdir(mode=0o700, exist_ok=True)
    codex_home.chmod(0o700)
    work_items = root / "work-items"
    work_items.mkdir(mode=0o700, exist_ok=True)
    work_items.chmod(0o700)
    run = root / "run"
    run.mkdir(mode=0o700, exist_ok=True)
    run.chmod(0o700)
    config_path = root / "config.json"
    protected_file(
        config_path,
        json.dumps(
            {
                "version": 1,
                "git_path": str(git),
                "codex_path": str(codex),
                "codex_home": str(codex_home),
                "output_schema": str(schema),
                "work_items_root": str(work_items),
                "active_lock_path": str(run / "active.lock"),
                "egress_proxy_url": "http://127.0.0.1:3128",
                "git_timeout_seconds": 10,
                "codex_timeout_seconds": 20,
            }
        ),
    )
    return config_path


class RunnerMainTests(unittest.TestCase):
    def test_loads_only_exact_protected_fixed_path_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            loaded = load_runner_configuration(config(root))
            self.assertEqual((root / "git").resolve(), loaded.git_path)
            self.assertEqual((root / "codex-home").resolve(), loaded.codex_home)
            self.assertEqual((root / "work-items").resolve(), loaded.work_items_root)
            self.assertEqual(20.0, loaded.codex_timeout_seconds)
            self.assertEqual("http://127.0.0.1:3128", loaded.egress_proxy_url)
            self.assertIsNotNone(build_runner_service(loaded))

    def test_legacy_configuration_without_proxy_remains_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = config(root)
            payload = json.loads(path.read_text(encoding="utf-8"))
            del payload["egress_proxy_url"]
            path.write_text(json.dumps(payload), encoding="utf-8")

            self.assertIsNone(load_runner_configuration(path).egress_proxy_url)

    def test_explicit_rootless_docker_configuration_is_strict_and_wired(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            path = config(root)
            docker_config = root / "docker-config"
            docker_config.mkdir(mode=0o700)
            disk_images = root / "disk-images"
            disk_images.mkdir(mode=0o700)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["execution_mode"] = "rootless_docker"
            policy_root, policy_digest = policy_bundle(root)
            payload["policy_root"] = str(policy_root)
            payload["policy_digest"] = policy_digest
            payload["docker_runtime"] = {
                "docker_path": str(root / "git"),
                "docker_host": f"unix:///run/user/{os.geteuid()}/docker.sock",
                "cli_config_directory": str(docker_config),
                "image": "registry.example.invalid/codex-runner@sha256:" + "a" * 64,
                "codex_sha256": "b" * 64,
                "code_mode_host_sha256": "c" * 64,
                "egress_proxy_url": ROOTLESS_HOST_PROXY_URL,
                "work_item_disk": {
                    "image_directory": str(disk_images),
                    "image_size_bytes": 64 * 1024 * 1024,
                    "host_reserve_bytes": 64 * 1024 * 1024,
                    "mkfs_ext4_path": str(root / "git"),
                    "fuse2fs_path": str(root / "git"),
                    "fusermount_path": str(root / "git"),
                    "e2fsck_path": str(root / "git"),
                    "findmnt_path": str(root / "git"),
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = load_runner_configuration(path)

            self.assertEqual("rootless_docker", loaded.execution_mode)
            self.assertIsNotNone(loaded.docker_runtime)
            self.assertIsNotNone(loaded.work_item_disk)
            assert loaded.docker_runtime is not None
            self.assertEqual(
                (root / "codex-code-mode-host").resolve(),
                loaded.docker_runtime.code_mode_host_path,
            )
            self.assertEqual(
                "c" * 64,
                loaded.docker_runtime.code_mode_host_sha256,
            )
            self.assertIsNotNone(build_runner_service(loaded))

    def test_rejects_implicit_partial_or_wrong_rootless_docker_configuration(self) -> None:
        cases = ("implicit", "partial", "socket", "proxy", "unknown")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(
                dir=Path.cwd()
            ) as temp_dir:
                root = Path(temp_dir)
                path = config(root)
                docker_config = root / "docker-config"
                docker_config.mkdir(mode=0o700)
                disk_images = root / "disk-images"
                disk_images.mkdir(mode=0o700)
                runtime = {
                    "docker_path": str(root / "git"),
                    "docker_host": f"unix:///run/user/{os.geteuid()}/docker.sock",
                    "cli_config_directory": str(docker_config),
                    "image": "registry.example.invalid/codex-runner@sha256:" + "a" * 64,
                    "codex_sha256": "b" * 64,
                    "code_mode_host_sha256": "c" * 64,
                    "egress_proxy_url": ROOTLESS_HOST_PROXY_URL,
                    "work_item_disk": {
                        "image_directory": str(disk_images),
                        "image_size_bytes": 64 * 1024 * 1024,
                        "host_reserve_bytes": 64 * 1024 * 1024,
                        "mkfs_ext4_path": str(root / "git"),
                        "fuse2fs_path": str(root / "git"),
                        "fusermount_path": str(root / "git"),
                        "e2fsck_path": str(root / "git"),
                        "findmnt_path": str(root / "git"),
                    },
                }
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["docker_runtime"] = runtime
                if case != "implicit":
                    payload["execution_mode"] = "rootless_docker"
                    policy_root, policy_digest = policy_bundle(root)
                    payload["policy_root"] = str(policy_root)
                    payload["policy_digest"] = policy_digest
                if case == "partial":
                    del runtime["image"]
                elif case == "socket":
                    runtime["docker_host"] = "unix:///var/run/docker.sock"
                elif case == "proxy":
                    runtime["egress_proxy_url"] = "http://127.0.0.1:3128"
                elif case == "unknown":
                    runtime["extra"] = True
                path.write_text(json.dumps(payload), encoding="utf-8")

                with self.assertRaises(RunnerConfigurationError):
                    load_runner_configuration(path)

    def test_rootless_docker_accepts_no_policy_but_rejects_partial_or_invalid_policy(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            path = config(root)
            docker_config = root / "docker-config"
            docker_config.mkdir(mode=0o700)
            disk_images = root / "disk-images"
            disk_images.mkdir(mode=0o700)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["execution_mode"] = "rootless_docker"
            payload["docker_runtime"] = {
                "docker_path": str(root / "git"),
                "docker_host": f"unix:///run/user/{os.geteuid()}/docker.sock",
                "cli_config_directory": str(docker_config),
                "image": "registry.example.invalid/codex-runner@sha256:" + "a" * 64,
                "codex_sha256": "b" * 64,
                "code_mode_host_sha256": "c" * 64,
                "egress_proxy_url": ROOTLESS_HOST_PROXY_URL,
                "work_item_disk": {
                    "image_directory": str(disk_images),
                    "image_size_bytes": 64 * 1024 * 1024,
                    "host_reserve_bytes": 64 * 1024 * 1024,
                    "mkfs_ext4_path": str(root / "git"),
                    "fuse2fs_path": str(root / "git"),
                    "fusermount_path": str(root / "git"),
                    "e2fsck_path": str(root / "git"),
                    "findmnt_path": str(root / "git"),
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(load_runner_configuration(path).policy_bundle)

            policy_root, policy_digest = policy_bundle(root)
            payload["policy_root"] = str(policy_root)
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RunnerConfigurationError, "policy requires"):
                load_runner_configuration(path)

            payload["policy_digest"] = "0" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RunnerConfigurationError, "policy bundle"):
                load_runner_configuration(path)

            payload["policy_digest"] = policy_digest
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(
                policy_digest,
                load_runner_configuration(path).policy_bundle.policy_digest,  # type: ignore[union-attr]
            )

    def test_rootless_docker_requires_trusted_code_mode_host(self) -> None:
        for failure in ("missing", "writable"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(
                dir=Path.cwd()
            ) as temp_dir:
                root = Path(temp_dir)
                path = config(root)
                docker_config = root / "docker-config"
                docker_config.mkdir(mode=0o700)
                disk_images = root / "disk-images"
                disk_images.mkdir(mode=0o700)
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["execution_mode"] = "rootless_docker"
                policy_root, policy_digest = policy_bundle(root)
                payload["policy_root"] = str(policy_root)
                payload["policy_digest"] = policy_digest
                payload["docker_runtime"] = {
                    "docker_path": str(root / "git"),
                    "docker_host": f"unix:///run/user/{os.geteuid()}/docker.sock",
                    "cli_config_directory": str(docker_config),
                    "image": "registry.example.invalid/codex-runner@sha256:" + "a" * 64,
                    "codex_sha256": "b" * 64,
                    "code_mode_host_sha256": "c" * 64,
                    "egress_proxy_url": ROOTLESS_HOST_PROXY_URL,
                    "work_item_disk": {
                        "image_directory": str(disk_images),
                        "image_size_bytes": 64 * 1024 * 1024,
                        "host_reserve_bytes": 64 * 1024 * 1024,
                        "mkfs_ext4_path": str(root / "git"),
                        "fuse2fs_path": str(root / "git"),
                        "fusermount_path": str(root / "git"),
                        "e2fsck_path": str(root / "git"),
                        "findmnt_path": str(root / "git"),
                    },
                }
                path.write_text(json.dumps(payload), encoding="utf-8")
                code_mode_host = root / "codex-code-mode-host"
                if failure == "missing":
                    code_mode_host.unlink()
                else:
                    code_mode_host.chmod(0o722)

                with self.assertRaisesRegex(
                    RunnerConfigurationError, "code_mode_host_path"
                ):
                    load_runner_configuration(path)

    def test_rejects_proxy_credentials_and_non_http_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = config(root)
            for proxy_url in (
                "https://127.0.0.1:3128",
                "http://user:secret@127.0.0.1:3128",
                "http://127.0.0.1:3128/path",
            ):
                with self.subTest(proxy_url=proxy_url):
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload["egress_proxy_url"] = proxy_url
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(
                        RunnerConfigurationError, "egress_proxy_url"
                    ):
                        load_runner_configuration(path)
                    path = config(root)

    def test_rejects_writable_duplicate_and_relative_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = config(root)
            path.chmod(0o666)
            with self.assertRaisesRegex(RunnerConfigurationError, "protected"):
                load_runner_configuration(path)

            path.chmod(0o600)
            path.write_text('{"version":1,"version":1}', encoding="utf-8")
            with self.assertRaisesRegex(RunnerConfigurationError, "malformed"):
                load_runner_configuration(path)

            path = config(root)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["work_items_root"] = "relative"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RunnerConfigurationError, "absolute"):
                load_runner_configuration(path)

    def test_rejects_unprotected_shared_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = config(root)
            (root / "codex-home").chmod(0o755)
            with self.assertRaisesRegex(RunnerConfigurationError, "protected directory"):
                load_runner_configuration(path)

            (root / "codex-home").chmod(0o700)
            link = root / "codex-home-link"
            link.symlink_to(root / "codex-home", target_is_directory=True)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["codex_home"] = str(link)
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RunnerConfigurationError, "protected directory"):
                load_runner_configuration(path)

    def test_rejects_untrusted_files_and_unprotected_mutable_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = config(root)
            with patch(
                "codex_dispatcher.runner_main.os.geteuid",
                return_value=path.stat().st_uid + 1,
            ):
                with self.assertRaisesRegex(RunnerConfigurationError, "trusted"):
                    load_runner_configuration(path)

            path = config(root)
            (root / "work-items").chmod(0o755)
            with self.assertRaisesRegex(RunnerConfigurationError, "protected directory"):
                load_runner_configuration(path)

            path = config(root)
            (root / "run").chmod(0o755)
            with self.assertRaisesRegex(RunnerConfigurationError, "protected directory"):
                load_runner_configuration(path)

            path = config(root)
            active_lock = root / "run" / "active.lock"
            active_lock.write_text("", encoding="utf-8")
            active_lock.chmod(0o644)
            with self.assertRaisesRegex(RunnerConfigurationError, "active_lock_path"):
                load_runner_configuration(path)

    def test_rejects_executable_below_a_replaceable_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = config(root)
            tools = root / "mutable-tools"
            tools.mkdir(mode=0o700)
            codex = tools / "codex"
            protected_file(codex, "#!/bin/sh\nexit 0\n", executable=True)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["codex_path"] = str(codex)
            path.write_text(json.dumps(payload), encoding="utf-8")
            tools.chmod(0o770)

            with self.assertRaisesRegex(RunnerConfigurationError, "parent directories"):
                load_runner_configuration(path)

    def test_forced_command_failure_is_generic_and_emits_no_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = io.BytesIO()
            error = io.BytesIO()
            result = run(
                config_path=config(root),
                input_stream=io.BytesIO(b"malformed secret=do-not-echo"),
                output_stream=output,
                error_stream=error,
            )
            self.assertEqual(2, result)
            self.assertEqual(b"", output.getvalue())
            self.assertEqual(b"codex-runner-v1: request rejected\n", error.getvalue())
            self.assertNotIn(b"do-not-echo", error.getvalue())


if __name__ == "__main__":
    unittest.main()
