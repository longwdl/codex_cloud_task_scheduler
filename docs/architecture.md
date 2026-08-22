# Architecture baseline

## Purpose

The system dispatches explicitly reviewed GitHub issues to replaceable Codex CLI session generations
on a dedicated Linux runner. GitHub remains the project view and the only human-input channel. Slack is
an outbound-only view of execution details. The system never merges, deploys, or grants the Codex
process GitHub write credentials.

## Trust boundaries

```text
Untrusted GitHub issue and maintainer context
                    │
                    ▼
Linux control host (high trust)
├── Dispatcher: validation, lifecycle, SQLite, GitHub Issue/PR metadata
├── Publisher: fixed-parameter import and task-branch push
├── SlackReporter: outbound status only
└── GitHub credentials and audit state
                    │ restricted SSH requests and artifact transfer
                    ▼
Dedicated Linux runner (low trust and rebuildable)
├── Codex CLI, with no GitHub write credential
├── one persistent repository/state directory per work item
└── one protected, runner-wide CODEX_HOME at /srv/codex-runner/app for ChatGPT auth
    and Codex session state
```

The Dispatcher and Publisher run on the same control host. They remain separate responsibilities:
the Dispatcher cannot turn issue text into shell or Git arguments, while the Publisher accepts only
a recorded work-item identifier and expected commit identity. Repository, remote, and branch values
are loaded from trusted state rather than accepted from the caller.

The first SSH Runner phase intentionally adds no Docker or per-task operating-system isolation.
Each work item uses a different directory. The entire runner is therefore the disposable security
boundary, not the directory. Docker isolation is a later hardening phase and must not be described
as an existing control.

## Stable work-item identity

One executable GitHub issue has exactly one active development identity until completion:

```text
1 GitHub Issue
= 1 WorkItem row
= 1 stable task branch
= 1 runner task directory
= N ordered, replaceable Codex CLI session generations
= 1 Slack thread
= 0 or 1 Draft PR
```

Human clarification, planning, implementation, testing, and review feedback are separate turns of
the same work item. They do not create a new branch, directory, Slack thread, or PR. A bounded
session generation may be retired only at an idle, published Git checkpoint; its replacement starts
from the same stable branch, directory, and recorded HEAD.
After the PR is merged and the issue is completed, a bug or change request must use a new issue and
therefore a new work item.

The stable branch name is deterministic from repository and issue identity. It never contains an
attempt number. A suitable form is:

```text
codex/issue-<issue-number>-<stable-issue-key-prefix>
```

Migration never replaces a branch that was already durably bound under the earlier scheduler. An
existing binding may be imported only after the legacy SQLite anchor and the exact remote ref both
agree on repository, Issue, branch, base SHA, and head SHA. It is marked `migrated`; new Issues use
the derived identity. Both forms remain immutable after persistence.

## Work item and turn

`WorkItem` is the long-lived aggregate. `SessionGeneration` is a replaceable execution cache.
`Turn` is one bounded invocation that belongs to exactly one generation.

```text
WorkItem
  repository, issue_number, issue_node_id
  branch, base_branch, runner_directory
  codex_session_id, slack_channel_id, slack_thread_ts
  pr_number, last_published_sha, lifecycle_state

SessionGeneration
  work_item_id, generation_number, role, lifecycle_state
  start_head_sha, last_published_sha, policy_sha256, rotation_reason
  codex_session_id, immutable semantic baseline digests

Turn
  work_item_id, turn_number
  issue_revision, included_comment_ids, issue_allowed_paths, prompt_sha256, input_head_sha
  started_at, finished_at, status
  output_sha256, output_head_sha, result_status, result_summary, error_code
```

Only one turn may be active globally. A single dispatcher process and an operating-system startup
lock enforce this; SQLite leases are not used to enable concurrent submitters. `reconciling` is an
active state: an ambiguous SSH interruption retains the global slot until a read-only `status`
request proves the remote outcome. The original Prompt is never replayed during reconciliation.

