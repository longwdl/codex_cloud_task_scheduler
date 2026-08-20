# Owner preparation checklist

This checklist reflects the remote Linux Codex CLI architecture agreed on 2026-08-14. Do not send
credential values in chat, Issue bodies, Slack, repository files, or TOML configuration.

## Already prepared

- Source repository: `longwdl/codex_cloud_task_scheduler`, private, default branch `main`.
- Fixture repository: `longwdl/codex-dispatcher-fixture`, private, intended default branch `main`.
- GitHub maintainer: `longwdl`.
- `gh` is installed and authenticated with access restricted to the two repositories.
- Slack workspace `codex-nt54555` and the initial project channel exist.
- A Codex Cloud Environment ID was supplied earlier, but it is no longer used by the target
  architecture.

## Prepare before the SSH fixture

### 1. Linux Control Host

Prepare a non-production Linux host or VM with:

- 2 vCPU, 4 GiB RAM, and 50 GiB SSD minimum;
- Python 3.12+, Git, OpenSSH client, SQLite support, and systemd;
- outbound access to GitHub and Slack;
- a stable hostname and backups for `/var/lib/codex-dispatcher`;
- no production database, deployment, Kubernetes, cloud, or personal credentials.

Do not install Publisher credentials until its fixed-parameter contract and offline tests pass.

The write-enabled Control Host command now requires a strict `[ssh_runtime]` table. Store only
absolute executable/file/directory paths, fixed Runner host/user/port, timeouts, and the Runner
work-item root there. Do not place token or private-key contents in TOML. Production leaves the
optional `assh_proxy_path`/`assh_home` pair absent; the current Mac Fixture must configure both
together. The command additionally requires the explicit process environment gates
`CODEX_DISPATCHER_ENABLE_SSH_WRITES=1` and a recognized `GH_TOKEN` or `GITHUB_TOKEN`.
The `[tools]` table must pin the Control Host Git, gh, and OpenSSH versions; OpenSSH is read from
`ssh -V` and must match before the sweep can acquire or mutate external work.
Pass the live config by absolute path. Its parent and file may be owned by root or the Dispatcher
user. The SQLite directory and any existing DB/WAL/SHM files must be owned by the Dispatcher user.
All of them must be non-symlink and not group/world writable.
Provide a dedicated `publisher_temporary_root`; it holds only short-lived verified bundle staging
and must be Dispatcher-owned and non-group/world-writable. Do not share it with the Runner or expose
it as an Issue-controlled path.

Before requesting approval for a write-enabled sweep, run the SSH-specific read-only preflight with
the recognized GitHub token already in the environment:

```bash
PYTHONPATH=src python3 -m codex_dispatcher ssh-preflight \
  --config /absolute/path/dispatcher.toml --json
```

This command does not require `CODEX_DISPATCHER_ENABLE_SSH_WRITES`. It checks pinned Control Host
tools, reads GitHub recovery/candidate state, and migrates only a disposable SQLite snapshot. It does
not connect to the Runner or modify the configured database, repository refs, Issues, comments,
labels, or pull requests. Preserve the non-sensitive JSON and verify the repository, Issue number,
status, recovery action, and rejection codes before separately authorizing `ssh-run-once`. The
preflight JSON always says `authorizes_apply=false`; it is a point-in-time snapshot, and the live
sweep revalidates state while holding the Dispatcher lock.

### 2. Dedicated Linux Runner

Prepare a rebuildable Linux host with:

- 4 vCPU, 8 GiB RAM, and 100 GiB SSD minimum;
- Python/build tools needed by the fixture, Git, OpenSSH server, and a pinned Codex CLI;
- outbound access required for Codex authentication and the fixture task;
- no GitHub write credential;
- no Control Host login key, SSH agent, production secret, personal data, or host filesystem mount.

The first fixture intentionally runs without Docker or per-task operating-system restrictions. The
owner accepts loss or corruption of Runner-local task directories and Codex session state. Do not
place a higher-value repository on this Runner until the Docker hardening phase is complete.

