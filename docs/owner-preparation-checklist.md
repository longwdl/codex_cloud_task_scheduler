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

`CODEX_HOME=/srv/codex-runner` is already logged in using ChatGPT. It is one shared Runner-level
home, not one copy per task. Keep the directory owned by `ecs-user` with mode `0700`, keep
`auth.json` at `0600`, and initialize or refresh login only in place. Dispatcher and fixture scripts
must never read, print, copy, or log the credential file. Before each live fixture, verify only the
non-secret result of `CODEX_HOME=/srv/codex-runner codex login status`. Generated Codex child
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

### 5.5 Human merge and completion projection

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

### 6. Slack outbound app

The existing official Codex Slack binding is not the Dispatcher integration. A custom outbound-only
Slack app will eventually need:

- the bot-only [`chat:write`](https://docs.slack.dev/reference/methods/chat.postMessage/) scope and
  membership in the selected private project channel;
- a bot token stored only on the Control Host;
- no Events API subscription, Socket Mode, slash commands, interactions, message-history input, or
  task-control capability.

Slack documents [`chat.getPermalink`](https://docs.slack.dev/reference/methods/chat.getPermalink/)
as requiring no additional scope. The real publisher must remain disabled until a live fixture
proves that retrying its stable delivery key returns the original message receipt. Do not add
conversation-history or search scopes to compensate for an ambiguous write response.

The GitHub Issue will store a direct Slack thread link. Human task input remains in GitHub only.

## Keep out of scope

- Codex Cloud execution;
- Slack-to-Codex input;
- automatic merge, release, or deployment;
- production repositories or credentials during the unrestricted Runner phase;
- multiple active Turns or multiple Dispatcher instances;
- Docker until the SSH Runner fixture and offline contracts are proven.
