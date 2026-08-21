from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "deploy" / "runner" / "image" / "Dockerfile"
README = ROOT / "deploy" / "runner" / "image" / "README.md"
WORKFLOW = ROOT / ".github" / "workflows" / "runner-image.yml"


class RunnerImageTests(unittest.TestCase):
    def test_minimal_image_inputs_and_tool_boundary_are_fixed(self) -> None:
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        from_lines = tuple(
            line for line in dockerfile.splitlines() if line.startswith("FROM ")
        )

        self.assertEqual(2, len(from_lines))
        for line in from_lines:
            self.assertRegex(
                line,
                re.compile(
                    r"^FROM --platform=linux/amd64 "
                    r"docker\.io/library/[a-z0-9.:_-]+@sha256:[0-9a-f]{64}"
                    r"(?: AS [a-z_]+)?$"
                ),
            )
        self.assertIn("node:22.23.1-trixie-slim@sha256:", from_lines[0])
        self.assertIn("python:3.12.13-slim-trixie@sha256:", from_lines[1])
        self.assertIn("apt-get install -y --no-install-recommends", dockerfile)
        self.assertIn("rm -rf /var/lib/apt/lists/* /tmp/npm-cache", dockerfile)
        for required in (
            "curl",
            "git",
            "libatomic1",
            "patch",
            "ripgrep",
            "/usr/bin/timeout",
            "/usr/local/bin/node",
            "/usr/local/lib/node_modules/npm",
        ):
            self.assertIn(required, dockerfile)
        self.assertNotIn("apt-get upgrade", dockerfile)
        self.assertNotIn("curl http", dockerfile)
        self.assertNotIn("ADD ", dockerfile)
        self.assertNotIn("ARG ", dockerfile)
        self.assertNotIn("/var/run/docker.sock", dockerfile)
        self.assertNotRegex(dockerfile.lower(), r"(token|password|private[_ -]?key)")
        self.assertIn(
            'org.opencontainers.image.source="https://github.com/longwdl/'
            'codex_cloud_task_scheduler"',
            dockerfile,
        )
        self.assertNotIn("codex-dispatcher-fixture", dockerfile)

    def test_documentation_preserves_digest_and_retirement_gates(self) -> None:
        readme = README.read_text(encoding="utf-8")
        normalized = " ".join(readme.split())

        for required in (
            "linux/amd64",
            "no fixable critical or high finding",
            "at most 1 GiB uncompressed",
            "at most 2 GiB",
            "name@sha256:<digest>",
            "Keep the current `codex-universal` digest",
            "STATUS/recovery",
            "docker image rm",
            "Never use `docker system prune`",
            "must fail closed",
        ):
            self.assertIn(required, normalized)

    def test_repository_workflow_keeps_pull_requests_unprivileged(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        for required in (
            "pull_request:",
            "contents: read",
            "if: github.event_name == 'pull_request'",
            "ubuntu-24.04",
            "timeout-minutes: 20",
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
            "--platform=linux/amd64",
            "test \"$size\" -le 1073741824",
            "image_id=",
            "image_size_bytes=",
            "image_layer_count=",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges=true",
            "--pull=never",
            "deploy/runner/image/Dockerfile",
        ):
            self.assertIn(required, workflow)
        for prohibited in (
            "build-push-action",
            "secrets.",
            "GITHUB_TOKEN",
            "pull_request_target",
            "latest",
        ):
            self.assertNotIn(prohibited, workflow)

    def test_manual_publisher_is_exact_commit_only_and_self_cleans(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        for required in (
            "workflow_dispatch:",
            "expected_commit:",
            "if: github.event_name == 'workflow_dispatch'",
            "packages: write",
            "group: runner-image-publication",
            "test \"$ACTUAL_REF\" = refs/heads/main",
            "test \"$EXPECTED_COMMIT\" = \"$ACTUAL_COMMIT\"",
            "persist-credentials: false",
            "ghcr.io/longwdl/codex-cloud-task-scheduler-runner",
            'image_ref="$IMAGE_REPOSITORY:sha-$ACTUAL_COMMIT"',
            "GHCR_TOKEN: ${{ github.token }}",
            "--password-stdin",
            'docker push "$image_ref"',
            "test \"${#repo_digests[@]}\" -eq 1",
            'docker pull --platform=linux/amd64 "$repo_digest"',
            'test "${pulled_digests[0]}" = "$repo_digest"',
            'echo "repo_digest=$repo_digest"',
            "if: always()",
            'rm -rf -- "$DOCKER_CONFIG"',
        ):
            self.assertIn(required, workflow)

        self.assertEqual(1, workflow.count("packages: write"))
        self.assertEqual(
            4,
            workflow.count(
                "DOCKER_CONFIG: ${{ runner.temp }}/codex-runner-publisher-"
            ),
        )
        self.assertNotIn(
            "env:\n      IMAGE_REPOSITORY: "
            "ghcr.io/longwdl/codex-cloud-task-scheduler-runner\n"
            "      DOCKER_CONFIG:",
            workflow,
        )
        self.assertNotIn("push:\n", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("codex-dispatcher-fixture", workflow)


if __name__ == "__main__":
    unittest.main()