Return later, without secrets:

- Runner hostname or address;
- SSH port;
- dedicated Runner username;
- SSH host-key fingerprint;
- installed Git and Codex CLI versions;
- absolute work-item root, normally `/srv/codex-runner/work-items`.

The current `s3` fixture is already reachable as `ecs-user`, has Python 3.12, Git 2.43, Codex CLI
0.147.0, 4 vCPU, 15 GiB RAM, and about 75 GiB free disk. The disk is below the long-term
recommendation but sufficient for the bounded fixture. The implementation fixes these Runner paths:

- protected `/srv/codex-runner/etc/config.json`, based on `config/runner.example.json`;
- protected `/srv/codex-runner/etc/agent-result.schema.json`;
- `/srv/codex-runner/run/active.lock` writable by `ecs-user`;
- `/srv/codex-runner/work-items` for per-Issue repositories and Runner state;
- absolute resolved Git and Codex executable paths;
- Codex Turn timeout; the Control Host SSH operation timeout must be longer than it;
- an SSH `authorized_keys` forced command that invokes only
  `/srv/codex-runner/bin/codex-runner-v1`, with forwarding and
  PTY disabled.

### 3. Codex authentication on Runner

`CODEX_HOME=/srv/codex-runner/app` is already logged in using ChatGPT. It is one shared Runner-level
home, separate from the Runner's `etc`, `run`, and `work-items` directories, not one copy per task.
Keep the directory owned by `ecs-user` with mode `0700`, keep
`auth.json` at `0600`, and initialize or refresh login only in place. Dispatcher and fixture scripts
must never read, print, copy, or log the credential file. Before each live fixture, verify only the
non-secret result of `CODEX_HOME=/srv/codex-runner/app codex login status`. Generated Codex child
commands must not receive GitHub or Control Host credentials.

For the current Mac fixture only, Dispatcher may use the existing SSH identity and the protected
`/opt/homebrew/bin/assh` helper with the fixed proxy shape `assh connect --port=%p %h`. A dedicated
Runner private key is deferred for this environment. Configure the owned `/Users/wdl` home
explicitly so `assh` can find `~/.ssh/assh.yml`; do not inherit the rest of the local environment.
Production defaults to direct SSH with no ProxyCommand and must reassess key separation before
deployment.

### 4. GitHub execution label

The new backend label is:

```text
exec:ssh-cli
```

Keep `exec:cloud` only for historical evidence. Adding or changing labels is a GitHub remote-state
operation and should occur when the new Tracker contract is ready for a live fixture.

The complete canonical agent-state label set uses underscores exactly as the runtime enum does:

```text
agent:ready
agent:dispatching
agent:running
agent:review
agent:needs_input
agent:blocked
agent:paused
agent:completed
agent:discard
```

Do not create or use `agent:needs-input`. A human transition back to ready must remove the current
agent-state label and leave exactly one `agent:ready`; the adapter rejects or ignores ambiguous
multi-state Issues rather than choosing one.

### 5. Publisher authentication

Preferred long-term option: a GitHub App installed only on explicitly selected repositories.

- Dispatcher operations: Metadata read, Contents read, Issues write, Pull requests write.
- Publisher operation: temporary Contents write token.
- No Administration, Secrets, Environments, Deployments, Actions write, or bypass permission.
- The App must not merge, force-push, delete refs, write tags, or bypass default-branch protection.

For the private fixture, the previously accepted absence of a personal-account ruleset remains a
known residual risk. This does not permit exposing the write token to Codex; Publisher parameter
restrictions remain mandatory.

### 5.1 Controlled lost-receipt fixture

The source-tree-only fault entry is never a systemd command and must not be installed on a production
Control Host. It rejects any configuration other than the single private
`longwdl/codex-dispatcher-fixture` repository with base `main`, README-only policy, maintainer
`longwdl`, and required check `fixture`. It also requires an existing protected database, an exact
read-only preflight stage, `--apply`, the normal SSH write opt-in, and a third opt-in whose value is
the complete Fixture repository name. Every accepted invocation first creates and verifies a mode
`0600` SQLite online backup in the configured state directory.

