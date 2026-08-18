# Development

## Current implementation boundary

The current phase is environment-independent. It must run without Linux-only isolation, network
access, GitHub/Slack/OpenAI credentials, a running SSH server, or third-party Python packages.

The existing repository already contains:

- strict TOML configuration;
- deterministic legacy run transitions and additive SQLite migrations;
- issue task-spec parsing, path policy, prompt snapshots, redaction, and safe subprocess execution;
- tracker and executor ports with offline fakes;
- read-only GitHub candidate planning;
- safe local Git mirror/worktree preparation and applied-change validation;
- a disabled Codex Cloud adapter retained for migration compatibility.

The environment-independent core now additionally contains:

- stable WorkItem and Turn domain objects;
- deterministic branch and runner-directory identity per Issue;
- one-session binding and ordered Turn planning;
- versioned SSH Runner request/result contracts;
- strict Codex JSONL event parsing;
- Publisher request and pure publication-plan validation;
- additive SQLite persistence for WorkItems and Turns;
- outbound-only Slack message models and idempotency keys;
- a strict Runner response and artifact-manifest contract;
- length-prefixed request/Prompt and response/artifact wire framing;
- a stateful fake SSH Runner that records only Prompt hashes and sizes;
- atomic WorkItem/Turn start and finalization boundaries;
- an active `reconciling` state that forbids blind Prompt replay;
- offline first-turn, same-session resume, export, verification, and publication orchestration;
- a self-contained Git bundle quarantine verifier using fixed, non-executing Git inspections;
- an exact-base source bundle builder that never mutates the trusted mirror;
- persistent Runner task directories with no Git remote and strict source import/export;
- an idempotent Codex Turn executor tested with a local fake executable;
- a no-argument, protected-config `codex-runner-v1` forced-command service and global Turn lock;
- a fixed-parameter Git Publisher tested against a local bare remote, including lost-receipt recovery
  and remote-race rejection.
- immutable GitHub Issue node/revision snapshots and SSH-only candidate selection;
- a provider-independent service joining WorkItem recovery, deterministic Turn prompts, and Runner
  invocation without GitHub/Slack writes;
- an atomic Prompt Turn-number check and a protected non-blocking Control Host process lock;
- read-only enumeration of dispatching/running Issues and a fail-closed recovery planner for orphan
  claims, active Runner reconciliation, terminal label repair, and pending publication;
- persisted Issue revisions, Prompt hashes, input HEADs, and the exact allowlisted comment IDs used
  by each Turn, without persisting Prompt content;
- a recovery-first, single-process Control Host sweep that prepares a source bundle before claiming a
  new Issue, freezes a stable post-claim snapshot, starts or resumes exactly one Turn, and stops at a
  Publisher checkpoint.

The fixed OpenSSH argv/byte-stream adapter is covered by isolated unit tests, and the installed
Runner protocol has also completed the disposable SSH/real-Codex fixture recorded in
`docs/live-test-evidence.md`. The offline Control Host sweep is now covered through injected fakes.
The next phase is trusted-mirror refresh and runtime wiring for live GitHub claim/reconciliation,
followed by explicit Publisher/Slack delivery. The Runner must not receive GitHub write or production
credentials.

Explicitly deferred:

- an unattended Control Host scheduling entry point and concrete trusted-mirror refresh provider;
- live GitHub claim/reconciliation in the SSH workflow;
- live GitHub Publisher writes and Draft PR creation;
- Slack API calls;
- systemd deployment;
- Docker isolation on the Runner;
- any merge, deployment, release, or production access.

## Architecture constraints for offline code

- One GitHub Issue maps to one WorkItem, stable branch, directory, Codex session, Slack thread, and
  Draft PR until completion.
- A WorkItem may have multiple ordered Turns; Turn identity never changes branch or session identity.
- Only one Turn may be active globally.
- Missing or conflicting Codex session state is blocked, never replaced automatically.
- Slack has no inbound adapter or command surface.
- Codex has no GitHub write credential.
- Publisher accepts only a WorkItem ID and expected full commit SHA. Repository, branch, remote, and
  local paths are trusted lookups, not caller input.
- Existing SQLite schema remains readable; migrations are additive.

## Local verification

The supported runtime starts at Python 3.12. Tests use only `unittest`:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src tests
git diff --check
```

Run the existing offline preflight check:

```bash
PYTHONPATH=src python3 -m codex_dispatcher doctor --json
PYTHONPATH=src python3 -m codex_dispatcher doctor \
  --config config/dispatcher.example.toml --contract --json
```

The existing Cloud contract command is historical migration code. It must not be expanded or treated
as the target executor contract.

## Test conventions

- Unit tests must not use the network.
- External executables are replaced with temporary fake scripts.
- Time, UUIDs, paths, command results, and external responses are injected where they affect
  determinism.
- Failure-path tests assert that no external write was attempted.
- Control Host sweep tests use fake tracker/source/Runner ports and exercise process-lock contention,
  claim loss, snapshot drift, interrupted PREPARE/START recovery, and publication checkpoints.
- Git tests use temporary local repositories and never a configured GitHub remote.
- Runner tests operate on JSON/JSONL fixtures and temporary directories, not a real SSH daemon.
- Publisher tests produce a publication plan or rejection; they do not push.
- Slack tests cover only outbound rendering and deduplication; no inbound interface exists.
- Test fixtures may contain fake tokens, but never copy a real credential into a fixture.
