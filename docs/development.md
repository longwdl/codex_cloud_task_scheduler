# Development

## Scope of the first implementation phase

The first phase is deliberately environment-independent. It must run without Linux, GitHub
credentials, a Codex Cloud environment, network access, or third-party Python packages.

Implemented in the current snapshot:

- strict TOML configuration;
- deterministic run state transitions;
- SQLite migrations, constraints, events, integrity checks, and online backup;
- issue task-spec parsing and path policy;
- immutable prompt snapshots and hashes;
- secret redaction and safe subprocess execution;
- tracker and executor ports with offline fakes;
- read-only candidate validation, priority ordering, and capacity planning;
- local `doctor` and database `status` commands, plus a dependency-injected dry-run entry point.

Explicitly deferred:

- real `gh` writes;
- real `codex cloud exec`, status reconciliation, diff, or apply;
- branch push and draft PR creation;
- systemd installation or Linux hardening;
- any merge, deployment, or production access.

## Local verification

The supported runtime starts at Python 3.12. The tests use `unittest` so a clean interpreter is
enough:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src tests
```

Run the offline preflight check:

```bash
PYTHONPATH=src python3 -m codex_dispatcher doctor --json
```

Missing `gh` is expected during this phase and is reported as a future integration prerequisite,
not as an offline-core failure.

The repository does not yet select or add a third-party build backend. Run it from `src/` as shown
above; packaging can be added as a separate, reviewable tooling decision.

## Test conventions

- Unit tests must not use the network.
- External executables are replaced with temporary fake scripts.
- Time, UUIDs, and external responses should be injectable where they affect determinism.
- Failure-path tests must assert that no external write was attempted.
- Test fixtures may contain fake tokens, but never copy a real credential into a fixture.
