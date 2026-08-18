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
- protected trusted-mirror refresh of one fixed GitHub base ref without persisted remotes;
- fixed-lease task-branch publication with exact-SHA read-back recovery;
- fixed OpenSSH framing and an absolute-path `codex-runner-v1` forced-command service;
- persistent Runner workspaces and idempotent first/resume Turn execution;
- a recovery-first, process-locked Control Host sweep with stable Issue/comment snapshots and
  idempotent Publisher checkpoint completion;
- strict same-repository Draft PR lookup/creation, branch read-back, SQLite binding, and ordered
  Issue status projection with lost-receipt recovery;
- a read-only `ssh-preflight` that verifies pinned Control Host tools, plans recovery from a
  migrated disposable SQLite snapshot, and selects only `exec:ssh-cli` Issues through GitHub reads;
- a double-opt-in `ssh-run-once` entry point that assembles only fixed GitHub, mirror, and SSH ports;
- a hashed Slack outbox, unique root/thread binding, redacted terminal reports, and offline
  lost-receipt recovery behind an idempotent outbound publisher port.

The Runner path has now been exercised against the private Fixture through the real pinned SSH
transport and Codex CLI 0.147.0 using ChatGPT login. A migrated Issue binding completed PREPARE,
created one persistent session, and resumed that exact session on the same branch and directory;
the read-only task produced no diff or publication checkpoint. The first-phase Runner release is
installed on `s3`, but remains user-owned because the Fixture account has no passwordless sudo.

The live dependency assembly now includes exact-SHA task-branch publication, unique Draft PR
reconciliation, and ordered Issue comment/label delivery, but the write-enabled entry point has not
yet been exercised against the Fixture. The trusted-mirror fetch, Publisher push, and GitHub writes
are therefore still offline-tested only. The Slack coordination core is wired only through injected
ports; a real Slack HTTP publisher remains disabled until its provider-side deduplication behavior is
proven in a live fixture. Merge and production deployment remain absent. Existing Codex Cloud
adapter code is retained only during migration; Cloud writes remain disabled and are not part of the
target architecture.

Current SSH candidate and recovery planning is exposed through `ssh-preflight`. It checks Git, gh,
and OpenSSH versions, reads GitHub, and migrates only a temporary copy of SQLite. It does not alter
the configured database, claim Issues, mutate labels, fetch or push Git, invoke a Runner, or create a
pull request. Its result is a point-in-time snapshot and never authorizes a write; `ssh-run-once`
revalidates state while holding the Dispatcher lock. The older `run-once --dry-run` remains
Cloud-labelled migration code.

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
explicit recognized GitHub token. Do not run it merely to validate configuration.

A build backend and wheel packaging are intentionally deferred until that tooling choice is
approved; they are not needed for the offline core.

## Documentation

- [Implementation, deployment, and acceptance plan](docs/implementation-deployment-test-plan.md)
- [Owner preparation checklist](docs/owner-preparation-checklist.md)
- [Development notes](docs/development.md)
- [Architecture baseline](docs/architecture.md)
- [Live fixture test evidence](docs/live-test-evidence.md)

## Security model

The dispatcher processes untrusted issue text, Runner output, Git bundles, and agent-generated code.
It must never execute issue-provided commands on its control host, expose GitHub write credentials
to Codex, accept Slack as input, or interpret unknown external state as success. Ambiguous execution
or publication remains active for bounded status/read-back reconciliation and is never treated as
success by inference.
