from __future__ import annotations

import unittest
from pathlib import Path

from codex_dispatcher.executors.codex_cli import (
    build_codex_invocation,
    build_codex_login_status_invocation,
)


SESSION = "123e4567-e89b-12d3-a456-426614174000"
CODEX_HOME = Path("/srv/codex-runner/app")


class CodexCliInvocationTests(unittest.TestCase):
    def test_login_status_plan_uses_only_shared_codex_home(self) -> None:
        plan = build_codex_login_status_invocation(
            codex_path=Path("/opt/codex/bin/codex"),
            codex_home=CODEX_HOME,
        )
        self.assertEqual(
            (
                "/opt/codex/bin/codex",
                "-c",
                'forced_login_method="chatgpt"',
                "-c",
                'cli_auth_credentials_store="file"',
                "login",
                "status",
            ),
            plan.argv,
        )
        self.assertEqual(str(CODEX_HOME), plan.environment["CODEX_HOME"])
        self.assertNotIn("OPENAI_API_KEY", plan.environment)
        self.assertNotIn("GITHUB_TOKEN", plan.environment)

    def test_initial_plan_uses_stdin_and_contains_no_github_environment(self) -> None:
        plan = build_codex_invocation(
            codex_path=Path("/usr/local/bin/codex"),
            repository_directory=Path("/srv/tasks/issue-1/repo"),
            codex_home=CODEX_HOME,
            output_schema=Path("/srv/codex-runner/etc/result.schema.json"),
        )
        self.assertEqual("-", plan.argv[-1])
        self.assertIn("--json", plan.argv)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", plan.argv)
        self.assertIn('forced_login_method="chatgpt"', plan.argv)
        self.assertIn('cli_auth_credentials_store="file"', plan.argv)
        self.assertNotIn("resume", plan.argv)
        self.assertTrue(plan.reads_prompt_from_stdin)
        self.assertEqual(str(CODEX_HOME), plan.environment["CODEX_HOME"])
        self.assertFalse(any("GITHUB" in name or name == "GH_TOKEN" for name in plan.environment))

    def test_resume_plan_names_exact_session_and_never_uses_last_or_ephemeral(self) -> None:
        plan = build_codex_invocation(
            codex_path=Path("/usr/local/bin/codex"),
            repository_directory=Path("/srv/tasks/issue-1/repo"),
            codex_home=CODEX_HOME,
            output_schema=Path("/srv/codex-runner/etc/result.schema.json"),
            session_id=SESSION,
        )
        resume_index = plan.argv.index("resume")
        self.assertEqual(SESSION, plan.argv[resume_index + 1])
        self.assertNotIn("--last", plan.argv)
        self.assertNotIn("--ephemeral", plan.argv)

    def test_rejects_relative_paths_and_noncanonical_session(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute path"):
            build_codex_invocation(
                codex_path=Path("codex"),
                repository_directory=Path("/srv/tasks/issue-1/repo"),
                codex_home=CODEX_HOME,
                output_schema=Path("/srv/codex-runner/etc/result.schema.json"),
            )
        with self.assertRaisesRegex(ValueError, "canonical UUID"):
            build_codex_invocation(
                codex_path=Path("/usr/local/bin/codex"),
                repository_directory=Path("/srv/tasks/issue-1/repo"),
                codex_home=CODEX_HOME,
                output_schema=Path("/srv/codex-runner/etc/result.schema.json"),
                session_id="last",
            )
        with self.assertRaisesRegex(ValueError, "normalized absolute"):
            build_codex_invocation(
                codex_path=Path("/usr/local/bin/codex"),
                repository_directory=Path("/srv/tasks/../other/repo"),
                codex_home=CODEX_HOME,
                output_schema=Path("/srv/codex-runner/etc/result.schema.json"),
            )


if __name__ == "__main__":
    unittest.main()