Use one new reviewed Fixture Issue and execute the stages only in this order:

```bash
CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 \
CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS=longwdl/codex-dispatcher-fixture \
PYTHONPATH=src python3 -m codex_dispatcher.fixture_fault_cli \
  --config /absolute/path/dispatcher.toml --issue ISSUE_NUMBER \
  --fault publisher-receipt --apply --json

CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 \
CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS=longwdl/codex-dispatcher-fixture \
PYTHONPATH=src python3 -m codex_dispatcher.fixture_fault_cli \
  --config /absolute/path/dispatcher.toml --issue ISSUE_NUMBER \
  --fault draft-pr-receipt --apply --json

CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 \
CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS=longwdl/codex-dispatcher-fixture \
PYTHONPATH=src python3 -m codex_dispatcher.fixture_fault_cli \
  --config /absolute/path/dispatcher.toml --issue ISSUE_NUMBER \
  --fault issue-comment-receipt --apply --json
```

Each command must report `fault_triggered=true` and `recovery_required=true`. Run `ssh-preflight`
between stages and require, respectively, `resume_publication`, then `sync_tracker_state`, then
`sync_tracker_state`. After the third injected receipt loss, use the normal `ssh-run-once` exactly
once to repair the existing comment/PR binding and terminal label. Stop immediately on any different
status; do not skip a stage, substitute another repository, delete a branch, or edit SQLite.

### 5.2 Recorded-publication recovery fixture

Use a separate new reviewed Fixture Issue. The first stage stops immediately after the exact
published SHA is durably recorded and before the Turn is terminalized. The second stage installs
fail-before-delegate Runner and Publisher guards, so a successful recovery proves neither was
called:

```bash
CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 \
CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS=longwdl/codex-dispatcher-fixture \
PYTHONPATH=src python3 -m codex_dispatcher.fixture_fault_cli \
  --config /absolute/path/dispatcher.toml --issue ISSUE_NUMBER \
  --fault publication-recorded --apply --json

CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 \
CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS=longwdl/codex-dispatcher-fixture \
PYTHONPATH=src python3 -m codex_dispatcher.fixture_fault_cli \
  --config /absolute/path/dispatcher.toml --issue ISSUE_NUMBER \
  --fault recorded-publication-recovery --apply --json
```

The first command must report `process_interrupted`; preflight must then select
`resume_publication` for the exact WorkItem and Turn. The second command must report
`recovery_guarded=true`, `fault_triggered=false`, and `review`. Confirm exactly one Turn, session,
task branch, Draft PR, and status comment; confirm the task branch and SQLite publication record use
the same SHA and that the default branch did not move. This fixture does not substitute for the
separate operating-system process-kill or SSH-disconnect tests.

### 5.3 Exact Dispatcher process-kill fixture

Use a separate new ready Fixture Issue. The protected mirror's fixed base ref must already equal the
current GitHub `main` SHA. The parent starts one exact child argv in a new session, waits up to 180
seconds for a private-pipe handshake emitted only after the successful Issue claim, and sends
`SIGKILL` only to that still-running child:

```bash
CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 \
CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS=longwdl/codex-dispatcher-fixture \
PYTHONPATH=src python3 -m codex_dispatcher.fixture_process_cli \
  --config /absolute/path/dispatcher.toml --issue ISSUE_NUMBER \
  --apply --json
```

Require `termination_signal=SIGKILL`, `child_exit_code=-9`,
`local_work_item_persisted=false`, `runner_reached=false`, and
`recovery_action=recover_orphan_claim`. Independently require SQLite integrity, no WorkItem or Turn
for the Issue, and `agent:dispatching`. Then run the ordinary `ssh-run-once` path; it must recover the
same Issue into exactly one WorkItem/Turn/session/branch/PR and leave an immediate repeated sweep
idle. Do not manually reset the label or use the cached-base Fixture source for normal recovery.

### 5.4 START receipt and STATUS-only recovery fixture

