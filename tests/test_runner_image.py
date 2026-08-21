from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "deploy" / "runner" / "image" / "Dockerfile"
README = ROOT / "deploy" / "runner" / "image" / "README.md"


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


if __name__ == "__main__":
    unittest.main()
