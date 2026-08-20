# Development

## Current implementation boundary

The core and its complete test suite remain environment-independent. Tests must run without
Linux-only isolation, network access, GitHub/Slack/OpenAI credentials, a running SSH server, or
third-party Python packages. A write-enabled live entry point now exists, but it is never invoked by
the test suite.

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
- outbound-only Slack message/receipt models, idempotency keys, and a payload-hash outbox;
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
  by each Turn, plus the frozen Issue path policy, without persisting Prompt content;
- a recovery-first, single-process Control Host sweep that prepares a source bundle before claiming a
  new Issue, freezes a stable post-claim snapshot, starts or resumes exactly one Turn, and publishes
  an exact verified checkpoint through the fixed Publisher port;
- a protected GitHub mirror refresher that fetches one configured base branch into a fixed internal
  ref, keeps credentials out of argv and persistent Git config, retries only that read-only fetch
  once inside the original total timeout, and composes with the exact-source
  bundle builder;
- strict, secret-free SSH runtime configuration and a dependency assembly boundary for GitHub,
  mirror, quarantine, fixed SSH transport, process lock, and the single sweep;
- a strict GitHub CLI Draft PR adapter and delivery coordinator that read back the exact task branch,
  bind one PR in SQLite, upsert one fixed Issue status comment, and recover lost write receipts before
  changing the terminal Issue label;
- merged-PR reconciliation that verifies the bound PR and exact persisted head SHA, commits the local
  completed tombstone first, and then retries only the fixed Issue comment/label projection;
- a provider-independent Slack coordinator that creates one root before Codex starts, binds its
  receipt atomically, projects the link to GitHub, and retries terminal reports without replaying the
  Runner or Publisher;
- a read-only `ssh-preflight` that checks pinned local tools, plans recovery before new work, and
  evaluates SSH-labelled candidates against a migrated temporary SQLite snapshot;
- a double-opt-in `ssh-run-once` CLI whose Git/gh/OpenSSH version checks and local SQLite integrity check
  complete before the sweep can claim an Issue;
- an isolated `codex_dispatcher.fixture_fault_cli` source-tree entry that is hard-coded to the
  private README-only Fixture and can discard exactly one successful START, Publisher, Draft PR, or
  Issue-comment receipt, or terminate one exact spawned SSH client process group only after a
  second read-only STATUS proves durable Runner acceptance, after an exact recovery-stage preflight
  and private SQLite backup;
- an isolated `codex_dispatcher.fixture_process_cli` parent/child entry that sends `SIGKILL` only
  after an exact post-claim private-pipe handshake and proves no WorkItem, Turn, or Runner call was
  reached.