The live Control Host entry point is a one-sweep command, not an interactive shell. It requires both
the `--apply` argument and `CODEX_DISPATCHER_ENABLE_SSH_WRITES=1`; without either, configuration and
adapters are not loaded. Runtime TOML contains only fixed paths, host identity, timeouts, and storage
locations. GitHub credentials remain environment-only and are never passed to SSH.
The live command accepts only an absolute config path whose parent and file are owned by root or the
Dispatcher user, non-symlink, and non-group/world-writable.
The SQLite parent, database, WAL, and SHM files must be owned by the Dispatcher user and are checked
against the non-symlink/non-group-or-world-writable boundary before SQLite opens them.

On the Linux Control Host, systemd activates that same one-sweep entry through a fixed, argument-free
wrapper. The service is `Type=oneshot`; a monotonic timer starts the first sweep after boot and waits
until the preceding service is inactive before beginning the next interval. systemd serialization,
the non-blocking process lock, and SQLite constraints are independent single-instance controls.
Credentials enter only through a root-owned mode-`0600` `EnvironmentFile`; they do not appear in the
unit, wrapper argv, TOML, or release tree. The unit drops privileges to the stable Dispatcher user,
removes capabilities, makes the system tree read-only, and allows writes only below the Dispatcher
state and runtime directories. It retains only the network families needed for GitHub, Slack, and
the fixed SSH Runner.

A separate network-isolated oneshot creates daily SQLite Online Backups while the Dispatcher may be
active. It checks the live database before copying, validates the completed backup, and only then
atomically publishes one mode-`0600` file in the protected backup directory. The backup path is not
configurable from a timer invocation, receives no provider credential, and never deletes prior
backups; retention remains an explicit operator policy.

## Input and output channels

GitHub is the only human-input channel. The scheduler accepts a change only after an allowed
maintainer explicitly moves the issue into an executable state. Only issue content and allowed
maintainer comments beginning with `/codex-context` enter a prompt snapshot.

Slack is output-only:

- no Slack message, reaction, button, command, or modal can trigger or modify a task;
- the service does not subscribe to Slack message events;
- the service has no conversation-history or message-search adapter;
- one project channel contains one thread per work item;
- the GitHub issue stores a direct link to the Slack thread;
- Slack contains redacted status, questions, summaries, tests, and GitHub links, not full prompts,
  reasoning traces, credentials, or full diffs.

Before the first Codex Turn starts, the control host persists the root report key and payload hash,
publishes the root through an idempotent outbound-only port, and atomically binds the returned channel
and root timestamp. It then exposes the validated Slack permalink in the fixed GitHub status comment.
Terminal reports use a Turn-scoped key and the same root timestamp. The outbox stores hashes and
receipts, not report text. An ambiguous response is retried only through a publisher contract that
must return the original receipt for the same key and payload; a real provider adapter stays disabled
until that behavior is proven without granting message-history access.

The provider adapter maps the durable delivery key to a stable UUID `client_msg_id`, sends only
escaped top-level text with Slack markup, automatic mention expansion, unfurling, and thread
broadcast disabled, and then resolves the exact message through `chat.getPermalink`. It accepts only
bounded JSON receipts from the fixed Slack HTTPS origin and rejects redirects. Transport loss,
malformed success responses, Slack internal errors, and a missing permalink after a confirmed post
remain ambiguous; they are never interpreted as delivery success.

## Codex CLI session protocol

The first turn of each generation runs `codex exec --json --dangerously-bypass-approvals-and-sandbox` with the prompt
on standard input. The rootless per-WorkItem container is the external execution boundary. The
Dispatcher captures
the `thread.started.thread_id` event and binds it exactly once to that generation. Later turns in
the same generation run
`codex exec resume <session-id> --json --dangerously-bypass-approvals-and-sandbox -` from the same
repository directory and the same WorkItem `CODEX_HOME`. The session ID selects the exact Codex
context. Protocol v2 uses a generation-local Codex home and immutable generation/policy binding.
The host seeds one WorkItem-local writable `codex-home/auth.json`; its binding record stays
outside container mounts, and the Runner-wide auth seed is never mounted. The Codex invocation
remains unrestricted inside the bounded container.

