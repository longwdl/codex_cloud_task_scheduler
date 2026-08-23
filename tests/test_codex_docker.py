from __future__ import annotations

import json
import unittest
from pathlib import Path

from codex_dispatcher.executors.codex_docker import (
    CONTAINER_CODEX_HOME,
    DOCKER_LABEL_POLICY_DIGEST,
    DOCKER_LABEL_SESSION_GENERATION,
    DOCKER_LABEL_SESSION_GENERATION_ID,
    DOCKER_LABEL_TURN,
    DOCKER_LABEL_WORK_ITEM,
    DockerCodexRuntime,
    build_docker_codex_plan,
    build_docker_login_status_plan,
)
from codex_dispatcher.runner_policy import PolicyBundle


WORK_ITEM = "wi_" + "1" * 24
TURN = "turn_" + "2" * 32
SESSION = "123e4567-e89b-12d3-a456-426614174000"
GENERATION_ID = "sg_" + "d" * 32
IMAGE = "registry.example.invalid/codex-runner@sha256:" + "a" * 64
CODEX_SHA256 = "b" * 64
CODEX_PATH = Path("/srv/codex-runner/tools/codex/0.147.0/bin/codex")
CODE_MODE_HOST_PATH = CODEX_PATH.with_name("codex-code-mode-host")
CODE_MODE_HOST_SHA256 = "c" * 64
PROXY_URL = "http://codex-egress-proxy:3128"
DOCKER_CONFIG = Path("/srv/codex-runner/run/docker-cli")
ROOT = Path(__file__).resolve().parents[1]


