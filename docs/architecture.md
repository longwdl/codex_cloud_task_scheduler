# Architecture baseline

## Purpose

The system dispatches explicitly reviewed GitHub issues to a persistent Codex CLI session on a
dedicated Linux runner. GitHub remains the project view and the only human-input channel. Slack is
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
└── one persistent directory and CODEX_HOME per work item
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
= 1 task-specific CODEX_HOME
= 1 Codex CLI session ID
= 1 Slack thread
= 0 or 1 Draft PR
```

Human clarification, planning, implementation, testing, and review feedback are separate turns of
the same work item. They do not create a new branch, directory, Codex session, Slack thread, or PR.
After the PR is merged and the issue is completed, a bug or change request must use a new issue and
therefore a new work item.

The stable branch name is deterministic from repository and issue identity. It never contains an
attempt number. A suitable form is:

```text
codex/issue-<issue-number>-<stable-issue-key-prefix>
```

## Work item and turn

`WorkItem` is the long-lived aggregate. `Turn` is one bounded invocation of the persistent Codex
session.

```text
WorkItem
  repository, issue_number, issue_node_id
  branch, base_branch, runner_directory
  codex_session_id, slack_channel_id, slack_thread_ts
  pr_number, last_published_sha, lifecycle_state

Turn
  work_item_id, turn_number
  issue_revision, prompt_sha256, input_head_sha
  started_at, finished_at, status
  output_sha256, result_summary, error_code
```

Only one turn may be active globally. A single dispatcher process and an operating-system startup
lock enforce this; SQLite leases are not used to enable concurrent submitters.

## Input and output channels

GitHub is the only human-input channel. The scheduler accepts a change only after an allowed
maintainer explicitly moves the issue into an executable state. Only issue content and allowed
maintainer comments beginning with `/codex-context` enter a prompt snapshot.

Slack is output-only:

- no Slack message, reaction, button, command, or modal can trigger or modify a task;
- the service does not subscribe to Slack message events;
- one project channel contains one thread per work item;
- the GitHub issue stores a direct link to the Slack thread;
- Slack contains redacted status, questions, summaries, tests, and GitHub links, not full prompts,
  reasoning traces, credentials, or full diffs.

## Codex CLI session protocol

The first turn runs `codex exec --json` with the prompt on standard input. The Dispatcher captures
the `thread.started.thread_id` event and binds it exactly once to the work item. Later turns run
`codex exec resume <session-id> --json -` from the same repository directory with the same
task-specific `CODEX_HOME`.

`--ephemeral` and `resume --last` are forbidden. Missing, conflicting, or ambiguous session state
becomes `blocked`; the scheduler never creates a replacement session automatically.

JSONL events are untrusted structured input. Output is bounded, parsed strictly, redacted, and
reduced to a schema-controlled result. Reasoning events and raw command output are not copied to
Slack or GitHub.

## Source and publication flow

The runner has no GitHub write credential. The intended transfer is:

1. The control host records a base commit and prepares source input for the runner.
2. Codex modifies and tests the independent repository in the work-item directory.
3. Codex creates coherent local checkpoint commits.
4. The control host retrieves a Git bundle and a bounded result manifest over SSH.
5. The Publisher verifies bundle integrity, ancestry, exact head SHA, task branch, path policy,
   secret policy, size limits, and fast-forward behavior.
6. The Publisher pushes that exact SHA to the already-bound task branch.
7. The Dispatcher creates or updates the one Draft PR and Issue metadata.

The Publisher does not review semantics, edit files, stage changes, create commits, run repository
code, merge, or deploy. It never accepts a remote URL, arbitrary refspec, local path, Git option, or
shell fragment from the runner or issue.

## Core invariants

- One work item per repository and issue; no automatic second work item for the same issue.
- One stable branch, runner directory, Codex session, Slack thread, and Draft PR per work item.
- One active Codex turn globally.
- A turn never starts before its issue snapshot, prompt hash, input HEAD, and turn number are stored.
- A changed issue snapshot never receives a stale turn result automatically.
- A missing or conflicting Codex session ID is blocked, not replaced.
- The runner never receives GitHub write credentials or control-host credentials.
- Only the Publisher can push, and only an exact verified SHA to the recorded task branch.
- No force push, ref deletion, tag write, protected-branch write, merge, deployment, or release.
- Slack is never an input or control channel.
- Unknown external state is never success and is never resolved by blind replay.

## First-phase runner risk acceptance

The initial Linux SSH Runner runs directly on a dedicated host without Docker restrictions. This is
an explicit fixture-stage risk acceptance:

- a malicious or defective task may corrupt the runner, fill its disk, or delete any task data the
  runner account can access;
- the runner must contain no production secrets, personal data, deployment credentials, inbound
  SSH key to the control host, or mounted control-host filesystem;
- loss of uncheckpointed code or local Codex session context is accepted;
- published commits, GitHub issue state, control-host SQLite, and Slack metadata remain outside the
  runner failure domain.

Before using the runner for higher-value repositories, execute Codex inside a per-task Docker
container with explicit resource, filesystem, network, capability, and secret boundaries. Docker
must not mount the host Docker socket or run with `--privileged`.

## Dependency policy

The offline core continues to use only the Python standard library. SSH, Git, Codex, and GitHub are
external adapter boundaries. A Python dependency may be proposed only when it materially improves
correctness or security and its operational cost is reviewed first.
