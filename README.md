# Codex SSH CLI Task Scheduler

A small, fail-closed control-plane service that turns explicitly approved GitHub issues into
persistent Codex CLI sessions on a dedicated Linux runner and delivers checkpoint commits as draft
pull requests.

The project is intentionally not a task board, deployment system, or long-running web service.
GitHub is the only human-input and project view. SQLite stores recoverable work-item and turn state.
Slack is an outbound-only execution view. A single Linux dispatcher performs deterministic
reconciliation through restricted SSH.

## Current status

The repository is migrating from an abandoned Codex Cloud design to a remote Linux Codex CLI
executor. The environment-independent implementation now includes:

- strict configuration parsing;
- the legacy run state machine and additive SQLite persistence;
- hardened local Git mirror and task-worktree preparation;
- issue task-spec parsing and immutable prompt snapshots;
- redaction and safe subprocess execution;
- stable WorkItem/Turn identity and persistence;
- strict Runner and Publisher request contracts;
- Codex JSONL session binding and resume planning;
- deterministic task-directory and branch identity;
- bounded source/result Git bundle transfer and quarantine verification;
- protected trusted-mirror refresh of one fixed GitHub base ref without persisted remotes, with one
  bounded read-only fetch retry inside the original timeout budget;
- fixed-lease task-branch publication with exact-SHA read-back recovery;
- fixed OpenSSH framing and an absolute-path `codex-runner-v1` forced-command service;
- persistent Runner workspaces and idempotent first/resume Turn execution;
- a recovery-first, process-locked Control Host sweep with stable Issue/comment snapshots and
  idempotent Publisher checkpoint completion;
- strict same-repository Draft PR lookup/creation, branch read-back, SQLite binding, and ordered
  Issue status projection with lost-receipt recovery;
- exact merged-PR/head reconciliation that durably closes the WorkItem before projecting
  `agent:completed`, without merging or closing the Issue itself;
- a read-only `ssh-preflight` that verifies pinned Control Host tools, plans recovery from a
  migrated disposable SQLite snapshot, and selects only `exec:ssh-cli` Issues through GitHub reads;
- a double-opt-in `ssh-run-once` entry point that assembles only fixed GitHub, mirror, and SSH ports;
- separate triple-opt-in, hard-coded Fixture entries that can discard one successful Runner,
  Publisher, Draft PR, or Issue-comment receipt, or kill one exact post-claim child process,
  without changing the normal runtime path;
- a hashed Slack outbox, unique root/thread binding, redacted terminal reports, and offline
  lost-receipt recovery behind an idempotent outbound publisher port;
- a standard-library Slack Web API publisher pinned to `chat.postMessage` and
  `chat.getPermalink`, with deterministic `client_msg_id`, bounded responses, no redirects,
  output escaping, and separate runtime/token opt-ins.
- a fixed, argument-free Linux Control Host entrypoint plus a hardened systemd oneshot/timer and
  root-only environment-file template; live installation and activation remain operator actions.
- a credential-free, network-isolated daily systemd job that atomically publishes an
  integrity-checked SQLite Online Backup without automatic deletion.

The Runner path has now been exercised against the private Fixture through the real pinned SSH
transport and Codex CLI 0.147.0 using ChatGPT login. A migrated Issue binding completed PREPARE,
created one persistent session, and resumed that exact session on the same branch and directory;
the read-only task produced no diff or publication checkpoint. The first-phase Runner release is
installed on `s3`, but remains user-owned because the Fixture account has no passwordless sudo. A
separate production deployment contract now defines a dedicated locked protocol account,
root-owned immutable inputs, an external root-owned authorized-key file, and an sshd-enforced fixed
command; it is not installed on `s3` yet and is not a substitute for the later container boundary.