The fixed OpenSSH argv/byte-stream adapter is covered by isolated unit tests, and the installed
Runner protocol has also completed the disposable SSH/real-Codex fixture recorded in
`docs/live-test-evidence.md`. The offline Control Host sweep and runtime assembly are covered through
injected fakes, including Publisher and Draft PR lost-receipt, Issue projection ordering, and
post-record crash recovery. A bounded live happy-path run has now exercised the real GitHub claim,
trusted-mirror fetch, SSH Runner, Publisher push, unique Draft PR, Issue projection, and Fixture CI.
An immediate second write-enabled sweep returned idle, with unchanged SQLite rows and GitHub state;
it did not create a second Turn, session, push, PR, comment, or workflow run. A second controlled live
Fixture Issue has now exercised Publisher, Draft PR, and Issue-comment receipt loss.
The recovery retained one WorkItem/session/Turn/branch/PR/comment and returned preflight to idle; it
also exposed and fixed the ordering of terminal WorkItem recovery from a still-running Issue.
Fixture Issue `#10` then proved exact post-claim `SIGKILL` and ordinary orphan-claim recovery, while
Issue `#12` proved a lost START receipt followed by guarded `STATUS` then `EXPORT` without replaying
START/RESUME. After the maintainer merged its exact bound PR `#13`, the normal dispatcher path also
proved the live `completed` tombstone and ordered fixed-comment/`agent:completed` projection; a
repeated sweep returned idle. Fixture Issue `#14` subsequently proved a real OpenSSH client-process
`SIGKILL`: the hook required the durable local WorkItem/Turn and a separate STATUS proof before
fresh argv/PID/PGID/SID validation and exact process-group termination. The original Turn stayed
`reconciling`, recovery used only `STATUS, EXPORT`, and one WorkItem/Turn/session/branch/PR/Actions
run survived two idle sweeps. After explicit review and merge of PR `#15`, the normal completion
path also projected Issue `#14` to `agent:completed` after the local tombstone and fixed comment;
the task branch remained and a repeated sweep was idle. Slack root/result receipt loss and outbox
recovery are covered through an idempotent fake publisher. The guarded live-only path then discarded
a fully validated real root receipt and result receipt at their exact durable SQLite stages for
Fixture Issue `#18`. Ordinary recovery reused the same provider receipts and preserved one
WorkItem/Turn/session/branch/Draft PR/Actions run; independent Runner STATUS was finished and both
preflight and a repeated sweep were idle. The real Slack publisher contract was separately proven
by an exact-retry live fixture that returned one receipt and left one visible message. An earlier
normal task lifecycle also delivered one root/result thread with the same 1:1 identities.
The live-only completion fixture now has offline and live coverage for two ordered failure points.
After an explicitly authorized operator merge of Fixture PR `#19`, it discarded the exact
fixed-comment response only after the local completed tombstone was durable, then discarded the
completed-label response only after GitHub read-back. Both stages rejected Source, Runner, Git
Publisher, and Slack Publisher calls and required the exact merged PR binding. Final preflight and
two ordinary sweeps were idle with the original identities preserved.
The Runner must not receive GitHub write or production credentials.

Explicitly deferred:

- systemd/timer activation of the one-sweep entry point;
- systemd deployment;
- Docker isolation on the Runner;
- any dispatcher-initiated merge, deployment, release, or production access.

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

The SSH preflight uses real GitHub reads but has no write opt-in and never assembles the mirror,
Publisher, or Runner transport:

```bash
PYTHONPATH=src python3 -m codex_dispatcher ssh-preflight \
  --config /absolute/path/dispatcher.toml --json
```

It returns `ready_candidate`, `ready_recovery`, `idle`, or `blocked`. `blocked` exits nonzero. An
existing configured database is copied with SQLite backup and migrated only in a disposable
directory; a missing configured database is not created. The JSON always reports
`authorizes_apply=false`: preflight does not hold the Dispatcher lock, and a later write-enabled
sweep must re-read and revalidate state under that lock.

## Test conventions

- Unit tests must not use the network.
- External executables are replaced with temporary fake scripts.
- Time, UUIDs, paths, command results, and external responses are injected where they affect
  determinism.
- Failure-path tests assert that no external write was attempted.
- SSH preflight tests prove recovery-first ordering, SSH-only candidate selection, ambiguous-claim
  blocking, and non-mutation of both existing and missing configured databases.
- Control Host sweep tests use fake tracker/source/Runner/Publisher ports and exercise process-lock
  contention, claim loss, snapshot drift, interrupted PREPARE/START, ambiguous push, recorded
  publication recovery, Draft PR receipt loss, merged-PR completion, and Issue projection retry
  ordering.
- Git tests use temporary local repositories and never a configured GitHub remote.
- Runner tests operate on JSON/JSONL fixtures and temporary directories, not a real SSH daemon.
- Publisher tests push only to temporary local bare repositories and never to GitHub.
- Slack tests cover outbound rendering, durable payload identity, atomic root binding, terminal
  projection ordering, and lost-receipt retries; no inbound or message-history interface exists.
- Test fixtures may contain fake tokens, but never copy a real credential into a fixture.
