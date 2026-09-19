# Runner authentication and failed-session recovery

This runbook covers the reviewed `s2` -> `codex-runner` Fixture deployment. It does not authorize a
production repository, change model policy, or turn Slack into a control input. See the
[state machine](state-machine.md) for durable lifecycle and discard semantics.

## Identify the failure before acting

Record the exact repository, Issue number/node ID, WorkItem, Turn, generation, task branch,
base/published HEAD, PR, current releases, and intended recovery. Read only bounded metadata;
never print auth files, tokens, prompts, raw provider output, or complete session logs.

| Observation | Meaning | Supported next step |
|---|---|---|
| `codex_auth_invalid` | The exact generation's local login-status check failed before execution | Diagnose and restore authentication; separately classify the failed generation before resubmission |
| `codex_turn_failed` | Codex returned a failed provider/execution result | Inspect restricted failure metadata; this code alone does not prove an auth problem |
| Provider failure metadata confirms unauthorized/revoked credentials | Stored login readiness did not establish actual provider access | Replace the protected seed from a valid same-account login, then validate using an authorized Fixture |
| Interrupted or ambiguous START/RESUME | Whether execution happened is not established | Use normal identity-bound STATUS reconciliation; do not retry or create a replacement session |
| `session_generation_recovery_required` | Prior generation history cannot safely start a new session | Keep the old history; discard the old WorkItem and create a new reviewed Issue, or leave it blocked for a separately designed recovery |

The pre-claim guard is present only after deploying a release containing it. On older releases,
relabeling an established failed session as ready can claim the Issue and then fail with
`WorkItem without a live generation requires explicit recovery`, leaving `agent:dispatching`.
Do not repeat that relabel operation. The manual path below also works on those releases.

## Restore the login seed without changing existing sessions

OpenAI's [authentication documentation](https://learn.chatgpt.com/docs/auth) describes device-code
login and transferring a local file-based login cache to a trusted remote host over SSH. It also
explains that `login status` reports the selected authentication method. A successful status check
is not a successful model request. September 15 Fixture #50 demonstrated that distinction.

1. Inspect the Control service, active Turn ledger, and exact Runner container state. If work is
   active, let it finish or use the existing explicit inactive-Turn recovery procedure; do not kill
   it or change credentials underneath it. For a maintenance window, record the Dispatcher timer's
   prior state, stop that timer, wait for its service to become inactive, and recheck both hosts.
   Keep backup/health observation available. Do not stop unrelated services.
2. Obtain a valid same-account ChatGPT login on the trusted operator workstation, or use the
   documented device flow in a separate protected login home. Check account/workspace equality
   without emitting token contents or user identifiers. An account/workspace change needs its own
   review; do not substitute an API key, another provider, or another model.
3. Transfer credential bytes through the approved SSH route to the Runner, without logging them
   or placing them in argv, Issues, Slack, Git, or an unprotected temporary file. The deployed seed
   is `/srv/codex-runner/app/auth.json`; verify this against the protected runtime before writing.
4. Verify a regular, non-symlink, single-link seed, an accepted owner (root or the Runner service
   account), and mode `0600`. Preserve its deployed owner/group. Retain the old file under a unique
   protected operator-only name, write a mode-`0600` candidate in the same protected directory,
   fsync it, atomically replace the seed, and fsync the directory. Read back equality and metadata
   without printing credential bytes. Never overwrite an existing recovery backup as a retry.
5. Leave generation auth copies and immutable auth/session/tool sidecars untouched. Each new
   generation receives its own seed copy; replacing the host seed does not repair existing copies.
   Never force token expiry, edit expiry fields, or implement a second OAuth refresh mechanism.
6. Validate actual provider access with one explicitly authorized Fixture and the normal pinned
   Runner path. Require a completed Turn, exact-HEAD Actions, fresh Audit when configured, and
   durable GitHub/Slack receipts. A host-only `login status` result is insufficient. Retain a failed
   canary and diagnose it instead of creating repeated replacement Issues without new evidence.
7. Restore the Dispatcher timer to its recorded prior state, observe an idle sweep, and run the
   existing health service. Do not restore a credential known to be revoked merely to make bytes
   match the old seed. If the replacement is unverified, keep execution paused and report it.

This runbook does not add a periodic paid model probe. Authentication can fail between probes;
each real Turn still requires local readiness and provider-side execution evidence.

## Supported recovery choices

The Runner and SQLite enforce the original identity and immutable receipts. Do not edit SQLite,
revive a `failed` generation, remove Turn/session records, overwrite host-only bindings, or reopen
an already closed terminal Issue to resume it.