`--ephemeral`, `resume --last`, and resuming a retired generation are forbidden. Missing,
conflicting, or ambiguous session state becomes `blocked`. A replacement session is created only by
the explicit bounded rotation policy after a durable task-branch checkpoint; it never repairs an
ambiguous active Turn by blind replay.

## Generation handoff and bootstrap

Retiring a generation and planning its replacement is one SQLite transaction. That transaction also
persists one canonical Handoff bound to the exact WorkItem, source and target generation IDs,
published HEAD, Issue/TaskSpec/context digests, target policy digest, and rotation reason. The target
generation cannot start without that Handoff, and its first Turn atomically binds the same Handoff to
the prompt receipt. A crash after rotation therefore resumes the same planned generation and same
durable Handoff rather than regenerating advice.

The Handoff has two deliberately separate JSON objects:

- trusted facts generated by the Dispatcher, verified Git publication ledger, and exact-HEAD
  GitHub Actions importer;
- `untrusted_advisory` copied from the prior structured Agent result.

Verified publication evidence stores the bundle digest, previous and published checkpoint SHA,
commit count, size, and mechanically computed changed paths. Handoff checkpoint heads are not
represented as a complete commit list; Bootstrap reconstructs commit history from Git. Immediately
before rotation, the Actions importer reads the task ref, lists workflow runs, then reads the ref
again. Only a unique `pull_request` run with the exact repository, task branch, current HEAD, and
configured workflow name can become `passed`, `pending`, or `failed` evidence. Ref drift, duplicate
same-name runs, malformed output, permission failure, or output truncation aborts rotation. The
Checks API is not used.

The acceptance evaluator recognizes only three explicit Issue predicates:

```text
- [AC-1] required-check: unit-tests
- [AC-2] changed-paths-within-allowed
- [AC-3] task-head-published
```

All ordinary acceptance prose remains a separate `manual` criterion with status `unverified`.
Unknown or malformed `[AC-n]` directives are rejected instead of silently becoming prose. An Issue
cannot widen the configured required-check set: a `required-check` name absent from repository
configuration stays `unverified`. Model-reported tests, paths, summaries, and next actions never
become CI or acceptance facts. Handoff schema 2 stores the structured evaluations and exact Actions
run provenance; schema 1 Handoffs remain readable for crash recovery.

The first full prompt of every replacement generation includes a fixed Bootstrap contract. Before
editing, the new primary Sol must read repository instructions, inspect HEAD and recent commits,
verify the Handoff against code/tests/Issue, reconstruct acceptance and check status, report
contradictions, and then continue the smallest actionable step. Bootstrap shares the first normal
START Turn; same-generation Delta prompts do not replay it or the Handoff. The full prompt hash binds
the exact Bootstrap and Handoff bytes without changing the Runner v2 wire contract.

JSONL events are untrusted structured input. Output is bounded, parsed strictly, redacted, and
reduced to a schema-controlled result. Reasoning events and raw command output are not copied to
Slack or GitHub.

`--output-schema` constrains the final JSON shape, but domain invariants remain local. In
particular, `needs_input` must be non-empty exactly when `status=needs_input`; `completed` and
`blocked` require an empty question list. Prompt and schema descriptions state this rule, while the
Runner parser enforces it and never silently normalizes a conflicting Agent claim. Runner and JSONL
failures are persisted as bounded machine error codes, not provider output.

The SSH adapter ignores user SSH configuration, pins a protected `known_hosts` file and identity,
uses public-key batch authentication, disables agent/X11/all forwarding, proxy jumps, local
commands, and TTY allocation, and sends only the fixed
`/srv/codex-runner/bin/codex-runner-v1` command. Production uses
`ProxyCommand=none`. The current Mac-to-`s3` fixture may explicitly select one protected `assh`
executable and its owned home directory; the adapter then constructs only
`assh connect --port=%p %h` and supplies only that explicit `HOME` to let `assh` find its config.
It does not inherit the remaining Dispatcher environment, accept an arbitrary proxy command, or
accept any Issue-derived proxy value. JSON,
Prompt, source bundle, response, and result bundle bytes use versioned length-prefixed frames; none
enter SSH argv. `prepare` is the only operation allowed to carry a source bundle; `start/resume` are
the only operations allowed to carry a Prompt; `export` is the only response allowed to carry a
result bundle. Every artifact is bound to its request or manifest by exact size and SHA-256.

