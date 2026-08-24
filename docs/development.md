# Development

## Supported implementation boundary

The repository has one runtime path: GitHub -> Control Host -> SSH protocol v2 -> isolated Runner
-> Publisher -> Draft PR/Slack. Do not add another executor or create new legacy-format state.

Python 3.12+ and the standard library are the dependency baseline. Adding a dependency, service,
network call, telemetry path, migration, or external write requires an explicit design and approval.

## Code map

- `config.py`: strict secret-free Control configuration.
- `repository_admission.py`, `repository_evidence.py`: class/profile matrix and exact policy/readback
  evidence.
- `scheduler.py`, `ssh_preflight.py`, `ssh_recovery.py`: candidate selection and recovery-first
  planning.
- `control_sweep.py`, `ssh_dispatch_service.py`: one bounded sweep and WorkItem orchestration.
- `state_store.py`, `migrations/`: additive durable ledger.
- `work_items.py`, `followup_intents.py`, `handoffs.py`: WorkItem/Turn/generation/follow-up identity.
- `source_bundle.py`, `trusted_mirror.py`: exact source input.
- `runner_protocol.py`, `runner_transport.py`, `runner_wire.py`: strict protocol v2.
- `runner_workspace.py`, `runner_disk.py`, `runner_docker.py`, `runner_turns.py`: Runner storage,
  isolation, Codex invocation, and persistent result handling.
- `delegation_evidence.py`: metadata-only Sol/direct-child routing evidence.
- `git_bundle_verifier.py`, `publisher.py`, `git_publisher.py`: quarantine and exact task-branch
  publication.
- `github_actions_evidence.py`, `acceptance_evaluator.py`, `completion_gate.py`: trusted completion
  evidence.
- `github_delivery.py`, `slack_delivery.py`, `health_alert_delivery.py`: idempotent projections;
  WorkItem Slack delivery uses `issue_channel_id`, health alert delivery uses `system_channel_id`.
- `work_item_lifecycle.py`, `terminal_retention.py`, `terminal_storage.py`: disposition, archive,
  absence, and branch cleanup.
- `control_host_backup.py`, `disaster_recovery.py`, `release_handoff.py`: backup, independent
  two-host recovery bundle/drill, and immutable operational release evidence.
- `control_host_reclamation.py`: exact read-only Control release/recovery-artifact planning; it has
  no deletion entry point.
- `runner_asset_reclamation.py`, `runner_release_references.py`, `reclamation_canary.py`: exact
  asset planning, transactional references, and isolated threshold/system-channel proof.
- `lifecycle_health.py`, `runner_reclamation_status.py`, `github_api_metrics.py`: unattended health
  and disk/reclamation evidence.

## State and migration rules

- Migrations are additive and ordered. Never edit a migration that may already appear in a backup or
  release receipt.
- Persist identity and intent before an external write; persist provider receipt only after exact
  read-back.
- Transactions must keep WorkItem, Turn, generation, follow-up, completion, disposition, and
  archive facts mutually consistent.
- New work always uses current repository policy evidence, protocol v2, current AgentResult domain,
  bounded WorkItem images, and current Handoff semantics.
- Historical AgentResult/protocol/generation/archive/Handoff parsers exist only for retained recovery
  evidence. Do not route new work through them.
- The migration-1 `runs` and `run_events` tables are inert. They remain solely to preserve migration
  identity; application code must not read or write them.

## Fail-closed rules

- Treat Issue text, comments, repository content, Agent output, Runner replies, GitHub/Slack
  responses, and command output as untrusted.
- Reject unknown fields, versions, enum values, paths, identities, and state transitions.
- Never retry an ambiguous START or external write without identity-bound read-back.
- Never let Agent output assert CI success, acceptance, authorization, or external delivery.
- Never log prompts, raw provider output, credentials, authorization headers, full diffs, or auth
  state.
- Never weaken a test or convert an unexpected condition to idle solely to keep the timer running.

## Change workflow

Before editing:

1. read the applicable architecture and deployment section;
2. inspect `git status` and preserve unrelated user changes;
3. identify the durable identity, failure boundary, rollback, and expected receipt;
4. select the smallest file/test scope;
5. determine whether a live fixture is actually required.

During editing:

- prefer a small explicit state transition over a generic abstraction;
- keep parsing separate from state mutation and external writes;
- inject adapters into tests; offline tests must not require network or credentials;
- test idempotent retry, conflicting identity, malformed input, partial receipt, and rollback boundary;
- use deterministic timestamps, hashes, IDs, fake commands, and local Git repositories.

After editing:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src tests
git diff --check
```

For deployment-related changes also validate wrapper syntax, systemd units, protected path checks,
both Linux service accounts, release plan/receipt, one observed sweep, health, backup/restore state,
and timer activation.

## Live fixture policy

Use a live Fixture only for behavior that offline fakes cannot prove, such as provider receipt
identity, real Actions matching, SSH interruption, container/host restart, login-status behavior,
release rollback, or filesystem reclamation. Before any live write, list exact repository, Issue,
branch, PR, SHA, WorkItem, host, expected state change, rollback, and post-check.

Do not repeat an already proven scenario merely to exercise the current release. Preserve bounded
metadata-only evidence in `docs/live-test-evidence.md`; do not paste secrets, raw prompts, full
provider output, or full diffs.

## Compatibility removal checklist

Compatibility code may be removed only after proving all of the following:

1. no current entry point imports it;
2. the protected runtime configuration cannot select it;
3. the online database has no active row requiring it;
4. retained backups/receipts either do not contain the format or still have a read-only parser;
5. disaster recovery and release rollback do not call it;
6. tests for current behavior remain and obsolete tests are deleted rather than rewritten as fake
   current requirements.

Never remove an applied migration or rewrite immutable evidence as a compatibility cleanup.
