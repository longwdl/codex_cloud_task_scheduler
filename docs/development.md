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
- local `doctor` and database `status` commands, plus dependency-injected and GitHub CLI dry-run
  entry points;
- controlled GitHub claim/state/comment primitives with write-after-read verification;
- exact tool-version and empty Codex Cloud environment contract checks;
- safe local Git mirror/worktree preparation with hooks, custom protocols, submodules, and
  repository attribute drivers disabled or rejected;
- crash-recoverable initial task-branch publication: the immutable base SHA and deterministic
  branch name are committed to SQLite before the guarded remote ref creation, then verified before
  the run advances to `branch_prepared`.

Explicitly deferred:

- unattended `gh` write orchestration (write primitives are not exposed by the CLI);
- real `codex cloud exec`, status reconciliation, diff, or apply (all Cloud writes fail closed);
- production GitHub branch-write orchestration and draft PR creation;
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
PYTHONPATH=src python3 -m codex_dispatcher doctor \
  --config config/dispatcher.example.toml --contract --json
```

The networked dry-run against a private repository additionally requires `gh` authentication
through `GH_TOKEN` or `GITHUB_TOKEN`. The token remains outside the TOML configuration and is passed
only in the child-process environment. `doctor` still treats a missing `gh` executable as a later
integration prerequisite rather than an offline-core failure.

Git workspaces can authenticate over HTTPS with a token kept in the child-process environment, or
over SSH using an explicitly supplied agent socket and protected SSH config file. The dispatcher
does not inherit the caller's full environment or home directory; deployment must configure one
credential path deliberately.

The `--contract` mode is also read-only. It checks exact `git`, `gh`, and `codex` versions and
proves that each configured Cloud environment is visible through `codex cloud list --json`. The
pinned CLI's non-empty task schema and write commands remain disabled until dedicated contract
fixtures cover them.

The repository does not yet select or add a third-party build backend. Run it from `src/` as shown
above; packaging can be added as a separate, reviewable tooling decision.

## Test conventions

- Unit tests must not use the network.
- External executables are replaced with temporary fake scripts.
- Time, UUIDs, and external responses should be injectable where they affect determinism.
- Failure-path tests must assert that no external write was attempted.
- Git write/recovery tests use a temporary local bare repository and never a configured GitHub
  repository.
- Test fixtures may contain fake tokens, but never copy a real credential into a fixture.
