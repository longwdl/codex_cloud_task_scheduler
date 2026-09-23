from __future__ import annotations

import json
import unittest
from pathlib import Path

from codex_dispatcher.executors.codex_cli import (
    build_codex_invocation,
    build_codex_login_status_invocation,
)
from codex_dispatcher.runner_policy import PolicyBundle


SESSION = "123e4567-e89b-12d3-a456-426614174000"
CODEX_HOME = Path("/srv/codex-runner/app")
PROXY_URL = "http://127.0.0.1:3128"


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

    def test_runner_policy_profile_follows_resolved_generation_role(self) -> None:
        root = Path(__file__).resolve().parents[1] / "config" / "runner-codex-policy"
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        bundle = PolicyBundle.load(root, manifest["policy_digest"])
        for role, expected_profile in (("implementation", None), ("ci_repair", "repair"), ("audit", "audit")):
            for session_id in (None, SESSION):
                with self.subTest(role=role, session_id=session_id):
                    policy = bundle.runtime_policy(role)
                    plan = build_codex_invocation(
                        codex_path=Path("/usr/local/bin/codex"),
                        repository_directory=Path("/srv/tasks/issue-1/repo"),
                        codex_home=CODEX_HOME,
                        output_schema=Path("/srv/codex-runner/etc/result.schema.json"),
                        session_id=session_id,
                        enable_runner_policy=True,
                        agent_policy=policy,
                    )
                    expected_flags = ("--strict-config",) if expected_profile is None else ("--strict-config", "--profile", expected_profile)
                    self.assertEqual(expected_flags, plan.argv[5:5 + len(expected_flags)])
                    self.assertNotIn("--model", plan.argv)
                    self.assertNotIn("multi_agent", plan.argv)
                    self.assertLess(plan.argv.index("--strict-config"), plan.argv.index("exec"))
                    if session_id is None:
                        self.assertNotIn("resume", plan.argv)
                    else:
                        resume = plan.argv.index("resume")
                        self.assertEqual(SESSION, plan.argv[resume + 1])

    def test_proxy_plan_sets_only_one_fixed_credential_free_endpoint(self) -> None:
        plan = build_codex_invocation(
            codex_path=Path("/usr/local/bin/codex"),
            repository_directory=Path("/srv/tasks/issue-1/repo"),
            codex_home=CODEX_HOME,
            output_schema=Path("/srv/codex-runner/etc/result.schema.json"),
            egress_proxy_url=PROXY_URL,
        )

        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            self.assertEqual(PROXY_URL, plan.environment[name])
        self.assertEqual("", plan.environment["NO_PROXY"])
        self.assertEqual("", plan.environment["no_proxy"])
        self.assertFalse(any("TOKEN" in name or "AUTH" in name for name in plan.environment))

    def test_proxy_plan_rejects_credentials_paths_and_noncanonical_urls(self) -> None:
        for proxy_url in (
            "https://127.0.0.1:3128",
            "http://user:secret@127.0.0.1:3128",
            "http://127.0.0.1:3128/path",
            "http://127.0.0.1",
            "http://127.0.0.1:0",
            "http://PROXY.invalid:3128",
            "http://[::1]:3128",
        ):
            with self.subTest(proxy_url=proxy_url), self.assertRaisesRegex(
                ValueError, "egress_proxy_url"
            ):
                build_codex_login_status_invocation(
                    codex_path=Path("/usr/local/bin/codex"),
                    codex_home=CODEX_HOME,
                    egress_proxy_url=proxy_url,
                )

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
