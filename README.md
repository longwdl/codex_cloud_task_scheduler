# Codex Cloud Task Scheduler

A small, fail-closed control-plane service that turns explicitly approved GitHub issues into
Codex Cloud tasks, reconciles their state, and eventually delivers changes as draft pull requests.

The project is intentionally not a task board, deployment system, or long-running web service.
GitHub stores human task and code facts; SQLite stores recoverable run state; a periodic oneshot
dispatcher performs deterministic reconciliation.

## Current status

The repository is in its first implementation phase. The environment-independent core is being
built first:

- strict configuration parsing;
- run state machine and SQLite persistence;
- issue task-spec parsing and immutable prompt snapshots;
- redaction and safe subprocess execution;
- tracker/executor ports, offline fakes, and read-only candidate planning;
- offline unit and integration tests.

There is no enabled GitHub write sweep, Codex Cloud submission, systemd deployment, automatic PR
creation, merge, or deployment in this revision. GitHub claim/state/comment primitives exist for
controlled contract tests but are not exposed by the CLI.

Candidate planning is exposed through a dependency-injected Python entry point and a read-only
GitHub CLI dry-run command. The command performs tracker reads but does not claim issues, mutate
labels, create branches, or submit Cloud tasks.

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

The dispatcher processes untrusted issue text and agent-generated code. It must never execute
issue-provided commands on its control host, expose production credentials to Cloud tasks or PR CI,
or interpret an unknown external state as success. Any ambiguous dispatch or delivery state is
blocked for human reconciliation.