The narrow pre-session retry exception remains unchanged: every prior generation must prove a
single `runner_request_rejected` Turn before any Codex session, result, usage, publication, or
checkpoint or handoff, with contiguous generation history. The pre-claim guard also checks
remaining generation and total-Turn budgets; exhaustion returns `session_generation_budget_exhausted`
or `total_turn_budget_exhausted`. The transaction independently rechecks retry eligibility. An authentication failure or established failed session is not that exception.

For an established failed generation with no live generation, there is no supported general
same-WorkItem recovery CLI. A ready label is not approval to construct new handoff/session evidence.
On releases with the guard, preflight and the write sweep return
`session_generation_recovery_required` before claim, Runner preparation, or WorkItem reactivation;
the existing Issue label and failed history remain unchanged. Replace an operator-added ready
label with blocked while investigating, or choose the discard path below. Omitting `session_runtime`
does not bypass this protection: persisted v2 history remains blocked. A single active
`legacy_migration` binding with no policy digest and the exact original WorkItem session identity
retains the supported v1 resume path.

For the authorized Fixture discard-and-replace path:

1. Verify no ambiguous Turn remains, capture the exact current Issue labels and PR HEAD, and
   confirm the maintainer identity. Preserve all non-state labels.
2. Replace the *observed* current state label with `agent:discard` in one label edit. For example,
   only when the verified current label is `agent:blocked`:

   ```bash
   gh issue edit <issue-number> -R longwdl/codex-dispatcher-fixture \
     --remove-label agent:blocked --add-label agent:discard
   ```

   If an older release left `agent:dispatching`, remove that exact observed label instead. If
   multiple stale state labels already coexist, remove those exact observed labels in the same
   edit, leaving only discard. Re-read the Issue and verify the actor/event and single state label.
   Do not remove `agent:completed` or override a merged PR fact.
3. Let recovery-first sweeps persist the discard request, close the exact unmerged PR if present,
   record the disposition, close the Issue as `not_planned`, and archive when eligible. One sweep
   performs one major action, so several successful sweeps can be necessary. Do not manually
   pre-close the Issue/PR, delete storage, or repeatedly toggle labels to accelerate this process.
4. Require completed closure receipts and exact Runner archive/absence evidence. A failed or
   ambiguous receipt remains a blocker; no new Issue is a substitute for reconciling it.
   A stopped process may leave an uncommitted worktree even when HEAD still equals the approved
   checkpoint. Archive can then remain ambiguous because Runner refuses the dirty tree. Preserve
   the exact dirty files, patch, and hashes in a mode-protected operator-only evidence directory
   outside the WorkItem, and verify the saved copies before changing the originals. Only under
   explicit discard authorization, after proving no active execution, unchanged expected HEAD,
   exact task identity, and exclusively task-owned allowed-path changes, may an operator replace
   those tracked dirty paths with the bytes from the unchanged expected HEAD. Untracked or unknown
   paths remain blocked; this does not authorize their deletion or cleanup. Do not reset history,
   delete receipts, or change the database. Let normal ARCHIVE_STATUS/ARCHIVE prove the outcome.
5. After the cause has been corrected, create a new reviewed Fixture Issue with the current exact
   base SHA, allowed paths, acceptance criteria, and its own identity. If old unmerged changes are
   needed, review and specify them explicitly; do not silently adopt the old branch or session.
6. Confirm one new WorkItem/branch/PR/Slack thread, the final gate, and unchanged main. Retain the
   old closed Issue as the failure audit record. Fixture #50 -> #51 is the recorded example.

## Notifications and completion evidence

A normal terminal Turn failure is projected to its Issue and configured Issue-channel Slack
thread (`C0BR2D0MS8Y`). Inspect that exact WorkItem rather than waiting for a system health alert.
The health service independently reports a non-disposed WorkItem blocked for more than 24 hours,
service failures, and other lifecycle conditions in the system channel (`C0BS3LPG43G`). It does not
perform a provider authentication request or automatically refresh/re-login the seed. A healthy
idle system therefore does not certify that its next model request will authenticate.

A pre-claim rejection creates no new Turn or duplicate failure delivery. Its bounded reason is
available in preflight/sweep output; existing failure evidence stays authoritative. A retained
failed canary must be reported separately from the later successful recovery. Record only receipt
IDs/hashes, exact heads, counts, outcomes, and relevant permalinks in `live-test-evidence.md`.
