from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.command_runner import CommandResult
from codex_dispatcher.trackers.base import (
    DraftPullRequestRequest,
    IssueCloseReason,
    PullRequestState,
    TaskState,
)
from codex_dispatcher.trackers.github_cli import (
    GitHubCliTracker,
    GitHubCliTrackerError,
)


GH = "/opt/homebrew/bin/gh"
REPOSITORY = "owner/repo"


def result(payload: object) -> CommandResult:
    return CommandResult(0, json.dumps(payload), "")


def json_lines_result(*payloads: object) -> CommandResult:
    return CommandResult(0, "\n".join(json.dumps(payload) for payload in payloads), "")


def issue(*, number: int = 12, labels: object | None = None) -> dict[str, object]:
    return {
        "id": f"I_kwDOFixture{number}",
        "number": number,
        "title": "Safe task",
        "body": "Task body",
        "labels": labels if labels is not None else [label("agent:ready"), label("exec:ssh-cli")],
        "createdAt": "2026-08-13T00:00:00Z",
        "updatedAt": "2026-08-13T00:05:00Z",
        "state": "OPEN",
        "stateReason": None,
    }


def label(name: str) -> dict[str, object]:
    return {"id": f"id-{name}", "name": name, "description": None, "color": "abcdef"}


def pull_request(
    *,
    number: int = 7,
    branch: str = "codex/issue-12-abcdef123456",
    state: str = "OPEN",
    cross_repository: bool = False,
) -> dict[str, object]:
    return {
        "number": number,
        "url": f"https://github.com/{REPOSITORY}/pull/{number}",
        "headRefName": branch,
        "headRefOid": "a" * 40,
        "baseRefName": "main",
        "title": "Codex work for Issue #12",
        "isDraft": True,
        "state": state,
        "isCrossRepository": cross_repository,
    }


