from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from codex_dispatcher.ci_evidence import CiEvidenceError, RequiredCheckStatus
from codex_dispatcher.command_runner import CommandResult
from codex_dispatcher.github_actions_evidence import GitHubActionsEvidenceImporter
from codex_dispatcher.github_api_metrics import GitHubApiMetricsCollector


REPOSITORY = "owner/repo"
BRANCH = "codex/issue-42-aaaaaaaaaaaa"
HEAD = "a" * 40
TOKEN = "github-secret-fixture"


def ref_result(sha: str = HEAD) -> CommandResult:
    return CommandResult(
        0,
        json.dumps(
            {"ref": f"refs/heads/{BRANCH}", "sha": sha, "type": "commit"}
        ),
        "",
    )


def run_payload(
    *,
    name: str = "tests",
    run_id: int = 101,
    workflow_id: int = 201,
    head_sha: str = HEAD,
    status: str = "completed",
    conclusion: str | None = "success",
    event: str = "pull_request",
) -> dict[str, object]:
    return {
        "conclusion": conclusion,
        "created_at": "2026-08-22T08:00:00Z",
        "event": event,
        "head_branch": BRANCH,
        "head_repository": REPOSITORY,
        "head_sha": head_sha,
        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        "id": run_id,
        "name": name,
        "repository": REPOSITORY,
        "run_attempt": 1,
        "status": status,
        "updated_at": "2026-08-22T08:01:00Z",
        "workflow_id": workflow_id,
    }


def lines_result(*runs: dict[str, object]) -> CommandResult:
    return CommandResult(
        0,
        json.dumps({"total_count": len(runs), "runs": list(runs)}),
        "",
    )


