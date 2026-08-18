# Live test evidence

> The Codex Cloud-oriented sections are retained as historical evidence only. `exec:cloud` and the
> Cloud Environment are not part of the current SSH CLI target architecture.

## SSH CLI Dispatcher and Publisher fixture — 2026-08-18

This section records the first bounded write-enabled happy-path sweep. It is evidence for the
specific observed run, not authorization for unattended production use or evidence that live crash
recovery and Slack delivery have been proven.

### Admission and immutable identity

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#2`](https://github.com/longwdl/codex-dispatcher-fixture/issues/2).
- Initial labels were `agent:ready`, `exec:ssh-cli`, and `priority:p1`.
- The strict task spec allowed only the README marker value to change to
  `ssh-publisher-phase-d-v1`; Codex was explicitly forbidden to push, create a PR, merge, or deploy.
- GitHub Issue node ID: `I_kwDOT3NfX88AAAABNPZJlw`.
- WorkItem: `wi_70da53f08c0e7d96a08901b8`.
- Task branch: `codex/issue-2-70da53f08c0e`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-2`.

Before the write, `ssh-preflight` selected exactly Issue `#2` as `ready_candidate`, reported no
rejections, and stated `authorizes_apply=false`. The task branch and matching PR did not exist;
Fixture `main` was `b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`. The existing SQLite database passed
`integrity_check`, and a mode-`0600` online backup preserved the earlier WorkItem/session/Turn
history before additive migrations or new state were written.

### One write-enabled sweep

The exact entry point required both `--apply` and
`CODEX_DISPATCHER_ENABLE_SSH_WRITES=1`. It returned success for one Turn:

- Turn: `turn_c4e2249059de4b139deff19867c68a27`, number `1`;
- Codex session: `01a01583-2133-7a82-bd40-62278e57bd0d`;
- Runner result: `finished/completed`;
- checkpoint: `071ec769c63b8ab594865611cdc8af46ddd07b7f`;
- resulting WorkItem state: `review`.

