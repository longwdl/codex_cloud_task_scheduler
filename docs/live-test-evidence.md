# Live test evidence

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
