from __future__ import annotations

import json
import unittest
from pathlib import Path

from codex_dispatcher.executors.codex_docker import (
    CONTAINER_CODEX_HOME,
    DockerCodexRuntime,
    build_docker_codex_plan,
    build_docker_login_status_plan,
)


WORK_ITEM = "wi_" + "1" * 24
TURN = "turn_" + "2" * 32
SESSION = "123e4567-e89b-12d3-a456-426614174000"
IMAGE = "registry.example.invalid/codex-runner@sha256:" + "a" * 64
PROXY_URL = "http://codex-egress-proxy:3128"
DOCKER_CONFIG = Path("/srv/codex-runner/run/docker-cli")
ROOT = Path(__file__).resolve().parents[1]


def runtime() -> DockerCodexRuntime:
    return DockerCodexRuntime(
        docker_path=Path("/usr/bin/docker"),
        docker_host="unix:///run/user/998/docker.sock",
        cli_config_directory=DOCKER_CONFIG,
        image=IMAGE,
        egress_proxy_url=PROXY_URL,
    )


class DockerCodexPlanTests(unittest.TestCase):
    def test_offline_example_cannot_be_installed_as_a_live_image(self) -> None:
        payload = json.loads(
            (
                ROOT / "config" / "runner-rootless-docker.offline-example.json"
            ).read_text(encoding="utf-8")
        )
        direct = json.loads(
            (ROOT / "config" / "runner.example.json").read_text(encoding="utf-8")
        )

        self.assertEqual("direct", direct["execution_mode"])
        self.assertEqual("rootless_docker", payload["execution_mode"])
        self.assertEqual(
            "replace.invalid/codex-runner@sha256:" + "0" * 64,
            payload["docker_runtime"]["image"],
        )
        self.assertEqual(
            "http://10.0.2.2:3128",
            payload["docker_runtime"]["egress_proxy_url"],
        )

    def test_turn_plan_is_digest_pinned_bounded_and_prompt_free(self) -> None:
        plan = build_docker_codex_plan(
            runtime=runtime(),
            work_item_id=WORK_ITEM,
            turn_id=TURN,
            repository=Path("/srv/codex-runner/work-items/owner__repo/issue-42/repo"),
            codex_home=Path(
                "/srv/codex-runner/work-items/owner__repo/issue-42/runner-state/codex-home"
            ),
            auth_file=Path("/srv/codex-runner/app/auth.json"),
            output_schema=Path("/srv/codex-runner/etc/agent-result.schema.json"),
            session_id=SESSION,
        )

        self.assertTrue(plan.reads_prompt_from_stdin)
        self.assertEqual({"DOCKER_CONFIG": str(DOCKER_CONFIG)}, plan.environment)
        self.assertEqual("/usr/bin/docker", plan.argv[0])
        self.assertIn("--host=unix:///run/user/998/docker.sock", plan.argv)
        for fixed in (
            "--rm",
            "--stop-timeout=5",
            "--pull=never",
            "--log-driver=none",
            "--interactive",
            "--init",
            "--user=0:0",
            "--read-only",
            "--network=codex-egress",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true",
            "--pids-limit=512",
            "--memory=8589934592",
            "--memory-swap=8589934592",
            "--cpus=2.0",
            "--ulimit=nofile=1024:1024",
            "--ulimit=nproc=512:512",
            "--ulimit=core=0:0",
        ):
            self.assertIn(fixed, plan.argv)
        self.assertEqual(IMAGE, plan.argv[plan.argv.index(IMAGE)])
        image_index = plan.argv.index(IMAGE)
        self.assertEqual(
            (
                "/usr/bin/timeout",
                "--signal=KILL",
                "--kill-after=5s",
                "3600s",
                "/usr/local/bin/codex",
            ),
            plan.argv[image_index + 1 : image_index + 6],
        )
        self.assertIn(f"--env=CODEX_HOME={CONTAINER_CODEX_HOME}", plan.argv)
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            self.assertIn(f"--env={name}={PROXY_URL}", plan.argv)
        self.assertIn("--env=NO_PROXY=", plan.argv)
        self.assertIn("--env=no_proxy=", plan.argv)
        self.assertIn("resume", plan.argv)
        self.assertIn(SESSION, plan.argv)
        self.assertNotIn("Prompt contents", " ".join(plan.argv))
        self.assertFalse(any("--privileged" in item for item in plan.argv))
        self.assertFalse(any("--cap-add" in item for item in plan.argv))
        self.assertFalse(any("--pid=host" in item for item in plan.argv))
        self.assertFalse(any("--network=host" in item for item in plan.argv))
        self.assertNotIn("--tty", plan.argv)
        self.assertFalse(any("docker.sock,target=" in item for item in plan.argv))

    def test_turn_mounts_only_one_repo_session_home_auth_and_schema(self) -> None:
        repository = Path("/srv/codex-runner/work-items/owner__repo/issue-42/repo")
        codex_home = Path(
            "/srv/codex-runner/work-items/owner__repo/issue-42/runner-state/codex-home"
        )
        auth_file = Path("/srv/codex-runner/app/auth.json")
        schema = Path("/srv/codex-runner/etc/agent-result.schema.json")
        plan = build_docker_codex_plan(
            runtime=runtime(),
            work_item_id=WORK_ITEM,
            turn_id=TURN,
            repository=repository,
            codex_home=codex_home,
            auth_file=auth_file,
            output_schema=schema,
            session_id=None,
        )
        mounts = tuple(item for item in plan.argv if item.startswith("--mount="))

        self.assertEqual(4, len(mounts))
        self.assertIn(f"source={repository},target=/workspace", mounts[0])
        self.assertIn(f"source={codex_home},target=/codex-home", mounts[1])
        self.assertEqual(
            f"--mount=type=bind,source={auth_file},target=/codex-home/auth.json,readonly",
            mounts[2],
        )
        self.assertEqual(
            f"--mount=type=bind,source={schema},"
            "target=/runner-contract/agent-result.schema.json,readonly",
            mounts[3],
        )
        self.assertFalse(any("target=/srv" in item for item in mounts))
        self.assertFalse(any("source=/srv/codex-runner/app,target=" in item for item in mounts))
        self.assertFalse(any("source=/srv/codex-runner/work-items,target=" in item for item in mounts))

    def test_auth_plan_has_no_repository_or_schema_mount(self) -> None:
        plan = build_docker_login_status_plan(
            runtime=runtime(),
            work_item_id=WORK_ITEM,
            codex_home=Path(
                "/srv/codex-runner/work-items/owner__repo/issue-42/runner-state/codex-home"
            ),
            auth_file=Path("/srv/codex-runner/app/auth.json"),
        )
        mounts = tuple(item for item in plan.argv if item.startswith("--mount="))

        self.assertFalse(plan.reads_prompt_from_stdin)
        self.assertEqual(2, len(mounts))
        self.assertEqual(("login", "status"), plan.argv[-2:])
        self.assertFalse(any("/workspace" in item for item in plan.argv))
        self.assertFalse(any("agent-result.schema" in item for item in plan.argv))

    def test_rejects_unpinned_images_nonunix_daemons_and_unsafe_mounts(self) -> None:
        compatible_runtime = DockerCodexRuntime(
            Path("/usr/bin/docker"),
            "unix:///run/user/998/docker.sock",
            DOCKER_CONFIG,
            IMAGE,
        )
        with self.assertRaisesRegex(ValueError, "audited egress proxy"):
            build_docker_login_status_plan(
                runtime=compatible_runtime,
                work_item_id=WORK_ITEM,
                codex_home=Path(
                    "/srv/codex-runner/work-items/owner__repo/issue-42/runner-state/codex-home"
                ),
                auth_file=Path("/srv/codex-runner/app/auth.json"),
            )
        with self.assertRaisesRegex(ValueError, "digest-pinned"):
            DockerCodexRuntime(
                Path("/usr/bin/docker"),
                "unix:///run/user/998/docker.sock",
                DOCKER_CONFIG,
                "registry.example.invalid/codex-runner:latest",
                PROXY_URL,
            )
        with self.assertRaisesRegex(ValueError, "Unix socket"):
            DockerCodexRuntime(
                Path("/usr/bin/docker"),
                "tcp://127.0.0.1:2375",
                DOCKER_CONFIG,
                IMAGE,
                PROXY_URL,
            )
        with self.assertRaisesRegex(ValueError, "egress_proxy_url"):
            DockerCodexRuntime(
                Path("/usr/bin/docker"),
                "unix:///run/user/998/docker.sock",
                DOCKER_CONFIG,
                IMAGE,
                "http://user:secret@codex-egress-proxy:3128",
            )
        with self.assertRaisesRegex(ValueError, "comma"):
            build_docker_login_status_plan(
                runtime=runtime(),
                work_item_id=WORK_ITEM,
                codex_home=Path("/srv/codex-runner/unsafe,home"),
                auth_file=Path("/srv/codex-runner/app/auth.json"),
            )
        with self.assertRaisesRegex(ValueError, "one WorkItem"):
            build_docker_codex_plan(
                runtime=runtime(),
                work_item_id=WORK_ITEM,
                turn_id=TURN,
                repository=Path("/etc"),
                codex_home=Path("/srv/codex-runner/arbitrary"),
                auth_file=Path("/srv/codex-runner/app/auth.json"),
                output_schema=Path(
                    "/srv/codex-runner/etc/agent-result.schema.json"
                ),
                session_id=None,
            )


if __name__ == "__main__":
    unittest.main()
