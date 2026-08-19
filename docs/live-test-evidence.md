# Live test evidence

> The Codex Cloud-oriented sections are retained as historical evidence only. `exec:cloud` and the
> Cloud Environment are not part of the current SSH CLI target architecture.

## SSH CLI human-merge completion projection fixture — 2026-08-19

This fixture completes the lifecycle of Fixture Issue
[`#12`](https://github.com/longwdl/codex-dispatcher-fixture/issues/12) after its START-receipt
recovery. The dispatcher did not merge the pull request. The maintainer independently reviewed the
README-only diff, confirmed Actions run
[`32169064603`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32169064603) was
successful at exact checkpoint `41e67598b506dcbfeac00e5871a812e6e9874078`, marked Draft PR
[`#13`](https://github.com/longwdl/codex-dispatcher-fixture/pull/13) ready, and explicitly merged it.

Before the completion sweep, read-only preflight selected `complete_merged_work_item` for the exact
Issue, WorkItem `wi_594a1305a087ff78a0ab32f8`, and PR `#13`. The normal double-opt-in sweep then:

- required repository `longwdl/codex-dispatcher-fixture`, base `main`, task branch
  `codex/issue-12-594a1305a087`, and PR head SHA to match the persisted binding;
- committed the irreversible local WorkItem state `completed` before projecting GitHub state;
- updated the one fixed status comment to `agent:completed` at `2026-08-19T01:47:34Z`;
- applied the `agent:completed` label at `2026-08-19T01:47:37Z`, after the comment update;
- left the Issue open and retained the task branch.

Independent read-back found exactly one WorkItem and one finished/completed Turn. SQLite retained
PR `#13` and the exact checkpoint. The task branch still resolved to that checkpoint, while `main`
resolved to merge commit `7fe0a9a5d51f4438423744ffb199563a0bcd4d9a`. No second workflow run,
Turn, session, branch, or PR appeared. A subsequent read-only preflight and a repeated write-enabled
sweep both returned idle.

This proves the normal AC-055 completion path and ordered projection. It does not prove lost-receipt
recovery for the completion comment or label, automatic merging, Issue closing, branch deletion, or
deployment; those actions remain forbidden or separately covered only by offline fault tests.

## SSH CLI exact Dispatcher process-kill recovery fixture — 2026-08-19

This fixture covers the post-claim/pre-persistence operating-system boundary with a real
`SIGKILL`. It does not select a process by name. A dedicated parent starts one exact child argv in a
new session, waits for an identity-bound private-pipe handshake emitted only after GitHub claim, and
kills only that verified still-running child.

### Kill boundary

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#10`](https://github.com/longwdl/codex-dispatcher-fixture/issues/10),
  node ID `I_kwDOT3NfX88AAAABNQOefw`.
- The successful process fixture returned `child_exit_code=-9`, `termination_signal=SIGKILL`,
  `local_work_item_persisted=false`, and `runner_reached=false`.
- SQLite passed `integrity_check`, contained no WorkItem for Issue `#10` and no active Turn, while
  the Issue was exactly `agent:dispatching`.
- Read-only preflight selected `ready_recovery/recover_orphan_claim` for that exact Issue.
- The accepted pre-kill online backup
  `state.pre-claim-acquired-process-kill-0ukms_yw.db` was mode `0600` and passed
  `integrity_check`.

Two earlier pre-claim attempts encountered the then-active 120-second GitHub Git transport timeout.
The first exposed that a shorter parent handshake timeout could terminate Python while its isolated
Git process group was still running; the exact group was stopped, the parent deadline was raised
above the longest bounded pre-claim Git operation, and a second timeout verified clean teardown.
Neither attempt reached claim or changed Issue `#10`. For the successful kill injection only, the
fixture required GitHub REST `main` and the protected cached mirror ref to equal exact SHA
`b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`; this did not alter normal dispatcher fetch behavior.

### Ordinary recovery

When Git HTTPS recovered, one normal double-opt-in `ssh-run-once` consumed the orphan claim without
manual label repair:

- WorkItem: `wi_56cfb4bd6efc095beabb0852`;
- Turn: `turn_44612d44d99b4ac89947b19afd19655f`, number `1`;
- Codex session: `01a01605-beca-7040-adb8-ca3a6e6bd06c`;
- task branch: `codex/issue-10-56cfb4bd6efc`;
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-10`;
- exact checkpoint: `a17ae709a111cd84d7a08050afa975351190fa73`;
- Draft PR [`#11`](https://github.com/longwdl/codex-dispatcher-fixture/pull/11).

SQLite contained exactly one WorkItem and one finished/completed Turn for the Issue. The PR changed
only `README.md`, one insertion and one deletion, to exact marker `p1-process-kill-v1`; its head and
SQLite publication SHA matched. The fixed Issue comment existed once and the Issue entered
`agent:review`. GitHub Actions run
[`32168464039`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32168464039)
completed successfully for that exact branch/SHA. `main` remained at the base SHA, final preflight
was idle, and an immediate second write-enabled sweep also returned idle.

## SSH CLI START receipt STATUS-only recovery fixture — 2026-08-19

This fixture covers AC-036 with two guarded source-tree stages. The first stage delegates a real
START and discards its response only after strict parsing proves the same WorkItem/Turn identity and
a remote state of `running` or `finished`. The second stage rejects PREPARE, START, and RESUME before
delegation and records the exact recovery operation order.

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#12`](https://github.com/longwdl/codex-dispatcher-fixture/issues/12),
  node ID `I_kwDOT3NfX88AAAABNQlrSw`.
- WorkItem: `wi_594a1305a087ff78a0ab32f8`.
- Turn: `turn_be25135c133b47c6a580ee585d71cd18`, number `1`.
- Task branch: `codex/issue-12-594a1305a087`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-12`.

Immediately after `start-receipt`, the command reported `fault_triggered=true`,
`recovery_required=true`, and `runner_active`. Independent SQLite read-back found exactly one
running WorkItem and one `reconciling` Turn; local `codex_session_id`, `last_published_sha`,
`pr_number`, result status, and output SHA were all unset. The Issue was `agent:running`, and
preflight selected only `reconcile_active_turn` for the exact WorkItem/Turn.

The `start-status-recovery` command then reported `recovery_guarded=true`,
`fault_triggered=false`, `runner_operations=["status","export"]`, and `review`. It reused the same
WorkItem and Turn, bound Codex session `01a0160b-4cb9-7522-9b32-41dff7ab73b6`, and published exact
checkpoint `41e67598b506dcbfeac00e5871a812e6e9874078` to the existing task identity. Draft PR
[`#13`](https://github.com/longwdl/codex-dispatcher-fixture/pull/13) changed only `README.md`, one
insertion and one deletion, to exact marker `p1-start-status-v1`. The fixed Issue comment existed
once, the Issue entered `agent:review`, and GitHub Actions run
[`32169064603`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32169064603)
completed successfully for the same branch/SHA.

Both stage backups were mode `0600` and passed `integrity_check`. Final SQLite contained one
WorkItem and one finished/completed Turn, `main` remained at
`b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`, final preflight was idle, and an immediate normal
write-enabled sweep also returned idle. A brief Issue-list visibility delay after creation was
resolved by an explicit maintainer ready-label transition; no fault write ran until the list read and
preflight both selected the exact Issue. This proves STATUS-only protocol recovery and does not claim
a physical network-link or SSH-daemon interruption.

## SSH CLI exact transport-process interruption fixture — 2026-08-19

This fixture covers AC-021 and AC-056 with a real OpenSSH client-process `SIGKILL`. It did not
change the SSH daemon, firewall, routes, host networking, or any unrelated connection. The guarded
entry received only the exact primary child capability; it had no process-name lookup or bulk-kill
path.

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#14`](https://github.com/longwdl/codex-dispatcher-fixture/issues/14),
  node ID `I_kwDOT3NfX88AAAABNUW04w`.
- WorkItem: `wi_b7edba3be957aa3d4a851c56`.
- Turn: `turn_7e8f8db322764d579c51595e4b2725ab`, number `1`.
- Task branch: `codex/issue-14-b7edba3be957`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-14`.
- Pre-live implementation commit: `3221884`.

Before the live write, all 286 offline tests, `compileall`, and `git diff --check` passed. Read-only
preflight selected only Issue `#14`; SQLite had no binding for it, the derived branch and PR did not
exist, and `main` was `7fe0a9a5d51f4438423744ffb199563a0bcd4d9a`. The independently created
pre-live online backup and both command-created backups were mode `0600` and passed
`integrity_check`.

After the local WorkItem and Turn were durable, a separate hook-free SSH transport issued only
STATUS. Attempt 2 observed the Runner's durable executing signature
`state=unknown,error_code=turn_outcome_unresolved`; the local WorkItem was still running and the
same Turn was still starting. The hook freshly verified immutable argv and exact
`PID=PGID=SID=80465`, then sent `SIGKILL` only to that process group. Independent read-back found the
PID gone, SQLite integrity `ok`, one running WorkItem, and the same Turn in `reconciling` with no
local session, output SHA, publication SHA, or PR. Read-only preflight selected exactly
`reconcile_active_turn`.

The guarded recovery rejected PREPARE, START, and RESUME before delegation and reported exact
`runner_operations=["status","export"]`. It reused the same WorkItem and Turn, bound the existing
Runner session (local and remote SHA-256 fingerprint prefix `0926e3c0ccb6901c`), and published exact
checkpoint `fb2fb166a74984298a56811f3e3e52c4676df82c`. Draft PR
[`#15`](https://github.com/longwdl/codex-dispatcher-fixture/pull/15) changed only the allowed Fixture
path. The one fixed Issue comment existed, the Issue entered `agent:review`, and the one GitHub
Actions run
[`32213983342`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32213983342)
completed successfully at that exact SHA.

Final SQLite contained exactly one WorkItem and one finished/completed Turn; direct Runner STATUS
returned the same Turn, session fingerprint, checkpoint, and completed result. The task branch,
SQLite publication record, PR head, and Actions head all matched. Before human merge, `main` did not
move. Two ordinary write-enabled sweeps and the read-only preflight all returned idle, so no second
Turn, session, branch, PR, comment, or workflow run was created.

### Human merge and completed projection

After explicit review confirmed the README-only diff, exact head SHA, and successful Actions run,
PR `#15` was marked ready and merged without deleting its task branch. GitHub created merge commit
`790c3e0b361f727863e3e6d86ee6e2dce16b4faf`; the PR closed as merged while the task branch remained
at checkpoint `fb2fb166a74984298a56811f3e3e52c4676df82c`.

Read-only preflight then selected `complete_merged_work_item` for the exact Issue, WorkItem, and PR.
One normal double-opt-in sweep first committed the local `completed` tombstone, updated the single
fixed status comment at `2026-08-19T06:32:21Z`, and applied `agent:completed` at
`2026-08-19T06:32:24Z`. The Issue stayed open. SQLite retained the original single finished Turn,
session, PR, and published SHA; no Runner or Publisher work was introduced. A subsequent normal
write-enabled sweep and read-only preflight were idle.

## SSH CLI same-Issue needs-input resume fixture — 2026-08-19

This section records the first end-to-end Issue lifecycle with two Turns in one persistent Codex
session. It proves the GitHub `needs_input → ready` path in addition to the earlier direct Runner
resume fixture. Slack remained disabled, and no merge or completion transition was performed.

### Turn 1: deliberate missing input

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#6`](https://github.com/longwdl/codex-dispatcher-fixture/issues/6).
- WorkItem: `wi_e5c93ce564fa68c4be09cc5c`.
- Task branch identity: `codex/issue-6-e5c93ce564fa`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-6`.
- Codex session: `01a015c4-4053-7812-bed0-d0b39db406be`.

The reviewed Issue intentionally omitted the exact README marker value and required the first Turn
to ask rather than guess. Read-only preflight selected exactly Issue `#6`; the write-enabled sweep
returned `needs_input` for Turn `turn_d550ef3560864379981d58304582f975`. SQLite recorded Turn number
`1`, `result_status=needs_input`, and identical input/output HEAD
`b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`. The WorkItem entered `waiting_input`, and the Issue
entered `agent:needs_input`. Independent read-back found no task ref, checkpoint, or PR; `main` did
not move.

### Maintainer context and Turn 2

The maintainer added one reviewed comment:

```text
/codex-context
Use the exact fixture marker value: p1-needs-input-resume-v1
```

Its immutable comment ID was `IC_kwDOT3NfX88AAAABPcWHXQ`. The Issue was then explicitly returned to
`agent:ready`. During this manual transition, the repository's old setup label
`agent:needs-input` was found to conflict with the canonical runtime label `agent:needs_input`, and
the Issue briefly had both `ready` and `needs_input`. Preflight returned idle and no Runner call was
made. The stale hyphenated repository label was unused and removed, the canonical underscore label
was retained, and the missing canonical `agent:completed` label was created. Only after the Issue
had exactly one state label did preflight select it again.

The second write-enabled sweep returned `review` for Turn
`turn_d3879f60603945f8bb1cd605541a3be4`. SQLite recorded Turn number `2`, included only the reviewed
context comment ID, and preserved the original WorkItem, branch, Runner directory, and session. The
checkpoint was `d25edd9d2b3287a596e773c28210f1d9cefa1f05`; Draft PR
[`#7`](https://github.com/longwdl/codex-dispatcher-fixture/pull/7) was the only PR for the task branch
and changed exactly `README.md` by one insertion and one deletion. The resulting marker was exactly
`p1-needs-input-resume-v1`.

GitHub Actions run [`32162425453`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32162425453)
completed successfully for the exact checkpoint and task branch. Final preflight returned idle;
SQLite contained exactly one WorkItem and two ordered Turns for Issue `#6`; `main` remained at its
original SHA. Both pre-Turn SQLite Online Backup API snapshots were mode `0600` and passed
`integrity_check`.

## SSH CLI recorded-publication recovery fixture — 2026-08-19

This fixture covers AC-047's narrow durability window: the exact checkpoint SHA was committed to
SQLite after a successful task-branch publication, but the Turn had not yet advanced from
`checkpointing` to `published`. The source-tree-only fault hook raises at that exact boundary. This
is deterministic process-level exception injection; it does not claim to be an operating-system
process kill or an SSH disconnect.

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#8`](https://github.com/longwdl/codex-dispatcher-fixture/issues/8).
- WorkItem: `wi_9eb14638cf6691e8b2a783bb`.
- Turn: `turn_54ec2bff87594f53bb668ae8bf950bc1`.
- Codex session: `01a015d2-3a31-7bd1-b847-b892efdbb795`.
- Task branch: `codex/issue-8-9eb14638cf66`.
- Exact checkpoint: `bd7ac54774d9d098c35f9731b18a34d732094736`.

The `publication-recorded` stage returned `process_interrupted` only after
`record_published_sha()` committed. Independent read-back then found the WorkItem still `running`,
the Turn `checkpointing/completed`, the Issue `agent:dispatching`, no Issue comment, and no PR. The
remote task branch and SQLite `last_published_sha` both resolved to the exact checkpoint. Read-only
preflight selected `resume_publication` for the same WorkItem and Turn.

The `recorded-publication-recovery` stage wrapped both the Runner transport and Publisher in
fail-before-delegate guards. Recovery completed successfully, which proves that neither port was
invoked: it used only the durable checkpoint record, then created and bound Draft PR
[`#9`](https://github.com/longwdl/codex-dispatcher-fixture/pull/9), wrote the fixed Issue status
comment, and projected `agent:review`. SQLite ended with exactly one WorkItem and one finished Turn;
the branch, session, output SHA, and PR binding were unchanged. The README marker was exactly
`p1-recorded-publication-v1`, `main` did not move, and GitHub Actions run
[`32163520437`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32163520437)
completed successfully for the exact checkpoint.

Both accepted stages created a mode `0600` SQLite Online Backup API snapshot and passed
`integrity_check`. A brief GitHub Issue-list visibility delay was observed immediately after the
initial label write; no write was attempted until direct read-back and a later preflight agreed on
the exact ready candidate.

## SSH CLI lost-receipt fixture — 2026-08-19

This section records the bounded, three-stage live fault sequence for Publisher, Draft PR, and
Issue-comment receipts. It used the source-tree-only triple-opt-in fault entry and the normal
Dispatcher recovery path. This run itself does not prove recovery from an actual SSH disconnect or
process kill, and it does not exercise Slack; the later Issue `#10` and `#12` sections above record
the separate process and START-receipt boundaries.

### Admission and initial state

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#4`](https://github.com/longwdl/codex-dispatcher-fixture/issues/4).
- The strict task spec allowed only `README.md`, changing the marker to
  `ssh-lost-receipt-phase-d-v1`.
- GitHub Issue node ID: `I_kwDOT3NfX88AAAABNPw7tg`.
- WorkItem: `wi_80df527531e34d4f039aa16f`.
- Task branch: `codex/issue-4-80df527531e3`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-4`.
- Fixture `main` was `b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`; neither the task branch nor
  a matching PR existed.

The first `publisher-receipt` attempt created and verified a private SQLite backup, then stopped at
the trusted-mirror `base_fetch` stage before claim, Runner invocation, or external write. A
restricted read-only retry of the same Git ref succeeded, confirming a transient transport failure
rather than an authentication or repository-state conflict. The exact fault stage was then retried.

### Three discarded receipts

1. `publisher-receipt` started exactly one Codex Turn and discarded the successful new-branch push
   receipt. The command returned `awaiting_publication`; Issue `#4` remained `agent:running`, and
   read-back found the task branch at checkpoint
   `00199ebe3d565048eb6118827aaef9e54ab450cf` with no PR. Preflight then required
   `resume_publication`.
2. `draft-pr-receipt` recovered the same checkpoint, reused the existing remote branch, created
   Draft PR [`#5`](https://github.com/longwdl/codex-dispatcher-fixture/pull/5), and discarded its
   receipt. SQLite had reached `review` with the exact published SHA but had no PR binding; Issue
   `#4` remained `agent:running` and still had no status comment.
3. The following read-only preflight exposed a recovery-order defect: a remote `running` Issue was
   classified as orphaned before its existing terminal WorkItem was considered. No third-stage
   write was attempted while that result was ambiguous. Commit `f42974b` moved the orphan check
   after the persisted binding and terminal-state checks and added a combined Publisher/PR receipt
   regression. All 255 tests passed; live preflight then returned
   `ready_recovery/sync_tracker_state` for the same WorkItem.
4. `issue-comment-receipt` found and bound the existing PR, created the one fixed status comment,
   and discarded that receipt. Preflight again required `sync_tracker_state`. One normal
   double-opt-in `ssh-run-once` returned `state_synchronized`, projected the Issue to
   `agent:review`, and left the subsequent preflight `idle`.

Each accepted stage made an Online Backup API snapshot before entering the sweep. The failed
pre-write Publisher attempt plus the three accepted fault stages left four retained backups; every
file was mode `0600` and passed `PRAGMA integrity_check`. The failed first attempt's backup was
retained rather than silently deleted.

### Independent final read-back

- SQLite passed `integrity_check` and contained exactly one WorkItem and one finished Turn for Issue
  `#4`. WorkItem state was `review`, PR binding was `5`, and both the stored publication SHA and Turn
  output SHA were the exact checkpoint.
- The only Codex session remained `01a015ae-1b9d-7ee3-957a-0a90b21629cd`; no second Turn or session
  was created. Recovery repeated only the exact checkpoint export needed after the ambiguous
  Publisher receipt; it did not PREPARE or START Codex again.
- GitHub contained exactly one open Draft PR for the deterministic branch and exactly one fixed
  Issue status comment. The Issue had `agent:review`, `priority:p1`, and `exec:ssh-cli`.
- The PR changed exactly `README.md`, with one insertion and one deletion; the fixed dispatcher
  marker was present in the only Issue comment.
- The task branch and PR head both resolved to the checkpoint SHA. `main` remained at its original
  SHA; no merge, deployment, release, tag, force-push, or ref deletion occurred.
- GitHub Actions run [`32160041932`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32160041932)
  was `completed/success` for workflow `fixture`, event `pull_request`, run attempt `1`, the exact
  task branch, and the exact checkpoint SHA.

The fine-grained PAT again denied the REST Checks endpoint with HTTP `403`; the permitted Actions
runs endpoint supplied the CI evidence. This run proves the live Publisher lost-receipt read-back
path and the Draft PR and Issue-comment recovery contracts. The narrower post-publication-record,
actual Dispatcher termination, and START-receipt recovery are recorded in the later Fixture
sections above. A physical SSH link/daemon interruption and Slack provider receipt loss remain
separate acceptance work.

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

This proves the completed happy-path is idle on an immediate repeated sweep. It does not by itself
prove the separate crash/lost-receipt recovery paths.

The fine-grained PAT could list Actions runs but could not read check runs through either the
GraphQL `statusCheckRollup` field or the REST Checks endpoint. CI success is therefore evidenced by
the accessible Actions workflow run, not inferred from those denied check APIs. The separate
controlled Publisher/PR/comment lost-receipt evidence is recorded above.

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
