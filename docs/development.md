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

The offline core also includes the fixed OpenSSH argv/byte-stream adapter. Its tests mock the
subprocess boundary and never connect to a host. The next phase is installing these components on a
disposable Linux Runner, preparing task-specific Codex authentication, and performing the first
networked SSH/real-Codex fixture. The Runner must not receive GitHub write or production
credentials.

Explicitly deferred:

- networked SSH execution;
- live real-account `codex exec` invocation and authentication bootstrap;
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
- Git tests use temporary local repositories and never a configured GitHub remote.
- Runner tests operate on JSON/JSONL fixtures and temporary directories, not a real SSH daemon.
- Publisher tests produce a publication plan or rejection; they do not push.
- Slack tests cover only outbound rendering and deduplication; no inbound interface exists.
- Test fixtures may contain fake tokens, but never copy a real credential into a fixture.