`codex-runner-v1` accepts no arguments. Its wrapper clears the inherited environment, disables the
Python user site and unsafe current-directory import path, and loads only a protected, exact-field JSON configuration,
derives the task directory from validated repository and Issue identities, and holds a global
non-blocking `flock` for the whole Codex invocation. A Turn request is persisted before Codex starts;
the canonical final reply is atomically persisted before the SSH response is written. Repeating the
same `turn_id` reads the stored reply and never runs Codex again. An incomplete durable record is
reported as `unknown`, never replayed.

The production SSH endpoint runs as the locked, non-sudo `codex-runner` account. Release source,
wrapper, Schema, versioned Codex distribution, configuration, external authorized-key file, and
sshd Match block are root-owned; only the shared Codex state, active lock, and WorkItem tree are
writable by the protocol account. Protected configured paths require a root-or-Runner-owned,
non-writable parent chain so an administrator-owned package directory cannot replace a trusted
executable. This protects the service boundary but does not isolate one direct Codex Turn from the
other WorkItems or shared authentication state.

## Source and publication flow

The runner has no GitHub write credential. For a never-seen Issue, the control host prepares the
exact source artifact before attempting the GitHub claim, so a mirror or bundle failure cannot leave
an Issue claimed without a recoverable base. The intended transfer is:

1. The control host copies the exact recorded base commit from its trusted local mirror into an
   isolated, self-contained source bundle; it does not mutate the mirror.
2. `prepare` transfers that bundle in the framed request. The Runner verifies its only advertised
   commit, rejects submodules and executable Git attribute drivers, creates the stable task branch,
   and configures no remote.
3. Codex modifies and tests the independent repository in the work-item directory.
4. Codex creates coherent local checkpoint commits and leaves the worktree clean.
5. The control host retrieves a self-contained Git bundle and a bounded result manifest over SSH.
6. The Publisher verifies bundle integrity, ancestry, exact head SHA, task branch, path policy,
   secret policy, size limits, and fast-forward behavior.
7. The Publisher pushes that exact SHA to the already-bound task branch.
8. The Dispatcher finds or creates the one same-repository Draft PR by exact task branch, reads it
   back, binds its number in SQLite, and then upserts the fixed Issue status metadata.

The durable `preparing -> ready` WorkItem event is the Control Host's proof that the Runner returned
the exact PREPARE acknowledgement. If PREPARE was definitively rejected, a later maintainer-approved
reactivation of the blocked or paused WorkItem rebuilds an exact bundle from the persisted Base SHA
before claim, moves the WorkItem back to `preparing`, and retries idempotent PREPARE. It cannot send
START until a new acknowledgement is durable. A blocked WorkItem that already has the acknowledgement
provenance follows the normal Turn reactivation path and is never re-prepared.

