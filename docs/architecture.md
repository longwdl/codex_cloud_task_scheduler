# Current architecture

## Purpose and safety boundary

The Dispatcher converts an explicitly reviewed GitHub Issue into bounded Codex CLI work and a
Draft Pull Request. It is recovery-first and fail-closed. It never merges, releases, deploys, or
executes Issue/repository text as Control Host shell input.

Current execution is SSH protocol v2 only. The ordinary runtime accepts only repositories admitted
by the fixed repository-class/profile matrix and Issues carrying the exact `exec:ssh-cli` label.
Higher-value repositories remain excluded from ordinary admission.

## Trust boundaries

### Control Host

The Control Host is the high-trust coordinator. It owns:

- protected TOML configuration and repository policy;
- GitHub and Slack credentials in root-owned environment files;
- the trusted mirror, source staging, quarantine, and Publisher staging;
- SQLite state, online backups, recovery drills, lifecycle health, and alert outboxes;
- exact release, rollback, archive, branch-cleanup, and reclamation receipts;
- a single global Dispatcher lock.

Issue bodies, comments, repository content, Runner replies, Agent output, Git diffs, GitHub API
responses, Actions results, and Slack responses are untrusted inputs. They are parsed into bounded
types before they may affect durable state.

### Runner Host

The Runner is a lower-trust, rebuildable execution host. A locked SSH account accepts only the fixed
Runner wrapper and protocol operations. Root owns releases, tool installations, configuration,
policy, schemas, the shared auth seed, and permanent receipts. Rootless Docker performs Turn
execution.

Each WorkItem receives:

- one registry identity and one dense bounded ext4 image;
- one repository directory and Runner state directory inside that image;
- a separate home/auth copy for each SessionGeneration;
- fixed read-only policy, tool, and schema mounts;
- a read-only container root, dropped capabilities, `no-new-privileges`, PID/CPU/memory/tmpfs
  limits, and proxy-only egress.

The Turn container receives neither the Docker socket, Runner-wide auth seed, another WorkItem,
Control credentials, nor GitHub write credentials.

### External systems

GitHub is the source of reviewed intent and the destination for task branches, Draft PRs, comments,
and state labels. Slack is a bounded outbound projection with two distinct destinations: WorkItem
root/result messages go to the Issue channel, while lifecycle-health alert/recovery episodes go to
the system channel. Slack is not a control plane. SQLite is the local coordination ledger but never
overrides contradictory external identity; contradictions block.

## Durable identity

The stable mapping is:

```text
1 GitHub Issue
  = 1 WorkItem
  = 1 task branch
  = 1 Runner directory/image identity
  = 1 Slack thread
```

A WorkItem is not a permanent Codex session. It may contain multiple Turns and multiple bounded
SessionGenerations:

```text
WorkItem
  SessionGeneration 1 (implementation or repair)
    Turn 1
    Turn 2
  SessionGeneration 2 (audit or replacement)
    Turn 3
```

The Issue node ID, repository, issue number, base branch/SHA, task branch, repository-policy digest,
and Runner directory form the immutable identity boundary. A request that disagrees with any of
them is rejected rather than rebound.

## Repository admission

Before claim, the planner evaluates:

- repository slug and class;
- configured recovery and target-readback profiles;
- immutable policy and target digests;
- exact Issue node ID and maintainer approval;
- one canonical `agent:ready` label and `exec:ssh-cli`;
- TaskSpec acceptance syntax, allowed paths, denied paths, and required checks;
- global single-Turn capacity and Runner disk admission.

Migration 20 stores the repository policy identity and exact recovery/readback evidence. A new
WorkItem cannot be claimed without current policy evidence. Historical pre-migration rows remain
readable for disaster recovery but cannot silently acquire a current policy.

The normal matrix admits the reviewed Fixture profile only. The higher-value canary requires an
isolated configuration, database, filesystem roots, exact Issue/node/base opt-ins, manual CLI, and
the shared global lock. It cannot enable normal higher-value admission.