The write-enabled dependency assembly has now completed one bounded happy-path run against Fixture
Issue `#2`: it claimed one SSH-labelled Issue, created one persistent Codex session, published the
exact checkpoint SHA to the deterministic task branch, opened one Draft PR, projected the Issue to
`agent:review`, and passed the Fixture GitHub Actions workflow. Read-back confirmed that `main` did
not move. A second write-enabled sweep returned `idle`; SQLite, Issue, PR, refs, and the single CI run
remained unchanged, so it did not create another Turn, session, push, comment, PR, or workflow run.
A second Fixture Issue has now completed controlled live Publisher, Draft PR, and Issue-comment
receipt loss. Recovery retained one WorkItem, session, Turn, branch, Draft PR, and status comment,
left `main` unchanged, and passed the exact-SHA Fixture workflow. That run exposed and fixed a
recovery-order defect for a terminal local WorkItem whose Issue was still `agent:running`. The real
Slack HTTP publisher is implemented and wired behind strict optional configuration. A controlled
live fixture against the private project channel proved that an exact `client_msg_id` retry returned
the original receipt and left one visible message; normal runtime still requires the separate
configuration assertion, token, and write opt-in. A subsequent normal end-to-end Fixture produced
one WorkItem/Turn/session, one Slack root/result thread, one Draft PR, and one successful Actions run;
an immediate repeated sweep was idle. Merge and production deployment remain absent.
Existing Codex Cloud adapter code is retained only during migration; Cloud writes remain disabled
and are not part of the target architecture.

A separate two-Turn Fixture has also proven the reviewed `needs_input → /codex-context → ready`
lifecycle through the real GitHub adapter and SSH Runner. The follow-up reused the original WorkItem,
branch, Runner directory, and Codex session, created only one Draft PR, and passed Fixture CI. This
run also corrected the Fixture's legacy hyphenated needs-input label; canonical state labels use
`agent:needs_input`.

AC-047's recorded-publication recovery has also been exercised against Fixture Issue `#8`. A
fixture-only hook stopped after the exact published SHA was committed but before Turn
terminalization; guarded recovery then completed without calling the Runner or Publisher again,
created one Draft PR, and passed Fixture CI. That deterministic exception injection alone did not
cover an operating-system process kill or a real SSH disconnect; those boundaries were exercised
separately below.

Fixture Issue `#10` subsequently covered the operating-system process boundary. A parent accepted
an identity-bound private-pipe handshake only after the Issue claim, sent `SIGKILL` to that exact
child, and proved that no WorkItem, Turn, or Runner call existed. Read-only preflight selected
`recover_orphan_claim`; after Git transport recovered, the ordinary dispatcher path created one
WorkItem, Turn, session, task branch, and Draft PR `#11`, passed Fixture CI, and returned idle on an
immediate repeated sweep.

Fixture Issue `#12` covered the ambiguous START receipt. The first guarded stage discarded only a
valid identity-matching START reply and left one `reconciling` Turn with no local session binding.
The second stage rejected PREPARE/START/RESUME before delegation and completed through exactly
`STATUS` then `EXPORT`, reusing the same WorkItem and Turn. Draft PR `#13` passed Fixture CI and a
repeated sweep returned idle. This proves the protocol recovery path, not a physical network-cable
or SSH-daemon failure.

Fixture Issue `#14` then exercised a real OpenSSH client-process interruption. The guarded hook
waited until the exact local WorkItem and Turn were durable, used a separate hook-free SSH
connection to prove the Runner's durable executing record, freshly revalidated the primary
process's immutable argv and PID/PGID/SID identity, and sent `SIGKILL` only to that process group.
The same Turn remained `reconciling`; guarded recovery used only `STATUS` then `EXPORT`, reusing the
same WorkItem, Turn, branch, Runner directory, and Codex session. Draft PR `#15` and its single
Fixture Actions run passed at the exact published SHA; before human merge, `main` did not move and
two ordinary write-enabled sweeps returned idle. After explicit review and merge, the normal
completion path advanced `main` to merge commit `790c3e0b361f727863e3e6d86ee6e2dce16b4faf`, committed
the local tombstone, updated the fixed comment, and only then applied `agent:completed`; the Issue
stayed open, the task branch remained, and a repeated sweep was idle. No SSH daemon, firewall,
route, or unrelated connection was modified.

Merged-PR completion is implemented and live-verified: after the maintainer reviewed, marked ready,
and merged Fixture PR `#13`, the dispatcher required the bound PR at the exact persisted head SHA,
wrote the irreversible local `completed` tombstone first, then idempotently updated the fixed Issue
comment and finally `agent:completed`. It did not close the Issue, delete the task branch, or perform
the merge itself; an immediate repeated sweep returned idle.