def runtime() -> DockerCodexRuntime:
    return DockerCodexRuntime(
        docker_path=Path("/usr/bin/docker"),
        docker_host="unix:///run/user/998/docker.sock",
        cli_config_directory=DOCKER_CONFIG,
        image=IMAGE,
        codex_sha256=CODEX_SHA256,
        code_mode_host_path=CODE_MODE_HOST_PATH,
        code_mode_host_sha256=CODE_MODE_HOST_SHA256,
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
        self.assertEqual(
            "0" * 64,
            payload["docker_runtime"]["code_mode_host_sha256"],
        )

    def test_turn_plan_is_digest_pinned_bounded_and_prompt_free(self) -> None:
        plan = build_docker_codex_plan(
            runtime=runtime(),
            work_item_id=WORK_ITEM,
            turn_id=TURN,
            codex_path=CODEX_PATH,
            repository=Path("/srv/codex-runner/work-items/owner__repo/issue-42/repo"),
            codex_home=Path(
                "/srv/codex-runner/work-items/owner__repo/issue-42/runner-state/codex-home"
            ),
            auth_file=Path(
                "/srv/codex-runner/work-items/owner__repo/issue-42/"
                "runner-state/codex-home/auth.json"
            ),
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
            "--entrypoint=",
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
        self.assertFalse(any(item.startswith("--label=") for item in plan.argv))
        self.assertNotIn("--tty", plan.argv)
        self.assertFalse(any("docker.sock,target=" in item for item in plan.argv))

    def test_turn_mounts_only_one_repo_session_home_auth_and_schema(self) -> None:
        repository = Path("/srv/codex-runner/work-items/owner__repo/issue-42/repo")
        codex_home = Path(
            "/srv/codex-runner/work-items/owner__repo/issue-42/runner-state/codex-home"
        )
        auth_file = codex_home / "auth.json"
        schema = Path("/srv/codex-runner/etc/agent-result.schema.json")
        plan = build_docker_codex_plan(
            runtime=runtime(),
            work_item_id=WORK_ITEM,
            turn_id=TURN,
            codex_path=CODEX_PATH,
            repository=repository,
            codex_home=codex_home,
            auth_file=auth_file,
            output_schema=schema,
            session_id=None,
        )
        mounts = tuple(item for item in plan.argv if item.startswith("--mount="))

        self.assertEqual(5, len(mounts))
        self.assertEqual(
            f"--mount=type=bind,source={CODEX_PATH},target=/usr/local/bin/codex,readonly",
            mounts[0],
        )
        self.assertEqual(
            f"--mount=type=bind,source={CODE_MODE_HOST_PATH},"
            "target=/usr/local/bin/codex-code-mode-host,readonly",
            mounts[1],
        )
        self.assertIn(f"source={repository},target=/workspace", mounts[2])
        self.assertIn(f"source={codex_home},target=/codex-home", mounts[3])
        self.assertEqual(
            f"--mount=type=bind,source={schema},"
            "target=/runner-contract/agent-result.schema.json,readonly",
            mounts[4],
        )
        self.assertFalse(any(str(auth_file) in item for item in mounts))
        self.assertFalse(any("target=/srv" in item for item in mounts))
        self.assertFalse(any("source=/srv/codex-runner/app,target=" in item for item in mounts))
        self.assertFalse(any("source=/srv/codex-runner/work-items,target=" in item for item in mounts))

        audit_plan = build_docker_codex_plan(
            runtime=runtime(),
            work_item_id=WORK_ITEM,
            turn_id=TURN,
            codex_path=CODEX_PATH,
            repository=repository,
            codex_home=codex_home,
            auth_file=auth_file,
            output_schema=schema,
            session_id=None,
            repository_readonly=True,
        )
        audit_mounts = tuple(
            item for item in audit_plan.argv if item.startswith("--mount=")
        )
        self.assertEqual(
            f"--mount=type=bind,source={repository},target=/workspace,readonly",
            audit_mounts[2],
        )

    def test_policy_bundle_mounts_exact_three_readonly_targets(self) -> None:
        policy_root = ROOT / "config" / "runner-codex-policy"
        manifest = json.loads((policy_root / "manifest.json").read_text(encoding="utf-8"))
        policy = PolicyBundle.load(policy_root, manifest["policy_digest"])
        plan = build_docker_codex_plan(
            runtime=runtime(),
            work_item_id=WORK_ITEM,
            turn_id=TURN,
            codex_path=CODEX_PATH,
            repository=Path("/srv/codex-runner/work-items/owner__repo/issue-42/repo"),
            codex_home=Path(
                "/srv/codex-runner/work-items/owner__repo/issue-42/runner-state/codex-home"
            ),
            auth_file=Path(
                "/srv/codex-runner/work-items/owner__repo/issue-42/"
                "runner-state/codex-home/auth.json"
            ),
            output_schema=Path("/srv/codex-runner/etc/agent-result.schema.json"),
            session_id=None,
            policy_bundle=policy,
        )
        mounts = tuple(item for item in plan.argv if item.startswith("--mount="))

        self.assertEqual(8, len(mounts))
        self.assertEqual(
            f"--mount=type=bind,source={policy.config_path},"
            "target=/codex-home/config.toml,readonly",
            mounts[5],
        )
        self.assertEqual(
            f"--mount=type=bind,source={policy.agents_path},"
            "target=/codex-home/agents,readonly",
            mounts[6],
        )
        self.assertEqual(
            f"--mount=type=bind,source={policy.requirements_path},"
            "target=/etc/codex/requirements.toml,readonly",
            mounts[7],
        )
        self.assertIn("--strict-config", plan.argv)
        self.assertIn("gpt-5.6-sol", plan.argv)
        self.assertIn("multi_agent", plan.argv)
        self.assertFalse(any(f"source={policy.root},target=" in item for item in mounts))

    def test_v2_turn_uses_generation_home_and_exact_identity_labels(self) -> None:
        policy_root = ROOT / "config" / "runner-codex-policy"
        manifest = json.loads((policy_root / "manifest.json").read_text(encoding="utf-8"))
        policy = PolicyBundle.load(policy_root, manifest["policy_digest"])
        work_item_root = Path(
            "/srv/codex-runner/work-items/owner__repo/issue-42"
        )
        codex_home = (
            work_item_root
            / "runner-state"
            / "generations"
            / GENERATION_ID
            / "codex-home"
        )
        plan = build_docker_codex_plan(
            runtime=runtime(),
            work_item_id=WORK_ITEM,
            turn_id=TURN,
            codex_path=CODEX_PATH,
            repository=work_item_root / "repo",
            codex_home=codex_home,
            auth_file=codex_home / "auth.json",
            output_schema=Path("/srv/codex-runner/etc/agent-result.schema.json"),
            session_id=None,
            policy_bundle=policy,
            session_generation_id=GENERATION_ID,
            session_generation=2,
            agent_policy_digest=policy.policy_digest,
        )

        for key, value in (
            (DOCKER_LABEL_WORK_ITEM, WORK_ITEM),
            (DOCKER_LABEL_SESSION_GENERATION_ID, GENERATION_ID),
            (DOCKER_LABEL_SESSION_GENERATION, "2"),
            (DOCKER_LABEL_TURN, TURN),
            (DOCKER_LABEL_POLICY_DIGEST, policy.policy_digest),
        ):
            self.assertIn(f"--label={key}={value}", plan.argv)
        self.assertIn(f"source={codex_home},target=/codex-home", "\n".join(plan.argv))

        with self.assertRaisesRegex(ValueError, "exact policy bundle"):
            build_docker_codex_plan(
                runtime=runtime(),
                work_item_id=WORK_ITEM,
                turn_id=TURN,
                codex_path=CODEX_PATH,
                repository=work_item_root / "repo",
                codex_home=codex_home,
                auth_file=codex_home / "auth.json",
                output_schema=Path(
                    "/srv/codex-runner/etc/agent-result.schema.json"
                ),
                session_id=None,
                session_generation_id=GENERATION_ID,
                session_generation=2,
                agent_policy_digest=policy.policy_digest,
            )

    def test_auth_plan_has_no_repository_or_schema_mount(self) -> None:
        plan = build_docker_login_status_plan(
            runtime=runtime(),
            work_item_id=WORK_ITEM,
            codex_path=CODEX_PATH,
            codex_home=Path(
                "/srv/codex-runner/work-items/owner__repo/issue-42/runner-state/codex-home"
            ),
            auth_file=Path(
                "/srv/codex-runner/work-items/owner__repo/issue-42/"
                "runner-state/codex-home/auth.json"
            ),
        )
        mounts = tuple(item for item in plan.argv if item.startswith("--mount="))

        self.assertFalse(plan.reads_prompt_from_stdin)
        self.assertEqual(3, len(mounts))
        self.assertIn("target=/usr/local/bin/codex,readonly", mounts[0])
        self.assertIn(
            "target=/usr/local/bin/codex-code-mode-host,readonly", mounts[1]
        )
        self.assertIn("target=/codex-home", mounts[2])
        self.assertFalse(any("target=/codex-home/auth.json" in item for item in mounts))
        self.assertEqual(("login", "status"), plan.argv[-2:])
        self.assertFalse(any("/workspace" in item for item in plan.argv))
        self.assertFalse(any("agent-result.schema" in item for item in plan.argv))

    def test_rejects_unpinned_images_nonunix_daemons_and_unsafe_mounts(self) -> None:
        compatible_runtime = DockerCodexRuntime(
            docker_path=Path("/usr/bin/docker"),
            docker_host="unix:///run/user/998/docker.sock",
            cli_config_directory=DOCKER_CONFIG,
            image=IMAGE,
            codex_sha256=CODEX_SHA256,
            code_mode_host_path=CODE_MODE_HOST_PATH,
            code_mode_host_sha256=CODE_MODE_HOST_SHA256,
        )
        with self.assertRaisesRegex(ValueError, "audited egress proxy"):
            build_docker_login_status_plan(
                runtime=compatible_runtime,
                work_item_id=WORK_ITEM,
                codex_path=CODEX_PATH,
                codex_home=Path(
                    "/srv/codex-runner/work-items/owner__repo/issue-42/runner-state/codex-home"
                ),
                auth_file=Path("/srv/codex-runner/app/auth.json"),
            )
        with self.assertRaisesRegex(ValueError, "digest-pinned"):
            DockerCodexRuntime(
                docker_path=Path("/usr/bin/docker"),
                docker_host="unix:///run/user/998/docker.sock",
                cli_config_directory=DOCKER_CONFIG,
                image="registry.example.invalid/codex-runner:latest",
                codex_sha256=CODEX_SHA256,
                code_mode_host_path=CODE_MODE_HOST_PATH,
                code_mode_host_sha256=CODE_MODE_HOST_SHA256,
                egress_proxy_url=PROXY_URL,
            )
        with self.assertRaisesRegex(ValueError, "codex_sha256"):
            DockerCodexRuntime(
                docker_path=Path("/usr/bin/docker"),
                docker_host="unix:///run/user/998/docker.sock",
                cli_config_directory=DOCKER_CONFIG,
                image=IMAGE,
                codex_sha256="not-a-digest",
                code_mode_host_path=CODE_MODE_HOST_PATH,
                code_mode_host_sha256=CODE_MODE_HOST_SHA256,
                egress_proxy_url=PROXY_URL,
            )
        with self.assertRaisesRegex(ValueError, "code_mode_host_sha256"):
            DockerCodexRuntime(
                docker_path=Path("/usr/bin/docker"),
                docker_host="unix:///run/user/998/docker.sock",
                cli_config_directory=DOCKER_CONFIG,
                image=IMAGE,
                codex_sha256=CODEX_SHA256,
                code_mode_host_path=CODE_MODE_HOST_PATH,
                code_mode_host_sha256="not-a-digest",
                egress_proxy_url=PROXY_URL,
            )
        with self.assertRaisesRegex(ValueError, "Unix socket"):
            DockerCodexRuntime(
                docker_path=Path("/usr/bin/docker"),
                docker_host="tcp://127.0.0.1:2375",
                cli_config_directory=DOCKER_CONFIG,
                image=IMAGE,
                codex_sha256=CODEX_SHA256,
                code_mode_host_path=CODE_MODE_HOST_PATH,
                code_mode_host_sha256=CODE_MODE_HOST_SHA256,
                egress_proxy_url=PROXY_URL,
            )
        with self.assertRaisesRegex(ValueError, "egress_proxy_url"):
            DockerCodexRuntime(
                docker_path=Path("/usr/bin/docker"),
                docker_host="unix:///run/user/998/docker.sock",
                cli_config_directory=DOCKER_CONFIG,
                image=IMAGE,
                codex_sha256=CODEX_SHA256,
                code_mode_host_path=CODE_MODE_HOST_PATH,
                code_mode_host_sha256=CODE_MODE_HOST_SHA256,
                egress_proxy_url="http://user:secret@codex-egress-proxy:3128",
            )
        mismatched_runtime = DockerCodexRuntime(
            docker_path=Path("/usr/bin/docker"),
            docker_host="unix:///run/user/998/docker.sock",
            cli_config_directory=DOCKER_CONFIG,
            image=IMAGE,
            codex_sha256=CODEX_SHA256,
            code_mode_host_path=Path("/opt/other/codex-code-mode-host"),
            code_mode_host_sha256=CODE_MODE_HOST_SHA256,
            egress_proxy_url=PROXY_URL,
        )
        with self.assertRaisesRegex(ValueError, "fixed sibling"):
            build_docker_login_status_plan(
                runtime=mismatched_runtime,
                work_item_id=WORK_ITEM,
                codex_path=CODEX_PATH,
                codex_home=Path(
                    "/srv/codex-runner/work-items/owner__repo/issue-42/runner-state/codex-home"
                ),
                auth_file=Path("/srv/codex-runner/app/auth.json"),
            )
        with self.assertRaisesRegex(ValueError, "comma"):
            build_docker_login_status_plan(
                runtime=runtime(),
                work_item_id=WORK_ITEM,
                codex_path=CODEX_PATH,
                codex_home=Path("/srv/codex-runner/unsafe,home"),
                auth_file=Path("/srv/codex-runner/app/auth.json"),
            )
        with self.assertRaisesRegex(ValueError, "one WorkItem"):
            build_docker_codex_plan(
                runtime=runtime(),
                work_item_id=WORK_ITEM,
                turn_id=TURN,
                codex_path=CODEX_PATH,
                repository=Path("/etc"),
                codex_home=Path("/srv/codex-runner/arbitrary"),
                auth_file=Path("/srv/codex-runner/app/auth.json"),
                output_schema=Path(
                    "/srv/codex-runner/etc/agent-result.schema.json"
                ),
                session_id=None,
            )
        with self.assertRaisesRegex(ValueError, "WorkItem host binding"):
            build_docker_login_status_plan(
                runtime=runtime(),
                work_item_id=WORK_ITEM,
                codex_path=CODEX_PATH,
                codex_home=Path(
                    "/srv/codex-runner/work-items/owner__repo/issue-42/runner-state/codex-home"
                ),
                auth_file=Path("/srv/codex-runner/app/auth.json"),
            )


if __name__ == "__main__":
    unittest.main()