## Recovery-first sweep

Every sweep follows the same ordering:

1. validate protected paths, ownership, mode, tool versions, credentials, database integrity, and
   the global lock;
2. read durable WorkItems, Turns, generations, outboxes, archives, branch cleanups, dispositions,
   follow-up intents, and external receipts;
3. plan at most one exact recovery action;
4. execute and reread that recovery, or return `blocked` on ambiguity;
5. only when recovery is idle, inspect one new Issue candidate;
6. perform at most one Turn or one terminal lifecycle action.

Recovery actions include orphan-claim repair, PREPARE retry, STATUS-only Turn reconciliation,
publication/PR/comment/Slack receipt recovery, completion projection, follow-up consumption, fresh
Audit rotation, inactive-Turn abandonment, disposition, archive, and branch cleanup.

A network error is not proof that a write failed. START is never retried after an ambiguous reply;
STATUS must prove the exact Runner state. Publisher, GitHub, Issue-channel Slack, archive, and deletion recovery
likewise use stable identity and read-back instead of blind replay.

## Source, Turn, and publication flow

### Source preparation

The Control Host fetches only the configured base ref into a trusted mirror, resolves one exact base
SHA, and builds a size/digest-bound self-contained Git bundle. The Runner verifies the bundle and
prepares only the WorkItem repository. Git hooks, filters, alternate object databases, submodules,
unexpected refs, and injected proxy/protocol configuration are rejected.

### SessionGeneration and Turn

The current runtime creates protocol-v2 SessionGenerations under explicit budgets for turns,
generations, tokens, age, repair cycles, audit cycles, and no-progress continuation. A generation
has a role (`implementation`, `ci_repair`, or `audit`), policy digest, baseline snapshot, dedicated
home/auth copy, and at most one Codex session ID.

Immediately before START/RESUME, the Runner executes the fixed `codex login status` check inside the
generation boundary. Failure or output drift rejects the request before Codex work. Login recovery
is an operator re-login; the Dispatcher does not edit tokens or invent refresh behavior.

The Runner invokes primary Sol with the fixed policy. Sol may work directly or select a configured
direct-child profile. The Runner reads only metadata deltas from the isolated Codex state database
and commits a delegation receipt with the Turn. Agent output is accepted only after strict JSONL,
schema, domain, Git, resource, and delegation validation.

### Publication

The Runner exports a bounded bundle only from the exact Turn output HEAD. The Control Host imports it
into quarantine, rejects unsafe Git structure/configuration, verifies allowed changed paths and
commit bounds, and prepares a PublicationPlan bound to the WorkItem and persisted policy.

The Publisher may update only the deterministic task branch. It verifies the remote ref before and
after the write, creates or recovers one Draft PR, records the exact PR/head/base identity, and then
projects the Issue comment/label and Issue-channel Slack report. It cannot write the base branch, tags, force
push, delete a ref through publication, merge, or deploy.

## Completion and autonomous follow-up

`completed` in AgentResult is only a completion candidate. The completion gate requires:

- the persisted publication ledger and remote task branch to agree on one HEAD;
- all configured Actions workflows to have one acceptable exact-head result;
- every structured acceptance criterion to be supported by trusted evidence;
- the changed-path set to match the durable publication facts.

Pending evidence waits without restarting Codex. Failed or ambiguous evidence blocks or creates one
bounded repair follow-up. AgentResult never upgrades itself into CI or acceptance evidence.

When configured, a passed implementation candidate rotates to a fresh Audit generation with a new
session and immutable completion-candidate Handoff. Audit must leave trusted Git unchanged; a
fixable gap rotates to `ci_repair`, not a writable Audit continuation.

Checkpoint, CI-failure, and Audit-gap continuation uses a schema-19 immutable follow-up intent. The
intent binds source Turn, target role/generation, HEAD, cause, and evidence digest. The next Turn
must consume the planned intent atomically. Health alerts on stale intents, generation/Turn binding
conflicts, and exact no-progress exhaustion.

