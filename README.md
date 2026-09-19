# Codex SSH CLI Task Dispatcher

This repository implements a fail-closed, unattended path from a reviewed GitHub Issue to an
isolated Codex CLI WorkItem and a Draft Pull Request.

The deployed architecture has one execution path: a Linux Control Host coordinates a separate
Linux Runner over a fixed SSH protocol. Codex runs only inside a rootless Docker container on the
Runner. The Dispatcher never merges, releases, deploys, or executes Issue-provided shell commands
on the Control Host.

## Current boundary

- One GitHub Issue maps to one durable WorkItem, task branch, Runner directory, and Slack thread.
- WorkItem reports use the configured Issue channel; lifecycle, systemd, GitHub API, backup/restore,
  and Runner-capacity alerts use a separate system channel.
- A WorkItem may have several Turns and several bounded SessionGenerations.
- The primary Runner agent is Sol. It may select only configured direct-child agent profiles; the
  Runner records metadata-only delegation evidence and rejects policy drift.
- Only `exec:ssh-cli` is an executable Issue label.
- Repository admission is an explicit class/profile matrix. The ordinary runtime currently admits
  only the reviewed Fixture profile. Higher-value admission remains hard false; its separate manual
  canary cannot widen normal admission.
- SQLite migrations 1 through 21 are the durable ledger. Historical result and generation shapes
  remain readable only where disaster recovery needs them; no current workflow creates old-format
  rows.

## Components

```text
GitHub Issue and comments
          |
          v
Control Host
  admission -> recovery-first planner -> SQLite -> Publisher -> GitHub/Slack
          |
          | fixed SSH protocol v2
          v
Runner Host
  registry + ext4 image + generation auth/home -> rootless Docker -> Codex CLI
```

The Control Host owns repository policy, GitHub and Slack credentials/channel routing, the trusted mirror,
quarantine, Publisher, SQLite, backups, health checks, and release receipts. The Runner receives a
self-contained exact-base bundle and metadata-bound requests. It has no GitHub write credential,
no host Docker socket inside the Turn container, and no access to another WorkItem.

See [Architecture](docs/architecture.md) for invariants, the
[state machine](docs/state-machine.md) for terminal completion/discard behavior, and
[Implementation, deployment, and acceptance](docs/implementation-deployment-test-plan.md) for the
operational sequence.

## Normal lifecycle

1. `ssh-preflight` performs exact tool/config/database checks and plans recovery before new work.
2. A maintainer-reviewed `agent:ready` Issue is admitted only when repository class, recovery
   profile, target-readback profile, immutable policy digest, paths, checks, and approver agree.
3. The Control Host freezes Issue/comment/task-spec evidence and creates or reuses the single
   WorkItem.
4. The Runner verifies and prepares an exact source bundle inside the WorkItem's bounded image.
5. A Turn starts or resumes the bound generation. `codex login status` must pass immediately before
   every START/RESUME.
6. The Runner validates structured AgentResult, trusted Git state, resource use, and delegation
   evidence before returning a checkpoint.
7. The Publisher verifies the exported bundle, changed paths, exact HEAD, and task branch before
   pushing and creating or recovering one Draft PR.
8. The completion gate imports exact-HEAD Actions evidence, evaluates structured acceptance
   criteria, and requires a fresh Audit generation when configured.
9. A passed WorkItem enters `review`. An exact merged PR projects `agent:completed` and closes the
   Issue as `completed`. A trusted maintainer `agent:discard` event freezes new work, closes any
   exact unmerged PR, records the discard, and closes the Issue as `not_planned`.
10. Closed Issues remain audit indexes. Retention may archive only an eligible completed/discarded WorkItem through an exact Runner
    tombstone. Branch cleanup and release/image reclamation use separate exact receipts.

Ambiguous START, STATUS, publication, GitHub, Slack, archive, or external identity never causes a
blind replay. It becomes an explicit recovery action or `blocked`.

## Runtime commands

Read-only planning and inspection:

```bash
PYTHONPATH=src python3 -m codex_dispatcher doctor --config config/dispatcher.example.toml --json
PYTHONPATH=src python3 -m codex_dispatcher status --database /path/to/state.db --json
PYTHONPATH=src python3 -m codex_dispatcher ssh-preflight --config /etc/codex-dispatcher/config.toml --json
PYTHONPATH=src python3 -m codex_dispatcher lifecycle-health --config /etc/codex-dispatcher/config.toml --json
```

The write sweep requires both CLI and environment opt-in and is normally invoked only by the fixed
systemd wrapper:

```bash
CODEX_DISPATCHER_ENABLE_SSH_WRITES=1 \
PYTHONPATH=src python3 -m codex_dispatcher ssh-run-once \
  --config /etc/codex-dispatcher/config.toml --apply --json
```

Do not copy this into an Issue or make it an Issue-controlled argument. Recovery, abandonment,
higher-value canary, release, rollback, and reclamation commands have additional independent gates.

## Verification

Python 3.12 or newer and only the standard library are required.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src tests
git diff --check
```

## Documentation

- [Architecture](docs/architecture.md)
- [State machine](docs/state-machine.md)
- [Development](docs/development.md)
- [Implementation, deployment, and acceptance](docs/implementation-deployment-test-plan.md)
- [Owner preparation checklist](docs/owner-preparation-checklist.md)
- [Runner authentication and failed-session recovery](docs/runner-auth-and-session-recovery.md)
- [Current-schema disaster recovery and exact reclamation](docs/schema18-disaster-recovery-and-runner-reclamation.md)
- [Higher-value manual canary](docs/higher-value-canary.md)
- [Live test evidence](docs/live-test-evidence.md)

`docs/live-test-evidence.md` is append-only operational evidence. Historical sections describe the
system that produced each receipt; they are not current interfaces or implementation requirements.

## Explicit non-goals

- automatic merge, release, deployment, or production-infrastructure mutation;
- Slack inbound control;
- concurrent Turns or multiple active Dispatchers;
- production credentials, private-network access, or production self-hosted CI on the Runner;
- treating Agent output as CI, acceptance, authorization, or audit fact;
- broad cleanup such as `docker image prune` or unbound branch deletion.