The Dispatcher then published only that exact checkpoint to the deterministic task branch, created
Draft PR [`#3`](https://github.com/longwdl/codex-dispatcher-fixture/pull/3), updated the one fixed
Issue status comment, and projected Issue `#2` to `agent:review`. The PR uses base `main`, the exact
task branch as head, and the same checkpoint SHA. SQLite binds PR number `3` and the published SHA
to the same WorkItem and contains exactly one finished Turn for this Issue.

### Independent read-back

- The remote task branch resolved to the checkpoint SHA; `main` remained at its original SHA.
- The PR diff contained only `README.md`, with one insertion and one deletion inside the allowed
  marker; the marker boundaries were unchanged.
- GitHub Actions run [`32155421239`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32155421239)
  was `completed/success` for workflow `fixture`, event `pull_request`, the exact task branch, and
  the exact checkpoint SHA.
- A following read-only `ssh-preflight` returned `idle` with no candidate or recovery action.
- No merge, deployment, release, main-branch update, Slack delivery, or second Codex Turn occurred.

### Second write-enabled sweep

After a new mode-`0600` online SQLite backup passed `integrity_check`, the same double-opt-in
`ssh-run-once` command was executed again. It completed in 3.36 seconds with `status=idle` and null
Issue, WorkItem, and Turn identifiers.

Independent before/after reads proved:

- SQLite remained at two WorkItems and four total Turns;
- Issue `#2` retained the same `review` WorkItem row, PR binding, published SHA, session ID, and
  `updated_at` value;
- it retained exactly one finished Turn with the same ID, number, result, checkpoint, and
  `updated_at` value;
- Issue labels, comment count, and `updated_at` value did not change;
- exactly one Draft PR remained, with the same `updated_at` value and head SHA;
- task-branch and `main` refs did not move;
- the Actions query still returned exactly the original successful workflow run;
- a final read-only `ssh-preflight` again returned `idle`.

This proves the completed happy-path is idle on an immediate repeated sweep. It does not prove the
separate crash/lost-receipt recovery paths.

The fine-grained PAT could list Actions runs but could not read check runs through either the
GraphQL `statusCheckRollup` field or the REST Checks endpoint. CI success is therefore evidenced by
the accessible Actions workflow run, not inferred from those denied check APIs. Live
push/PR/comment lost-receipt injection remains outstanding.

## SSH CLI Runner fixture — 2026-08-18

This section records the first real SSH CLI protocol run. The Fixture Issue remained open with
`agent:paused`, `exec:cloud`, and `priority:p2`; the test invoked the new protocol directly and did
not claim that end-to-end SSH scheduler label routing exists.

### Release and transport

- Current Runner release: `057226b185dfef70aa1b09bb54561b357f094743`.
- Fixed remote entrypoint: `/srv/codex-runner/bin/codex-runner-v1`.
- Fixed configuration selected Git `/usr/bin/git`, Codex CLI 0.147.0, shared
  `CODEX_HOME=/srv/codex-runner`, and `/srv/codex-runner/work-items`.
- The real `SshRunnerTransport` used the protected host key and identity plus the explicitly
  approved Mac Fixture `assh` proxy shape. An unknown STATUS request was definitively rejected.
- `codex login status` returned `Logged in using ChatGPT`; no API-key billing path was used.
- The release, wrapper, and configuration are user-owned in this Fixture because `ecs-user` has no
  passwordless sudo. Older releases remain available for symlink rollback. This is not the
  production ownership model.

### Migrated 1:1:1 binding and PREPARE

The legacy SQLite anchor, local mirror, GitHub `main`, and the exact remote task ref all agreed on
SHA `b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`. The existing branch was imported rather than
re-derived:

- WorkItem: `wi_3a97e3d99c30bdcbb50501dd`;
- GitHub Issue: `longwdl/codex-dispatcher-fixture#1`;
- task branch: `codex/issue-1-8e3775879000` with `task_branch_source=migrated`;
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-1`.

Real PREPARE transferred a 4,804-byte self-contained Git bundle, verified its SHA-256, and created
one clean local repository with no remote. The repository branch and HEAD matched the migrated
binding and base SHA.

### START and resume evidence

All three Turns used the same WorkItem, branch, directory, input HEAD, and Codex session
`01a01486-0eee-7511-85ee-abe46c8bfb8b`:

1. Turn 1 created the session. Its Schema-valid Agent output used `status=blocked` with one question,
   conflicting with the stricter domain rule, so the Runner returned `agent_result_invalid` and no
   checkpoint.
2. Prompt and Schema descriptions were aligned without weakening the parser. Turn 2 resumed the
   exact session and produced a domain-valid final result in the rollout, but the bounded JSONL
   parser returned `codex_output_invalid`. No raw provider output was persisted or forwarded.
3. Static JSONL failure classification was added and deployed. Turn 3 again resumed the exact
   session; it completed with a parsed business result `blocked`, an unchanged output HEAD, and no
   error code. The blocker accurately reported that the isolated Runner cannot prove GitHub
   Timeline and dispatcher dry-run acceptance evidence.

Turn 2's JSONL anomaly did not reproduce in Turn 3, so it remains an observed transient rather
than a confirmed root cause. Future occurrences return a specific bounded JSONL error code.

Post-checks proved the worktree clean, no Git remote, the global lock acquirable, the GitHub task
branch unchanged at the base SHA, `last_published_sha` unset, and no Publisher, PR, Slack, merge, or
deployment action triggered by the task. The
[official Codex non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode)
confirms that `codex exec --json` emits JSONL, `--output-schema` constrains the final JSON, and an
explicit session ID can be used with `codex exec resume`.

## Historical Codex Cloud fixture — 2026-08-13

Snapshot date: 2026-08-13. This file records non-secret evidence from the dedicated private
fixture. It is not a substitute for repeatable automated tests.

## Repository initialization

- Source repository: `longwdl/codex_cloud_task_scheduler`, private, default branch `main`.
- Fixture repository: `longwdl/codex-dispatcher-fixture`, private, default branch `main`.
- Fixture baseline commit: `b877bdf801afbc1f6edcdca687c8aea8c0532a66`.
- Safe Markdown Issue template commit: `b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`.
- Fixture workflow run `31701519356`: completed successfully.
- All 13 `agent:*`, `exec:cloud`, and `priority:*` labels were present.

## Phase 2 admission contract

Fixture Issue `#1` was created with `agent:paused`, `exec:cloud`, and `priority:p2`.

1. With `agent:paused`, live `run-once --dry-run` returned no selected tasks.
2. Maintainer `longwdl` replaced `agent:paused` with `agent:ready`.
3. GitHub Timeline recorded `longwdl` as the Ready-label actor.
4. Live dry-run selected exactly Fixture Issue `#1`.
5. The Issue was restored to `agent:paused`; live dry-run again returned an empty queue.

No branch, pull request, Codex Cloud task, merge, or deployment was created by this test.

## Local task-branch recovery contract

The branch publication protocol is tested against a temporary local bare Git repository. The test
simulates a process failure after the remote branch is created but before SQLite advances the run,
then verifies that retry reuses the exact persisted branch and Base SHA without another write. A
second test advances `main` before recovery and verifies that the persisted Base SHA remains the
task-branch anchor. These tests do not access either configured GitHub repository.

## Phase 3 GitHub write primitive contract

Using the real adapter against Fixture Issue `#1`:

1. The adapter replaced the single state with `agent:dispatching` and verified the reread state.
2. It created one marker-based run comment and then updated the same comment idempotently.
3. It restored the Issue to `agent:paused` and verified the reread state.
4. The temporary test comment was deleted; no matching dispatcher test comment remains.

The test did not invoke Codex Cloud, create a branch, or create a pull request.

## Phase 3 task-branch publication contract

The recoverable branch service ran against Fixture Issue `#1` using deterministic run ID
`fixture-issue-1-branch-contract-v1`. It recorded Base SHA
`b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`, created branch
`codex/issue-1-8e3775879000` at that exact commit, then reran and reused the existing branch. The
persisted run ended in `branch_prepared`, `head_sha` equalled `base_sha`, and SQLite
`PRAGMA integrity_check` returned `ok`.

No Issue label/comment, pull request, Codex Cloud task, merge, or deployment was created. The branch
is intentionally retained as the stable input for the later Cloud submission contract test.

## Local Git workspace contract

The hardened Git workspace component cloned the private Fixture through HTTPS into a temporary
local mirror, fetched `main` without tags or recursive submodules, resolved base SHA
`b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`, and created local branch
`agent/local-read-only-contract` in an isolated worktree. No branch was pushed and no remote Git
reference changed.

## Accepted fixture risk

GitHub rulesets are unavailable for this private personal-account repository on the current plan.
The owner accepted that residual risk for this credential-free fixture only. This exception does
not apply to a production-connected repository.