## Terminal lifecycle and storage

An exact merged PR at the persisted head may move a WorkItem to `completed`. A maintainer
`agent:discard` event creates an immutable `abandoned` or `superseded` disposition. Blocked,
needs-input, review, or active WorkItems are never reclaimed merely because they are old.

After configured retention, the Control Host prepares one schema-12 archive request. The Runner
rechecks clean exact HEAD, terminal Turns, inactive containers, registry identity, mount state, and
storage shape under the global lock. It writes a permanent metadata-bound tombstone before staging
and removing only that WorkItem image. `ARCHIVE_STATUS` resolves lost replies. Registry, tombstone,
Control ledger, GitHub, Slack, and receipts remain.

Task-branch cleanup is a separate exact GitHub operation after its own retention and rollout
cutover. Higher-value automatic branch deletion remains denied. Release and image reclamation is
also separate: a read-only inventory binds current/rollback releases, configured image digest,
WorkItem/registry references, and rollback receipts to a plan SHA. Apply requires a new exact
reinspection and permanent receipt; broad prune commands are forbidden.

## Operations, health, and disaster recovery

The Control Host runs four oneshot timers: Dispatcher, lifecycle health, online backup, and restore
drill. The Runner runs capacity and exact reclamation-planning timers. Health reads durable
lifecycle state, systemd state, GitHub API metrics/cursor age, archive/branch backlog, follow-up
state, Runner capacity, and the strict latest reclamation status. Slack alert and recovery
projection uses a dedicated durable outbox and only the configured system channel; it never reuses
a WorkItem thread or the Issue channel.

Backups use SQLite Online Backup, mode `0600`, integrity validation, atomic publication, and bounded
rotation. Restore drills use an isolated temporary database and verify integrity, foreign keys, and
the exact migration ledger. The full disaster-recovery drill additionally reconciles the release
and operational-handoff receipts, Control/Runner release identity, schema-v2 Runner release/image
references and their apply receipt, current planner status/unit digests, Runner tombstones and
absences, GitHub, and Slack. It rebuilds isolated empty Control and Runner application filesystems
without replacing either online environment.

Release activation is Runner-first and Control-second, guarded by inactive service/locks, exact
archive digest, both-host tests, prior links, database backup, and a permanent transaction receipt.
Timers remain stopped for the observed handoff sweep. Rollback is exact and receipt-bound; after a
new Dispatcher invocation, state/external recovery may require the preserved backup rather than a
symlink-only rollback.

After the sweep, backup, restore drill, lifecycle health, and all timers have been observed, a
separate immutable handoff receipt records the operational boundary. It does not rewrite the
release transaction receipt. The handoff command is idempotent for the same commit, makes no
GitHub, Slack, Runner-asset, or online-database write, and refuses incomplete or stale evidence.

## Compatibility boundary

Current code does not expose a second executor, old dry-run path, or old Run API. Migration 1 still
contains inert `runs`/`run_events` tables because removing committed SQLite history would make
backup identity destructive. They are not read or written by the application.

Historical AgentResult shapes, protocol-v1 Runner receipts, legacy generation bindings, archive
storage classifications, and schema-1 Handoffs remain read-only recovery inputs because retained
fixture evidence still contains them. New work always uses current policy evidence, protocol v2,
current AgentResult validation, bounded images, and current Handoffs. Compatibility code must not
be used to admit or create new old-format state.

## Core invariants

- At most one active Turn globally and one live generation per WorkItem.
- Recovery precedes new work; ambiguity blocks.
- Exact identities are persisted before external writes and reread afterward.
- Agent text is advisory, never authorization or acceptance fact.
- Runner cannot publish; Publisher cannot run Codex; Slack cannot control execution.
- No automatic merge, release, deployment, branch deletion outside exact retention, or broad disk
  cleanup.
- Ordinary higher-value admission remains false until a separate reviewed release changes the code
  matrix and completes repository-specific acceptance.