class GitHubCliTrackerTests(unittest.TestCase):
    def test_conditional_state_update_preserves_concurrent_discard(self) -> None:
        discarded = issue(
            labels=[label("agent:discard"), label("exec:ssh-cli")]
        )
        event = {
            "id": 987,
            "event": "labeled",
            "created_at": "2026-08-24T18:34:00Z",
            "label": {"name": "agent:discard"},
            "actor": {"login": "alice"},
        }
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[
                result(discarded),
                json_lines_result(),
                json_lines_result(event),
                result(discarded),
            ],
        ) as runner:
            observed = GitHubCliTracker(gh_path=GH).set_state(
                REPOSITORY,
                "12",
                TaskState.RUNNING,
                expected_state=TaskState.DISPATCHING,
            )

        self.assertIs(TaskState.DISCARD, observed.state)
        self.assertEqual("alice", observed.state_approved_by)
        self.assertEqual(4, runner.call_count)
        self.assertFalse(
            any("PATCH" in call.args[0] for call in runner.call_args_list)
        )

    def test_open_issue_empty_state_reason_is_normalized_but_closed_requires_reason(self) -> None:
        open_issue = issue()
        open_issue["stateReason"] = ""
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[result(open_issue), json_lines_result()],
        ):
            observed = GitHubCliTracker(gh_path=GH).get_task(REPOSITORY, "12")
        self.assertIsNotNone(observed)
        assert observed is not None
        self.assertTrue(observed.is_open)
        self.assertIsNone(observed.state_reason)

        closed_issue = dict(open_issue, state="CLOSED")
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=result(closed_issue),
        ):
            with self.assertRaisesRegex(GitHubCliTrackerError, "invalid issue state"):
                GitHubCliTracker(gh_path=GH).get_task(REPOSITORY, "12")

    def test_exact_pull_request_and_issue_closure_are_verified(self) -> None:
        branch = "codex/issue-12-abcdef123456"
        open_pr = pull_request(branch=branch)
        closed_pr = pull_request(branch=branch, state="CLOSED")
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[result([open_pr]), result({}), result([closed_pr])],
        ) as runner:
            observed_pr = GitHubCliTracker(gh_path=GH).close_pull_request(
                REPOSITORY,
                branch,
                expected_number=7,
                expected_head_sha="a" * 40,
            )
        self.assertIs(PullRequestState.CLOSED, observed_pr.state)
        self.assertIn(
            "/repos/owner/repo/pulls/7",
            runner.call_args_list[1].args[0],
        )

        completed = issue(
            labels=[label("agent:completed"), label("exec:ssh-cli")]
        )
        closed = dict(completed, state="CLOSED", stateReason="COMPLETED")
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[
                result(completed),
                json_lines_result(),
                result({}),
                result(closed),
                json_lines_result(),
            ],
        ) as runner:
            observed_issue = GitHubCliTracker(gh_path=GH).close_task(
                REPOSITORY,
                "12",
                expected_issue_node_id="I_kwDOFixture12",
                reason=IssueCloseReason.COMPLETED,
            )
        self.assertFalse(observed_issue.is_open)
        self.assertEqual("completed", observed_issue.state_reason)
        patch_argv = runner.call_args_list[2].args[0]
        self.assertIn("state=closed", patch_argv)
        self.assertIn("state_reason=completed", patch_argv)

    def test_terminal_branch_reads_and_deletes_only_an_exact_encoded_ref(self) -> None:
        branch = "codex/issue-12-abcdef123456"
        head = "a" * 40
        refs = [{"ref": f"refs/heads/{branch}", "object": {"type": "commit", "sha": head}}]
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=result(refs),
        ) as runner:
            observed = GitHubCliTracker(gh_path=GH).get_branch_head(
                REPOSITORY, branch
            )
        self.assertEqual(head, observed)
        self.assertTrue(
            any(
                "heads/codex%2Fissue-12-abcdef123456" in argument
                for argument in runner.call_args.args[0]
            )
        )

        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[result(refs), CommandResult(0, "", "")],
        ) as runner:
            GitHubCliTracker(gh_path=GH).delete_branch(REPOSITORY, branch, head)
        delete_argv = runner.call_args_list[1].args[0]
        self.assertEqual("DELETE", delete_argv[delete_argv.index("--method") + 1])
        self.assertTrue(
            any(
                "refs/heads/codex%2Fissue-12-abcdef123456" in argument
                for argument in delete_argv
            )
        )

    def test_terminal_branch_delete_rejects_a_changed_head_before_write(self) -> None:
        branch = "codex/issue-12-abcdef123456"
        refs = [
            {
                "ref": f"refs/heads/{branch}",
                "object": {"type": "commit", "sha": "b" * 40},
            }
        ]
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=result(refs),
        ) as runner:
            with self.assertRaisesRegex(GitHubCliTrackerError, "changed"):
                GitHubCliTracker(gh_path=GH).delete_branch(
                    REPOSITORY, branch, "a" * 40
                )
        runner.assert_called_once()

    def test_api_metrics_count_commands_and_report_core_and_graphql_budget(self) -> None:
        tracker = GitHubCliTracker(gh_path=GH)
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[
                result([]),
                result(
                    {
                        "resources": {
                            "core": {"remaining": 4990, "limit": 5000, "reset": 1800},
                            "graphql": {
                                "remaining": 4980,
                                "limit": 5000,
                                "reset": 1801,
                            },
                        }
                    }
                ),
            ],
        ):
            self.assertEqual((), tracker.list_ready_tasks(REPOSITORY))
            metrics = tracker.collect_api_metrics()

        self.assertEqual(2, metrics.command_count)
        self.assertEqual(2, metrics.read_count)
        self.assertEqual(0, metrics.write_count)
        self.assertEqual(0, metrics.failure_count)
        self.assertEqual(4990, metrics.core_remaining)
        self.assertEqual(4980, metrics.graphql_remaining)
        self.assertIsNone(metrics.rate_limit_error)

    def test_api_metrics_do_not_fail_a_sweep_when_budget_read_is_unavailable(self) -> None:
        tracker = GitHubCliTracker(gh_path=GH)
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=CommandResult(1, "", "provider failure"),
        ):
            metrics = tracker.collect_api_metrics()
        self.assertEqual("github_rate_limit_unavailable", metrics.rate_limit_error)
        self.assertEqual(1, metrics.command_count)
        self.assertEqual(1, metrics.failure_count)

    def test_discard_state_requires_stable_label_event_identity(self) -> None:
        discarded = issue(
            labels=[label("agent:discard"), label("exec:ssh-cli")]
        )
        event = {
            "id": 987,
            "event": "labeled",
            "created_at": "2026-08-23T01:00:00Z",
            "label": {"name": "agent:discard"},
            "actor": {"login": "alice"},
        }
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[
                result([discarded]),
                json_lines_result(),
                json_lines_result(event),
                result(discarded),
            ],
        ):
            tasks = GitHubCliTracker(gh_path=GH).list_open_tasks(
                REPOSITORY, TaskState.DISCARD
            )
        self.assertEqual("alice", tasks[0].state_approved_by)
        self.assertEqual("987", tasks[0].state_approval_event_id)
        self.assertEqual("2026-08-23T01:00:00Z", tasks[0].state_approved_at)

        conflicting = dict(event, id=988, actor={"login": "bob"})
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[
                result([discarded]),
                json_lines_result(),
                json_lines_result(event, conflicting),
                result(discarded),
            ],
        ):
            ambiguous = GitHubCliTracker(gh_path=GH).list_open_tasks(
                REPOSITORY, TaskState.DISCARD
            )
        self.assertIsNone(ambiguous[0].state_approved_by)
        self.assertIsNone(ambiguous[0].state_approval_event_id)

    def test_discard_snapshot_change_during_audit_fails_closed(self) -> None:
        discarded = issue(
            labels=[label("agent:discard"), label("exec:ssh-cli")]
        )
        revoked = dict(
            discarded,
            labels=[label("agent:blocked"), label("exec:ssh-cli")],
            updatedAt="2026-08-23T01:01:00Z",
        )
        event = {
            "id": 987,
            "event": "labeled",
            "created_at": "2026-08-23T01:00:00Z",
            "label": {"name": "agent:discard"},
            "actor": {"login": "alice"},
        }
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[
                result([discarded]),
                json_lines_result(),
                json_lines_result(event),
                result(revoked),
            ],
        ):
            with self.assertRaisesRegex(
                GitHubCliTrackerError, "snapshot changed"
            ):
                GitHubCliTracker(gh_path=GH).list_open_tasks(
                    REPOSITORY, TaskState.DISCARD
                )

    def test_list_ready_tasks_reads_fixed_argv_and_audits_latest_label_actor(self) -> None:
        calls: list[tuple[str, ...]] = []
        keyword_arguments: list[dict[str, object]] = []
        config_directories: list[str] = []

        def fake_run(argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            calls.append(argv)
            keyword_arguments.append(kwargs)
            environment = kwargs["env"]
            assert isinstance(environment, dict)
            config_directory = environment["GH_CONFIG_DIR"]
            assert isinstance(config_directory, str)
            self.assertTrue(Path(config_directory).is_dir())
            self.assertEqual(0o700, os.stat(config_directory).st_mode & 0o777)
            config_directories.append(config_directory)
            if argv[1:3] == ("issue", "list"):
                return result([issue()])
            return json_lines_result(
                {
                    "event": "labeled",
                    "created_at": "2026-08-13T01:00:00Z",
                    "label": {"name": "agent:ready"},
                    "actor": {"login": "alice"},
                },
                {
                    "event": "labeled",
                    "created_at": "2026-08-13T02:00:00Z",
                    "label": {"name": "agent:ready"},
                    "actor": {"login": "bob"},
                },
            )

        with patch("codex_dispatcher.trackers.github_cli.run_command", side_effect=fake_run):
            tasks = GitHubCliTracker(gh_path=GH, token="test-token").list_ready_tasks(REPOSITORY)

        self.assertEqual(1, len(tasks))
        self.assertEqual(TaskState.READY, tasks[0].state)
        self.assertEqual("bob", tasks[0].ready_approved_by)
        self.assertEqual("I_kwDOFixture12", tasks[0].issue_node_id)
        self.assertEqual("2026-08-13T00:05:00Z", tasks[0].updated_at)
        self.assertEqual((GH, "issue", "list"), calls[0][:3])
        self.assertIn("--repo", calls[0])
        self.assertIn(REPOSITORY, calls[0])
        self.assertEqual("api", calls[1][1])
        self.assertEqual("GET", calls[1][3])
        self.assertIn("/repos/owner/repo/issues/12/timeline?per_page=100", calls[1])
        self.assertIn("--jq", calls[1])
        self.assertNotIn("--slurp", calls[1])
        environment = keyword_arguments[0]["env"]
        assert isinstance(environment, dict)
        self.assertEqual("1", environment["GH_PROMPT_DISABLED"])
        self.assertEqual("test-token", environment["GH_TOKEN"])
        self.assertEqual(("test-token",), keyword_arguments[0]["secrets"])
        self.assertTrue(config_directories)
        self.assertTrue(all(not Path(path).exists() for path in config_directories))

    def test_list_open_tasks_reads_a_requested_recovery_state(self) -> None:
        dispatching = issue(
            labels=[label("agent:dispatching"), label("exec:ssh-cli")]
        )
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[result([dispatching]), json_lines_result()],
        ) as runner:
            tasks = GitHubCliTracker(gh_path=GH).list_open_tasks(
                REPOSITORY, TaskState.DISPATCHING
            )

        self.assertEqual((12,), tuple(task.issue_number for task in tasks))
        argv = runner.call_args_list[0].args[0]
        label_index = argv.index("--label")
        self.assertEqual("agent:dispatching", argv[label_index + 1])
        self.assertIn("open", argv)

        with self.assertRaises(TypeError):
            GitHubCliTracker(gh_path=GH).list_open_tasks(
                REPOSITORY, "dispatching"  # type: ignore[arg-type]
            )

    def test_missing_or_malformed_audit_data_is_untrusted_not_inferred(self) -> None:
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[
                result([issue()]),
                json_lines_result({
                    "event": "labeled",
                    "created_at": "2026-08-13T02:00:00Z",
                    "label": {"name": "agent:ready"},
                }),
            ],
        ):
            tasks = GitHubCliTracker(gh_path=GH).list_ready_tasks(REPOSITORY)
        self.assertIsNone(tasks[0].ready_approved_by)

    def test_list_comments_reads_paginated_bounded_snapshots(self) -> None:
        payload = [[
            {
                "id": 7,
                "node_id": "IC_kwDOFixture7",
                "user": {"login": "alice", "id": 1},
                "body": "/codex-context\nUse the existing parser",
                "created_at": "2026-08-13T01:00:00Z",
                "updated_at": "2026-08-13T01:01:00Z",
                "html_url": "https://example.invalid/comment/7",
            }
        ]]
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=result(payload),
        ) as runner:
            comments = GitHubCliTracker(gh_path=GH).list_comments(REPOSITORY, "12")

        self.assertEqual(1, len(comments))
        self.assertEqual("IC_kwDOFixture7", comments[0].comment_id)
        self.assertEqual("alice", comments[0].author)
        argv = runner.call_args.args[0]
        self.assertIn("--paginate", argv)
        self.assertIn("/repos/owner/repo/issues/12/comments?per_page=100", argv)

    def test_comment_missing_duplicate_or_unbounded_data_fails_closed(self) -> None:
        valid = {
            "node_id": "IC_kwDOFixture7",
            "user": {"login": "alice"},
            "body": "/codex-context\nSafe",
            "created_at": "2026-08-13T01:00:00Z",
            "updated_at": "2026-08-13T01:01:00Z",
        }
        payloads = (
            [[{key: value for key, value in valid.items() if key != "user"}]],
            [[valid, dict(valid)]],
            [[dict(valid, body="x" * 65_537)]],
            [[dict(valid, created_at="not-a-time")]],
        )
        for payload in payloads:
            with self.subTest(payload_size=len(str(payload))):
                with patch(
                    "codex_dispatcher.trackers.github_cli.run_command",
                    return_value=result(payload),
                ):
                    with self.assertRaises(GitHubCliTrackerError):
                        GitHubCliTracker(gh_path=GH).list_comments(REPOSITORY, "12")

    def test_more_than_one_agent_state_fails_closed(self) -> None:
        conflicting = issue(
            labels=[
                label("agent:ready"),
                label("agent:blocked"),
                label("exec:ssh-cli"),
            ]
        )
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=result([conflicting]),
        ) as runner:
            with self.assertRaisesRegex(GitHubCliTrackerError, "agent state"):
                GitHubCliTracker(gh_path=GH).list_ready_tasks(REPOSITORY)
        runner.assert_called_once()

    def test_malformed_issue_data_fails_closed(self) -> None:
        malformed = issue(labels=[{"name": "agent:ready", "color": "red"}])
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=result([malformed]),
        ):
            with self.assertRaisesRegex(GitHubCliTrackerError, "label"):
                GitHubCliTracker(gh_path=GH).list_ready_tasks(REPOSITORY)

    def test_get_task_and_json_command_failure(self) -> None:
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[result(issue()), json_lines_result()],
        ) as runner:
            task = GitHubCliTracker(gh_path=GH).get_task(REPOSITORY, "12")
        self.assertIsNotNone(task)
        self.assertEqual("12", task.task_id)
        self.assertEqual("view", runner.call_args_list[0].args[0][2])

        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=CommandResult(1, "", "denied"),
        ):
            with self.assertRaisesRegex(GitHubCliTrackerError, "exit code 1"):
                GitHubCliTracker(gh_path=GH).list_ready_tasks(REPOSITORY)

    def test_command_failure_does_not_include_token_or_provider_stderr(self) -> None:
        token = "github_pat_test_fixture"
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=CommandResult(1, "", f"denied for {token}"),
        ):
            with self.assertRaises(GitHubCliTrackerError) as caught:
                GitHubCliTracker(gh_path=GH, token=token).list_ready_tasks(REPOSITORY)
        self.assertNotIn(token, str(caught.exception))
        self.assertNotIn("denied", str(caught.exception))

    def test_path_repository_and_issue_id_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            GitHubCliTracker(gh_path="gh")
        with self.assertRaises(ValueError):
            GitHubCliTracker(gh_path=GH, token="")
        tracker = GitHubCliTracker(gh_path=Path(GH))
        for repository in ("owner/repo/extra", "owner/repo?per_page=1", "owner/repo name"):
            with self.assertRaises(ValueError):
                tracker.list_ready_tasks(repository)
        with self.assertRaises(ValueError):
            tracker.get_task(REPOSITORY, "012")

    def test_claim_rereads_updates_and_verifies_state(self) -> None:
        ready = issue()
        dispatching = issue(labels=[label("agent:dispatching"), label("exec:ssh-cli")])
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[
                result(ready),
                json_lines_result({
                    "event": "labeled",
                    "created_at": "2026-08-13T02:00:00Z",
                    "label": {"name": "agent:ready"},
                    "actor": {"login": "alice"},
                }),
                result({"number": 12}),
                result(dispatching),
                json_lines_result(),
            ],
        ) as runner:
            claimed = GitHubCliTracker(gh_path=GH).claim(
                REPOSITORY, "12", "worker", approved_by=("alice",)
            )

        self.assertTrue(claimed.claimed)
        self.assertIsNotNone(claimed.task)
        assert claimed.task is not None
        self.assertEqual(TaskState.DISPATCHING, claimed.task.state)
        edit_argv = runner.call_args_list[2].args[0]
        self.assertEqual((GH, "api", "--method", "PATCH"), edit_argv[:4])
        self.assertNotIn("labels[]=agent:ready", edit_argv)
        self.assertIn("labels[]=agent:dispatching", edit_argv)
        self.assertIn("labels[]=exec:ssh-cli", edit_argv)

    def test_claim_does_not_write_when_task_is_not_ready(self) -> None:
        paused = issue(labels=[label("agent:paused"), label("exec:ssh-cli")])
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[result(paused), json_lines_result()],
        ) as runner:
            claimed = GitHubCliTracker(gh_path=GH).claim(
                REPOSITORY, "12", "worker", approved_by=("alice",)
            )
        self.assertFalse(claimed.claimed)
        self.assertEqual(2, runner.call_count)

    def test_claim_requires_explicit_trusted_ready_approver(self) -> None:
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[result(issue()), json_lines_result({
                "event": "labeled",
                "created_at": "2026-08-13T02:00:00Z",
                "label": {"name": "agent:ready"},
                "actor": {"login": "mallory"},
            })],
        ) as runner:
            claimed = GitHubCliTracker(gh_path=GH).claim(
                REPOSITORY, "12", "worker", approved_by=("alice",)
            )
        self.assertFalse(claimed.claimed)
        self.assertEqual("ready approval is not trusted", claimed.reason)
        self.assertEqual(2, runner.call_count)

    def test_run_comment_is_created_or_updated_idempotently(self) -> None:
        tracker = GitHubCliTracker(gh_path=GH)
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[result([[]]), result({"id": 7})],
        ) as runner:
            tracker.upsert_run_comment(REPOSITORY, "12", "run-1:started", "Started")
        create_argv = runner.call_args_list[1].args[0]
        self.assertIn("POST", create_argv)
        self.assertIn("body=<!-- codex-dispatcher:run-1:started -->\nStarted", create_argv)

        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            side_effect=[
                result([[{
                    "id": 7,
                    "body": "<!-- codex-dispatcher:run-1:started -->\nOld",
                }]]),
                result({"id": 7}),
            ],
        ) as runner:
            tracker.upsert_run_comment(REPOSITORY, "12", "run-1:started", "Updated")
        update_argv = runner.call_args_list[1].args[0]
        self.assertIn("PATCH", update_argv)
        self.assertIn("/repos/owner/repo/issues/comments/7", update_argv)

    def test_ambiguous_comment_marker_fails_before_write(self) -> None:
        signature = "<!-- codex-dispatcher:run-1:started -->"
        comments = [[{"id": 7, "body": signature}, {"id": 8, "body": signature}]]
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=result(comments),
        ) as runner:
            with self.assertRaisesRegex(GitHubCliTrackerError, "multiple"):
                GitHubCliTracker(gh_path=GH).upsert_run_comment(
                    REPOSITORY, "12", "run-1:started", "body"
                )
        runner.assert_called_once()

    def test_duplicate_comment_ids_fail_closed(self) -> None:
        comments = [[{"id": 7, "body": "one"}, {"id": 7, "body": "two"}]]
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=result(comments),
        ) as runner:
            with self.assertRaisesRegex(GitHubCliTrackerError, "duplicate ids"):
                GitHubCliTracker(gh_path=GH).upsert_run_comment(
                    REPOSITORY, "12", "run-1:started", "body"
                )
        runner.assert_called_once()

    def test_finds_the_only_same_repository_pr_by_exact_branch(self) -> None:
        branch = "codex/issue-12-abcdef123456"
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=result([pull_request(branch=branch)]),
        ) as runner:
            observed = GitHubCliTracker(gh_path=GH).find_pr_by_branch(
                REPOSITORY,
                branch,
            )

        assert observed is not None
        self.assertEqual(7, observed.number)
        self.assertEqual("main", observed.base_branch)
        self.assertIs(PullRequestState.OPEN, observed.state)
        self.assertEqual("a" * 40, observed.head_sha)
        argv = runner.call_args.args[0]
        self.assertEqual((GH, "pr", "list"), argv[:3])
        self.assertIn("all", argv)
        self.assertEqual(branch, argv[argv.index("--head") + 1])

    def test_ambiguous_or_cross_repository_pr_list_fails_closed(self) -> None:
        branch = "codex/issue-12-abcdef123456"
        for payload in (
            [pull_request(branch=branch), pull_request(number=8, branch=branch)],
            [pull_request(branch=branch, cross_repository=True)],
        ):
            with self.subTest(count=len(payload)):
                with patch(
                    "codex_dispatcher.trackers.github_cli.run_command",
                    return_value=result(payload),
                ):
                    with self.assertRaises(GitHubCliTrackerError):
                        GitHubCliTracker(gh_path=GH).find_pr_by_branch(
                            REPOSITORY,
                            branch,
                        )

    def test_creates_draft_pr_with_fixed_noninteractive_arguments(self) -> None:
        branch = "codex/issue-12-abcdef123456"
        request = DraftPullRequestRequest(
            REPOSITORY,
            branch,
            "main",
            "Codex work for Issue #12",
            "Review and merge remain manual.",
        )
        with patch(
            "codex_dispatcher.trackers.github_cli.run_command",
            return_value=CommandResult(
                0,
                "https://github.com/owner/repo/pull/7\n",
                "",
            ),
        ) as runner:
            created = GitHubCliTracker(
                gh_path=GH,
                token="github_pat_fixture_only",
            ).create_draft_pr(request)

        self.assertEqual(7, created.number)
        argv = runner.call_args.args[0]
        self.assertEqual((GH, "pr", "create"), argv[:3])
        self.assertEqual(branch, argv[argv.index("--head") + 1])
        self.assertIn("--draft", argv)
        self.assertIn("--no-maintainer-edit", argv)
        self.assertNotIn("--fill", argv)
        environment = runner.call_args.kwargs["env"]
        self.assertEqual("1", environment["GH_PROMPT_DISABLED"])
        self.assertEqual("github_pat_fixture_only", environment["GH_TOKEN"])
        self.assertIn("GH_CONFIG_DIR", environment)

    def test_draft_pr_creation_rejects_protected_head_before_write(self) -> None:
        tracker = GitHubCliTracker(gh_path=GH)
        with patch("codex_dispatcher.trackers.github_cli.run_command") as runner:
            with self.assertRaises(ValueError):
                tracker.create_draft_pr(
                    DraftPullRequestRequest(
                        REPOSITORY,
                        "main",
                        "main",
                        "Unsafe",
                        "Unsafe",
                    )
                )
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