Use another new ready Fixture Issue and run exactly these two guarded stages:

```bash
CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 \
CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS=longwdl/codex-dispatcher-fixture \
PYTHONPATH=src python3 -m codex_dispatcher.fixture_fault_cli \
  --config /absolute/path/dispatcher.toml --issue ISSUE_NUMBER \
  --fault start-receipt --apply --json

CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 \
CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS=longwdl/codex-dispatcher-fixture \
PYTHONPATH=src python3 -m codex_dispatcher.fixture_fault_cli \
  --config /absolute/path/dispatcher.toml --issue ISSUE_NUMBER \
  --fault start-status-recovery --apply --json
```

After the first stage require `runner_active`, one `reconciling` Turn, a running WorkItem with no
local session binding/checkpoint/PR, and preflight `reconcile_active_turn`. The second command must
report `recovery_guarded=true`, `fault_triggered=false`, exact
`runner_operations=["status","export"]`, and `review`. PREPARE, START, and RESUME are rejected before
delegation during this recovery. Confirm the same WorkItem and Turn, one bound session and Draft PR,
exact checkpoint equality, successful required checks, mode-`0600` backups, unchanged `main`, and an
idle repeated sweep.

### 5.5 Exact SSH transport process-kill fixture

Use one new ready Fixture Issue whose derived branch and PR do not exist. Run the normal read-only
preflight first and require that exact Issue as the only candidate. Do not manually select or kill a
PID; the guarded hook owns only the SSH client process it just spawned:

```bash
CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 \
CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS=longwdl/codex-dispatcher-fixture \
PYTHONPATH=src python3 -m codex_dispatcher.fixture_fault_cli \
  --config /absolute/path/dispatcher.toml --issue ISSUE_NUMBER \
  --fault ssh-transport-process-kill --apply --json
```

The command may interrupt only after the exact WorkItem and starting Turn are durable locally and a
second hook-free SSH connection proves the Runner has a durable executing or finished record.
Require `fault_triggered=true`, `termination_signal=SIGKILL`,
`local_work_item_persisted=true`, `status=runner_active`, and `turn_state=reconciling`. Require the
reported PID, process-group ID, and session ID to be identical, with no rejection reason. Current
Runner executing proof is `status_proof_state=unknown` plus
`status_proof_error_code=turn_outcome_unresolved`; a finished proof is also acceptable. Any missing,
conflicting, or ambiguous proof must leave the SSH process untouched and fail closed.

Independently confirm the exact PID is gone, SQLite retains one running WorkItem and the same
`reconciling` Turn without local session/checkpoint/PR, and preflight selects
`reconcile_active_turn`. Recover only with the `start-status-recovery` command from section 5.4. It
must report exact `runner_operations=["status","export"]`; PREPARE, START, RESUME, and Prompt replay
are forbidden. Finally require one finished Turn, one session, one task branch, one Draft PR, one
fixed status comment, successful checks at the exact SHA, unchanged `main`, and at least two normal
write-enabled sweeps returning idle. Do not change sshd, firewall, routing, or another connection.

### 5.6 Human merge and completion projection

The dispatcher never merges. The completion reconciler treats the already-merged PR as the human
authorization boundary; it does not separately query Actions checks. In the private
personal-repository fixture, where a ruleset was explicitly deferred, the maintainer must therefore
verify checks before merging.

After a maintainer reviews the diff, confirms required checks, marks the Draft PR ready, and
explicitly merges it, first run `ssh-preflight`. It must report `complete_merged_work_item` for the
exact Issue, WorkItem, and PR number. Then run one ordinary double-opt-in `ssh-run-once` and require:

- PR repository/base/head branch and `headRefOid` exactly match the persisted binding and
  `last_published_sha`;
- SQLite reaches the irreversible `completed` state before any Issue write;
- the one fixed Issue comment reports `agent:completed` and retains any durable Slack permalink;
- only after the comment succeeds does the Issue label become `agent:completed`;
- no Runner, Publisher, new Turn, new PR, branch deletion, Issue close, deployment, or release occurs;
- a repeated sweep is idle, and setting the same Issue back to ready is rejected.