class GitHubActionsEvidenceImporterTests(unittest.TestCase):
    def importer(self) -> GitHubActionsEvidenceImporter:
        return GitHubActionsEvidenceImporter(gh_path="/usr/bin/gh", token=TOKEN)

    def import_with(self, *results: CommandResult):
        with patch(
            "codex_dispatcher.github_actions_evidence.run_command",
            side_effect=results,
        ) as runner:
            snapshot = self.importer().import_for_head(
                repository=REPOSITORY,
                task_branch=BRANCH,
                head_sha=HEAD,
                required_checks=("tests", "integration"),
                observed_at="2026-08-22T08:02:00Z",
            )
        return snapshot, runner

    def test_imports_only_exact_head_and_preserves_missing_check(self) -> None:
        snapshot, runner = self.import_with(
            ref_result(),
            lines_result(
                run_payload(head_sha="b" * 40, run_id=100),
                run_payload(),
            ),
            ref_result(),
        )

        self.assertEqual(HEAD, snapshot.remote_ref_sha)
        self.assertEqual(
            [RequiredCheckStatus.PASSED, RequiredCheckStatus.NOT_OBSERVED],
            [item.status for item in snapshot.required_checks],
        )
        self.assertEqual(101, snapshot.required_checks[0].run.run_id)  # type: ignore[union-attr]
        actions_argv = runner.call_args_list[1].args[0]
        self.assertIn("branch=codex%2Fissue-42-aaaaaaaaaaaa", actions_argv[-3])
        for call in runner.call_args_list:
            self.assertEqual(TOKEN, call.kwargs["env"]["GH_TOKEN"])
            self.assertEqual((TOKEN,), call.kwargs["secrets"])

    def test_shared_metrics_include_all_three_actions_reads(self) -> None:
        collector = GitHubApiMetricsCollector()
        importer = GitHubActionsEvidenceImporter(
            gh_path="/usr/bin/gh",
            token=TOKEN,
            metrics_collector=collector,
        )
        with patch(
            "codex_dispatcher.github_actions_evidence.run_command",
            side_effect=(ref_result(), lines_result(run_payload()), ref_result()),
        ):
            importer.import_for_head(
                repository=REPOSITORY,
                task_branch=BRANCH,
                head_sha=HEAD,
                required_checks=("tests",),
                observed_at="2026-08-22T08:02:00Z",
            )

        metrics = collector.snapshot(
            rate_limit_error="github_rate_limit_unavailable"
        )
        self.assertEqual(3, metrics.command_count)
        self.assertEqual(3, metrics.read_count)
        self.assertEqual(0, metrics.write_count)
        self.assertEqual(0, metrics.failure_count)

    def test_maps_incomplete_and_non_successful_runs(self) -> None:
        pending, _ = self.import_with(
            ref_result(),
            lines_result(run_payload(status="in_progress", conclusion=None)),
            ref_result(),
        )
        failed, _ = self.import_with(
            ref_result(),
            lines_result(run_payload(conclusion="failure")),
            ref_result(),
        )

        self.assertEqual(RequiredCheckStatus.PENDING, pending.required_checks[0].status)
        self.assertEqual(RequiredCheckStatus.FAILED, failed.required_checks[0].status)

    def test_rejects_ref_mismatch_or_drift(self) -> None:
        with patch(
            "codex_dispatcher.github_actions_evidence.run_command",
            return_value=ref_result("b" * 40),
        ):
            with self.assertRaisesRegex(CiEvidenceError, "does not equal"):
                self.importer().import_for_head(
                    repository=REPOSITORY,
                    task_branch=BRANCH,
                    head_sha=HEAD,
                    required_checks=("tests",),
                    observed_at="2026-08-22T08:02:00Z",
                )

        with patch(
            "codex_dispatcher.github_actions_evidence.run_command",
            side_effect=(ref_result(), lines_result(run_payload()), ref_result("b" * 40)),
        ):
            with self.assertRaisesRegex(CiEvidenceError, "changed during"):
                self.importer().import_for_head(
                    repository=REPOSITORY,
                    task_branch=BRANCH,
                    head_sha=HEAD,
                    required_checks=("tests",),
                    observed_at="2026-08-22T08:02:00Z",
                )

    def test_rejects_ambiguous_same_name_runs_and_malformed_data(self) -> None:
        with patch(
            "codex_dispatcher.github_actions_evidence.run_command",
            side_effect=(
                ref_result(),
                lines_result(run_payload(), run_payload(run_id=102, workflow_id=202)),
                ref_result(),
            ),
        ):
            with self.assertRaisesRegex(CiEvidenceError, "ambiguously"):
                self.importer().import_for_head(
                    repository=REPOSITORY,
                    task_branch=BRANCH,
                    head_sha=HEAD,
                    required_checks=("tests",),
                    observed_at="2026-08-22T08:02:00Z",
                )

        with patch(
            "codex_dispatcher.github_actions_evidence.run_command",
            side_effect=(
                ref_result(),
                lines_result(run_payload(event="workflow_dispatch")),
                ref_result(),
            ),
        ):
            with self.assertRaisesRegex(CiEvidenceError, "non-pull_request"):
                self.importer().import_for_head(
                    repository=REPOSITORY,
                    task_branch=BRANCH,
                    head_sha=HEAD,
                    required_checks=("tests",),
                    observed_at="2026-08-22T08:02:00Z",
                )

        malformed = run_payload()
        malformed.pop("workflow_id")
        with patch(
            "codex_dispatcher.github_actions_evidence.run_command",
            side_effect=(ref_result(), lines_result(malformed), ref_result()),
        ):
            with self.assertRaisesRegex(CiEvidenceError, "invalid"):
                self.importer().import_for_head(
                    repository=REPOSITORY,
                    task_branch=BRANCH,
                    head_sha=HEAD,
                    required_checks=("tests",),
                    observed_at="2026-08-22T08:02:00Z",
                )

    def test_command_failures_never_include_token_or_provider_stderr(self) -> None:
        collector = GitHubApiMetricsCollector()
        importer = GitHubActionsEvidenceImporter(
            gh_path="/usr/bin/gh", token=TOKEN, metrics_collector=collector
        )
        with patch(
            "codex_dispatcher.github_actions_evidence.run_command",
            return_value=CommandResult(1, "", f"denied for {TOKEN}"),
        ):
            with self.assertRaises(CiEvidenceError) as captured:
                importer.import_for_head(
                    repository=REPOSITORY,
                    task_branch=BRANCH,
                    head_sha=HEAD,
                    required_checks=("tests",),
                    observed_at="2026-08-22T08:02:00Z",
                )
        self.assertNotIn(TOKEN, str(captured.exception))
        self.assertNotIn("denied", str(captured.exception))
        metrics = collector.snapshot(
            rate_limit_error="github_rate_limit_unavailable"
        )
        self.assertEqual(1, metrics.command_count)
        self.assertEqual(1, metrics.failure_count)

    def test_rejects_incomplete_or_inconsistent_pagination(self) -> None:
        incomplete = CommandResult(
            0,
            json.dumps({"total_count": 2, "runs": [run_payload()]}),
            "",
        )
        with patch(
            "codex_dispatcher.github_actions_evidence.run_command",
            side_effect=(ref_result(), incomplete),
        ):
            with self.assertRaisesRegex(CiEvidenceError, "every branch run"):
                self.importer().import_for_head(
                    repository=REPOSITORY,
                    task_branch=BRANCH,
                    head_sha=HEAD,
                    required_checks=("tests",),
                    observed_at="2026-08-22T08:02:00Z",
                )

        malformed_page = CommandResult(0, json.dumps({"runs": []}), "")
        with patch(
            "codex_dispatcher.github_actions_evidence.run_command",
            side_effect=(ref_result(), malformed_page),
        ):
            with self.assertRaisesRegex(CiEvidenceError, "invalid fields"):
                self.importer().import_for_head(
                    repository=REPOSITORY,
                    task_branch=BRANCH,
                    head_sha=HEAD,
                    required_checks=("tests",),
                    observed_at="2026-08-22T08:02:00Z",
                )


if __name__ == "__main__":
    unittest.main()
