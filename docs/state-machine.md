# Dispatcher state machine

This document is the authoritative operator-facing state model. The Python enums and SQLite
constraints remain the executable definition; GitHub labels are a projection, not the durable
source of truth.

## Layers

One WorkItem lifecycle is represented by several coordinated layers:

| Layer | Purpose | Terminal facts |
|---|---|---|
| GitHub Issue | reviewed intent and audit index | `agent:completed` or `agent:discard`, then closed with an exact reason |
| `work_items.state` | execution progress | `completed`, or a non-completed state plus a discard disposition |
| Turn | one bounded Codex invocation | `finished`, `needs_input`, `failed`, `blocked`, or `interrupted` |
| SessionGeneration | replaceable Codex session boundary | `retired` or `failed` |
| terminal ledgers | exact external and storage effects | discard request, PR/Issue close receipts, disposition, archive/absence, branch cleanup |

`work_items.state` deliberately has no `discarded` enum. A discarded WorkItem is represented by an
immutable `work_item_discard_requests` row followed by a `work_item_dispositions` terminal overlay.
The stored `abandoned`/`superseded` value only preserves whether an exact PR was bound; it is not a
second operator choice and must not become another GitHub label.

## Normal execution

```text
discovered -> preparing -> ready -> running -> review -> completed
                   |          |         |          |
                   |          |         +-------> waiting_input
                   |          +-----------------> blocked / paused
                   +----------------------------> blocked / paused
```

The detailed transition sets are defined by `WorkItemState`, `TurnState`, and
`SessionGenerationState` in `src/codex_dispatcher/work_items.py`. Recovery may move a blocked,
paused, or waiting-input WorkItem back to an allowed preparation/ready state only through an exact
reviewed recovery path.

For authentication failures and established failed sessions without a live generation, follow
[Runner authentication and failed-session recovery](runner-auth-and-session-recovery.md).
A ready label alone does not authorize a replacement session or new handoff evidence.

## Completion

`agent:completed` is produced by the Control plane; it is not an operator authorization label.

```text
review WorkItem
  -> find the one persisted PR for the deterministic task branch
  -> require PR state MERGED
  -> require PR headRefOid == WorkItem.last_published_sha
  -> record WorkItem completed and retire live generations
  -> project the fixed Issue comment and agent:completed label
  -> prepare exact completed_issue closure intent
  -> close the Issue with state_reason=completed
  -> read back node ID, label, closed state, and close reason
  -> complete the permanent closure receipt
```

Squash merge, rebase merge, and merge commit are all valid. The identity check binds the PR's
source HEAD (`headRefOid`) to `last_published_sha`; it does not require the base branch merge commit
SHA to equal the task branch HEAD.

If the external close succeeds but the receipt write is lost, the next sweep reads the already
closed Issue, verifies the exact node ID/reason, and records `already_closed`. A different close
reason, reopened Issue, changed identity, or mismatched PR HEAD blocks recovery.

## Discard

`agent:discard` is a trusted maintainer command. The Dispatcher audits the latest stable label event
and never asks Codex to reinterpret the Issue text or decide whether the task should be abandoned.

```text
maintainer applies agent:discard
  -> audit actor, label-event ID/time, Issue node ID, WorkItem HEAD, and exact PR identity
  -> persist immutable discard request; reject any new Turn or SessionGeneration
  -> if a Turn has not started, cancel it atomically
  -> if a Turn is already active, reconcile it through STATUS until terminal
     -> reject any unpublished/checkpoint result; do not publish after discard authorization
  -> if an unmerged PR exists:
       prepare exact discarded_pull_request closure intent
       close only its persisted number/branch/HEAD
       read back CLOSED and complete the permanent receipt
  -> record one uniform discarded disposition
  -> prepare exact discarded_issue closure intent
  -> close the Issue with state_reason=not_planned
  -> read back node ID, label, closed state, and close reason
  -> complete the permanent receipt
  -> archive only through the separate exact Runner lifecycle
```

If the PR becomes merged before its close is confirmed, merge is the stronger external fact and the
WorkItem returns to completion reconciliation. The immutable discard request remains audit evidence
but does not replace the completed outcome. The Dispatcher never closes a merged PR. If no PR
exists, the PR-close stage is skipped; a PR appearing later is an identity conflict.

The only maintainer action is applying the exact single `agent:discard` state label. Maintainers
must not pre-close the PR or Issue as part of the normal path; doing so removes the Control plane's
prepare/write/read-back receipt boundary. Manual intervention is reserved for an explicitly
diagnosed blocked recovery.

## Closed Issues remain audit entries

Closing an Issue removes it from the active queue but does not remove its audit value. The Issue
continues to contain the immutable node identity, labels, fixed status comment, PR link, Slack link,
and GitHub timeline. SQLite, backups, Runner tombstones, and permanent receipts retain the durable
evidence.

Reopening a terminal Issue does not resume its WorkItem. A completed closure receipt plus an open
Issue is an external contradiction and blocks until an operator performs a separately reviewed
repair. New work requires a new reviewed Issue rather than reusing a terminal WorkItem.

## Recovery and retention ordering

One sweep performs at most one major action. The terminal ordering is:

```text
merge completion: completed ledger -> Issue close -> Runner archive/absence -> branch cleanup
discard: request -> optional PR close -> disposition -> Issue close -> Runner archive/absence
         -> branch cleanup
```

Every GitHub close is prepared in `terminal_github_closures` before the write and completed only
after exact read-back. A missing or unfinished terminal Issue closure receipt remains immediate
recovery work even when Runner archive/absence evidence already exists; only completed receipts
move into the periodic terminal re-audit cadence. A `blocked` closure receipt is permanent and
requires reviewed operator repair; it is never silently retried as a different target. Broad
deletion or prune commands are outside this state machine.
