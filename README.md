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
- fixed-lease task-branch publication with exact-SHA read-back recovery;
- fixed OpenSSH framing and an absolute-path `codex-runner-v1` forced-command service;
- persistent Runner workspaces and idempotent first/resume Turn execution;
- outbound-only Slack projection models and offline integration tests.

The Runner path has now been exercised against the private Fixture through the real pinned SSH
transport and Codex CLI 0.147.0 using ChatGPT login. A migrated Issue binding completed PREPARE,
created one persistent session, and resumed that exact session on the same branch and directory;
the read-only task produced no diff or publication checkpoint. The first-phase Runner release is
installed on `s3`, but remains user-owned because the Fixture account has no passwordless sudo.

End-to-end SSH scheduler selection, Publisher push, Slack delivery, automatic draft PR creation,
merge, and production deployment are still absent. Existing Codex Cloud adapter code is retained
only during migration; Cloud writes remain disabled and are not part of the target architecture.

Candidate planning is exposed through a dependency-injected Python entry point and a read-only
GitHub CLI dry-run command. The command performs tracker reads but does not claim issues, mutate
labels, create branches, invoke a Runner, or publish commits.

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
to Codex, accept Slack as input, or interpret unknown external state as success. Any ambiguous
execution or publication state is blocked for human reconciliation.