Any closed-but-unmerged PR, head mismatch, cross-repository PR, or premature completed/ready label is
`blocked`. A lost completion comment or label receipt may retry only the same Issue projection.

### 5.7 Controlled completion receipt-loss fixture

Use the same reviewed Fixture Issue only after its one bound Draft PR has passed the exact-SHA
required check and the maintainer has explicitly marked it ready and merged it. The dispatcher must
not perform the merge. With the current Slack-enabled Fixture configuration, keep both normal write
gates enabled and supply the bot token only through `SLACK_BOT_TOKEN`:

```bash
CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 \
CODEX_DISPATCHER_ENABLE_SLACK_WRITES=1 \
CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS=longwdl/codex-dispatcher-fixture \
PYTHONPATH=src python3 -m codex_dispatcher.fixture_fault_cli \
  --config /absolute/path/dispatcher.toml --issue ISSUE_NUMBER \
  --fault completion-comment-receipt --apply --json

CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 \
CODEX_DISPATCHER_ENABLE_SLACK_WRITES=1 \
CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS=longwdl/codex-dispatcher-fixture \
PYTHONPATH=src python3 -m codex_dispatcher.fixture_fault_cli \
  --config /absolute/path/dispatcher.toml --issue ISSUE_NUMBER \
  --fault completion-label-receipt --apply --json
```

Before the first command, read-only preflight must select `complete_merged_work_item` for the exact
Issue, WorkItem, branch, PR, and persisted head SHA. The first command must commit the irreversible
local `completed` tombstone, write the exact fixed `agent:completed` comment, discard only that
successful response, and leave the remote label at `agent:review`. Preflight must then select
`sync_tracker_state`. The second command must idempotently update the same comment, apply and read
back the exact `agent:completed` label, and discard only that verified response. It must not invoke
Source, Runner, Git Publisher, Slack Publisher, create another Turn/PR, or replay a Prompt.

Each invocation must create a verified mode-`0600` SQLite online backup and report the exact
completion guard flags. After the label response is discarded, both read-only preflight and a normal
write-enabled sweep must already be idle because the remote completed state was read back before the
fault. Independently verify one WorkItem, finished Turn, session, branch, merged PR, fixed comment,
completed label, successful exact-SHA Actions run, and unchanged Runner record; then repeat the
ordinary sweep. Stop on any identity or state mismatch.

Fixture Issue `#18` completed this sequence on 2026-08-20 after an explicitly authorized operator
merge of PR `#19` at exact head `7a90afd4dbbec378dbfb7cad45bfa3c72452e8a2`. The comment stage
left the Issue in review after the local tombstone and successful fixed-comment write; preflight
selected `sync_tracker_state`. The label stage read back `agent:completed` before discarding its
response. The comment timestamp preceded the completed-label event, Runner STATUS retained the
same finished Turn/session, and final preflight plus two ordinary sweeps were idle.

### 6. Slack outbound app

The existing official Codex Slack binding is not the Dispatcher integration. A custom outbound-only
Slack app will eventually need:

- the bot-only [`chat:write`](https://docs.slack.dev/reference/methods/chat.postMessage/) scope and
  membership in the selected private project channel;
- a bot token stored only on the Control Host;
- no Events API subscription, Socket Mode, slash commands, interactions, message-history input, or
  task-control capability.

Slack documents [`chat.getPermalink`](https://docs.slack.dev/reference/methods/chat.getPermalink/)
as requiring no additional scope. On 2026-08-20, a controlled fixture against Workspace
`T0BQ60N9WH4` and private channel `C0BR2D0MS8Y` proved that retrying one stable delivery key returned
the original message receipt; the maintainer independently confirmed that only one message was
visible. Do not add conversation-history or search scopes to compensate for an ambiguous write
response.

The runtime reads the bot token only from `SLACK_BOT_TOKEN`. Enabling configured Slack output also
requires `CODEX_DISPATCHER_ENABLE_SLACK_WRITES=1` and the exact configuration assertion
`idempotency_contract = "client_msg_id-live-fixture-verified-v1"`. That assertion records completed
fixture evidence; it is not permission to skip the fixture. The publisher uses direct TLS to
`slack.com`, does not inherit proxy variables or follow redirects, and never automatically retries a
request with an ambiguous write outcome.

The fixture-only CLI is not a routine health check. It requires `--apply`, a canonical UUIDv4, the
exact Workspace/channel IDs, and the ephemeral environment gate
`CODEX_DISPATCHER_ENABLE_SLACK_FIXTURE_WRITES=1`. Its successful API receipt still requires a human
single-message confirmation and does not itself edit runtime configuration.

A normal end-to-end run on 2026-08-20 then used the proven contract for Fixture Issue `#16`. It
created exactly one Slack root and one result reply, persisted both receipts, projected the root
permalink to the one fixed Issue comment, created one Draft PR, and passed the exact-SHA `fixture`
workflow. A read-only preflight and an immediate repeated write-enabled sweep were both idle.

### 6.1 Integrated Slack receipt-loss fixture

The provider-level exact-retry proof above is separate from Dispatcher recovery. To exercise the
real outbox without adding Slack read scopes, use one new reviewed Fixture Issue whose bounded task
will complete in one Turn. In addition to the normal fault gates, both stages require the configured
Slack write gate and a bot token in `SLACK_BOT_TOKEN`:

```bash
CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 \
CODEX_DISPATCHER_ENABLE_SLACK_WRITES=1 \
CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS=longwdl/codex-dispatcher-fixture \
PYTHONPATH=src python3 -m codex_dispatcher.fixture_fault_cli \
  --config /absolute/path/dispatcher.toml --issue ISSUE_NUMBER \
  --fault slack-root-receipt --apply --json

CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 \
CODEX_DISPATCHER_ENABLE_SLACK_WRITES=1 \
CODEX_DISPATCHER_ENABLE_FIXTURE_FAULTS=longwdl/codex-dispatcher-fixture \
PYTHONPATH=src python3 -m codex_dispatcher.fixture_fault_cli \
  --config /absolute/path/dispatcher.toml --issue ISSUE_NUMBER \
  --fault slack-terminal-receipt --apply --json
```

The first command may discard a receipt only after the exact root message and permalink have been
returned. It must leave one unstarted `READY` WorkItem, no Turn/session, and one `PREPARED` root
outbox record. Read-only preflight must then report `start_claimed_turn`. The second command first
recovers that same root, then may discard only the successful result-reply receipt after the same
WorkItem has one finished Turn, published SHA, and bound Draft PR. It must leave the Issue remotely
dispatching, the WorkItem locally in review, and only the result outbox record prepared. Read-only
preflight must then report `sync_tracker_state`.

Run the ordinary `ssh-run-once` once to recover the exact result delivery and final Issue label.
Compare the discarded and durable root/result timestamps and permalinks, then independently verify
one WorkItem, Turn, session, branch, Draft PR, Actions run, Slack root, and Slack reply. A repeated
preflight and sweep must be idle. Stop on any other state; do not add history/search scopes, replay
the Prompt, create a second Issue, or edit SQLite.

Fixture Issue `#18` completed this sequence on 2026-08-20. The root and result recovery calls each
returned the exact timestamp/permalink that had been discarded, SQLite finished with two delivered
records, and one WorkItem/Turn/session/branch/Draft PR/Actions run remained. Independent Runner
STATUS was `finished`; Fixture `main` was unchanged and the repeated preflight/sweep were idle.

The GitHub Issue will store a direct Slack thread link. Human task input remains in GitHub only.

## Keep out of scope

- Codex Cloud execution;
- Slack-to-Codex input;
- automatic merge, release, or deployment;
- production repositories or credentials during the unrestricted Runner phase;
- multiple active Turns or multiple Dispatcher instances;
- Docker until the SSH Runner fixture and offline contracts are proven.