The Issue allowlist used in step 6 is stored with the Turn before Codex starts. Publication recovery
never reparses a later Issue body to widen that frozen policy. An ambiguous push remains
`checkpointing` and retries by remote read-back without restarting Codex. If the verified remote SHA
was already stored but the process stopped before the Turn became `published`, the durable SHA is
sufficient to finish the recorded result without exporting or pushing again.
If Draft PR creation succeeds but its response is lost, the next sweep finds the PR by the stable
task branch and binds it instead of creating another. The terminal Issue label is written only after
the PR binding and idempotent status comment succeed; a delivery interruption therefore remains a
recoverable dispatching/running Issue and cannot trigger a new Turn.
After a human merges the bound PR, recovery reads the PR by the immutable task branch and requires
the persisted PR number, repository, base branch, task branch, and exact `headRefOid` to agree with
the WorkItem's `last_published_sha`. A closed-but-unmerged PR, a different head SHA, a cross-repository
PR, or a prematurely completed/reactivated Issue is blocked. On an exact match, the dispatcher first
commits the irreversible local `completed` WorkItem tombstone, then idempotently rewrites the fixed
Issue comment while preserving any durable Slack permalink, and only then changes the label to
`agent:completed`. If either GitHub write loses its receipt, the next sweep retries only this Issue
projection; it cannot invoke the Runner or Publisher, create another PR, or reopen the WorkItem.
The dispatcher never performs the merge, closes the Issue, deletes the branch, or bypasses checks.
Likewise, an ambiguous Slack root response is reconciled before Codex starts. An ambiguous terminal
Slack response is retried from the durable Turn and outbox identity after the commit/PR work is
already complete; it cannot restart Codex, repeat a push, or create another PR. The terminal Issue
label is written only after the terminal Slack projection succeeds when that optional port is enabled.
Migration gives historical Turns an empty frozen policy. A pre-migration Turn that is still waiting
at a checkpoint is therefore rejected rather than inferring permissions from the current Issue.

For a new WorkItem, the mirror updater fetches only
`refs/heads/<configured-base>:refs/codex-dispatcher/base` from the GitHub URL derived from the
configured repository slug. It stores no remote, tags, or credentials. Git system/global config,
credential helpers, hooks, submodules, automatic maintenance, and interactive prompts are disabled;
an optional read token exists only in the bounded Git child environment. Recovery of an existing
PREPARING WorkItem never fetches a moving branch: it builds from the already-persisted base SHA.

The Publisher does not review semantics, edit files, stage changes, create commits, run repository
code, merge, or deploy. It never accepts a remote URL, arbitrary refspec, local path, Git option, or
shell fragment from the runner or issue.

## Core invariants

- One work item per repository and issue; no automatic second work item for the same issue.
- One stable branch, runner directory, Slack thread, and Draft PR per work item; session generations
  are ordered, bounded, and replaceable only at safe checkpoints.
- One active Codex turn globally.
- Every sweep holds the process lock, performs recovery before selection, and claims at most one new
  Issue.
- A turn never starts before its issue snapshot, prompt hash, input HEAD, and turn number are stored.
- A changed issue snapshot never receives a stale turn result automatically.
- A missing or conflicting active-generation Codex session ID is blocked, not guessed or replaced.
- The runner never receives GitHub write credentials or control-host credentials.
- Only the Publisher can push, and only an exact verified SHA to the recorded task branch.
- Only an already-human-merged, exact bound PR can move a WorkItem to the terminal `completed`
  tombstone; that tombstone cannot be reactivated.
- No force push, ref deletion, tag write, protected-branch write, merge, deployment, or release.
- Slack is never an input or control channel.
- Unknown external state is never success and is never resolved by blind replay.

## Runner isolation status

The initial dedicated Linux SSH Runner used direct host execution as an explicit Fixture-stage risk
acceptance. The configured `s3` Fixture path now executes Codex in a rootless per-WorkItem container
with bounded disk, resources, mounts, capabilities, session home, and proxy-only network access.
The host Docker socket is never mounted and containers never run with `--privileged`.

Each WorkItem home contains its own writable auth copy so Codex can use atomic refresh safely. A
host-only binding beside that home records the WorkItem and seed digest; the shared Runner auth seed,
session/tool bindings, other WorkItems, and Control Host state remain outside the container mount.

The runner still contains no production secrets, personal data, deployment credentials, inbound
SSH key to the Control Host, or mounted Control Host filesystem. Published commits, GitHub state,
Control Host SQLite, and Slack metadata remain outside its failure domain. Higher-value repositories
remain prohibited until the outstanding attack and recovery acceptance in
`deploy/runner/DOCKER.md` is complete; a successful private Fixture Turn is not a general admission.

## Dependency policy

The offline core continues to use only the Python standard library. SSH, Git, Codex, and GitHub are
external adapter boundaries. A Python dependency may be proposed only when it materially improves
correctness or security and its operational cost is reviewed first.