After explicit operator authorization and exact-head merge of Fixture PR `#19`, Issue `#18` also
completed both GitHub projection receipt-loss windows. The first fault persisted the local completed
tombstone before discarding the successful fixed-comment response; the second retried that comment,
read back `agent:completed`, and then discarded the label response. Final preflight and two ordinary
sweeps were idle, with the original WorkItem, Turn, session, branch, and PR preserved.

Current SSH candidate and recovery planning is exposed through `ssh-preflight`. It checks Git, gh,
and OpenSSH versions, reads GitHub, and migrates only a temporary copy of SQLite. It does not alter
the configured database, claim Issues, mutate labels, fetch or push Git, invoke a Runner, or create a
pull request. Its result is a point-in-time snapshot and never authorizes a write; `ssh-run-once`
revalidates state while holding the Dispatcher lock. The older `run-once --dry-run` remains
Cloud-labelled migration code.

The fault entry is not a production command and is not exposed through the normal CLI. It accepts
only `longwdl/codex-dispatcher-fixture`, its exact README-only repository contract, and one exact
Issue/stage selected by read-only preflight. It requires a third repository-name environment opt-in
and creates a private SQLite online backup before entering the normal process-locked sweep. Its SSH
transport fault receives only the exact spawned client capability and cannot kill by name; it must
prove durable remote acceptance through a second read-only STATUS connection before an exact
process-group termination is authorized.

## Requirements

- Python 3.12 or newer
- Git

No runtime third-party Python dependency is currently required.

## Verify

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src tests
```

Run the source-tree CLI without installing a package:

```bash
PYTHONPATH=src python3 -m codex_dispatcher --help
PYTHONPATH=src python3 -m codex_dispatcher doctor \
  --config config/dispatcher.example.toml --contract --json
PYTHONPATH=src python3 -m codex_dispatcher run-once \
  --dry-run --config config/dispatcher.example.toml --json
```

With a recognized GitHub token already present in the process environment, inspect one protected
live SSH configuration without enabling writes:

```bash
PYTHONPATH=src python3 -m codex_dispatcher ssh-preflight \
  --config /absolute/path/dispatcher.toml --json
```

The write-enabled SSH command is intentionally not part of routine offline verification. It requires
both `--apply` and the exact environment opt-in `CODEX_DISPATCHER_ENABLE_SSH_WRITES=1`, plus an
explicit recognized GitHub token. If `[slack_runtime]` is configured, it additionally requires
`CODEX_DISPATCHER_ENABLE_SLACK_WRITES=1` and an `xoxb-` token in `SLACK_BOT_TOKEN`. Do not configure
the Slack idempotency proof value before the controlled live fixture succeeds, and do not run the
command merely to validate configuration. The fixture-only `slack-idempotency-fixture` entry point
requires `--apply`, a canonical UUIDv4, the exact Workspace/channel IDs, and the separate ephemeral
`CODEX_DISPATCHER_ENABLE_SLACK_FIXTURE_WRITES=1` gate; it must not be used as a routine health check.

A build backend and wheel packaging are intentionally deferred until that tooling choice is
approved; they are not needed for the offline core.

## Documentation

- [Implementation, deployment, and acceptance plan](docs/implementation-deployment-test-plan.md)
- [Owner preparation checklist](docs/owner-preparation-checklist.md)
- [Development notes](docs/development.md)
- [Architecture baseline](docs/architecture.md)
- [Live fixture test evidence](docs/live-test-evidence.md)
- [Linux Control Host system service](deploy/systemd/README.md)
- [Rootless Fixture Control Host user service](deploy/systemd-user/README.md)
- [Linux Runner production ownership and SSH boundary](deploy/runner/README.md)

## Security model

The dispatcher processes untrusted issue text, Runner output, Git bundles, and agent-generated code.
It must never execute issue-provided commands on its control host, expose GitHub write credentials
to Codex, accept Slack as input, or interpret unknown external state as success. Ambiguous execution
or publication remains active for bounded status/read-back reconciliation and is never treated as
success by inference.
