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

There is no real GitHub write adapter, Codex Cloud submission, systemd deployment, automatic PR
creation, merge, or deployment in this revision.

Candidate planning is exposed as a dependency-injected Python entry point for offline tests. The
user-facing `run-once --dry-run` CLI remains deferred until the read-only GitHub adapter exists.

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
```

A build backend and wheel packaging are intentionally deferred until that tooling choice is
approved; they are not needed for the offline core.

## Documentation

- [Implementation, deployment, and acceptance plan](docs/implementation-deployment-test-plan.md)
- [Owner preparation checklist](docs/owner-preparation-checklist.md)
- [Development notes](docs/development.md)
- [Architecture baseline](docs/architecture.md)

## Security model

The dispatcher processes untrusted issue text and agent-generated code. It must never execute
issue-provided commands on its control host, expose production credentials to Cloud tasks or PR CI,
or interpret an unknown external state as success. Any ambiguous dispatch or delivery state is
blocked for human reconciliation.
