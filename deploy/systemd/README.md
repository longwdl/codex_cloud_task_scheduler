# Linux Control Host systemd deployment

These units run the existing recovery-first `ssh-run-once` command as a bounded `oneshot`. They do
not create a long-running web service. The timer waits two minutes after the previous sweep becomes
inactive, so systemd does not intentionally overlap sweeps; the Dispatcher process lock and SQLite
constraints remain the authoritative concurrency controls.

The separate daily backup timer invokes a credential-free, network-isolated oneshot. It uses the
SQLite Online Backup API, verifies both source and backup integrity, publishes a mode-`0600` file
without overwriting a same-name backup, then validates all canonical copies and retains the oldest
migration anchor plus the newest seven. It deletes no file if any candidate fails validation.

The weekly credential-free, network-isolated restore drill restores the newest backup into a
temporary database, verifies integrity, foreign keys, and the migration ledger, then removes the
temporary copy without replacing the live database.

The 15-minute health timer receives only the protected Slack environment and outbound network. It
checks integrity and foreign keys, and reports bounded structured alerts for a Turn older than the
configured SSH operation timeout plus five minutes, a WorkItem blocked for more than 24 hours, an
archive pending for more than 15 minutes, an ambiguous/blocked archive, or an overdue disposition
or completed-retention archive. With `--systemd`, the fixed wrapper also requires all four timers
to be loaded, enabled, and active and rejects a failed dispatcher, backup, or restore-drill service
result. It also sends one fixed protocol-v2 read-only capacity request through the pinned Runner SSH
endpoint and reports unavailable, Turn-low, or provision-low capacity. It writes only the
migration-14 health outbox and active-episode row, sends one deduplicated Slack alert per stable
episode plus one threaded recovery, and never contacts GitHub or repairs remote state. The health
command does not run migrations; activate the complete shipped migration ledger through the normal
recovery-first Dispatcher path before enabling Slack health delivery.

The files are deployment artifacts, not an installer. Copying them into `/etc/systemd/system` or
enabling the timer changes a real host and can trigger GitHub, SSH, Publisher, and optional Slack
writes. Perform those steps only after a separately approved deployment command set.

## Required host boundary

- systemd 249 or newer, a root-owned Python 3.12 or newer at
  `/opt/codex-python/current/bin/python3`, and the exact Git, `gh`, and OpenSSH versions pinned in
  `config.toml`; do not replace the distribution's `/usr/bin/python3`;
- a system account and group both named `codex-dispatcher`, with no login shell;
- root-owned, non-group/world-writable releases below `/opt/codex-dispatcher/releases` and an atomic
  `/opt/codex-dispatcher/current` symlink;
- `/etc/codex-dispatcher` owned by root and not group/world writable;
- `config.toml` readable by the service user but not writable by it;
- `dispatcher.env` owned by root with mode `0600`; systemd reads it before changing to the service
  user, and token values never appear in the unit or `ExecStart` argv;
- `health.env` independently owned by root with mode `0600`, containing only the Slack write gate
  and outbound bot token from `health.env.example`;
- the Runner identity owned by `codex-dispatcher` with mode `0600`, because OpenSSH reads it after
  privilege drop;
- `/var/lib/codex-dispatcher` and every configured mutable subdirectory owned by
  `codex-dispatcher`, non-symlink, and mode `0700`.

The example timer's two-minute `OnUnitInactiveSec` matches
`scheduler.poll_interval_seconds = 120`. Changing the interval requires updating both files. The
effective delay is sweep duration plus two minutes, which avoids a timer backlog while a long Codex
Turn is active.

`TimeoutStartSec=infinity` is deliberate: the application already bounds each GitHub, Git, SSH, and
Slack operation, while one sweep may legitimately perform several such operations in sequence. A
shorter independent systemd deadline could terminate a valid `STATUS`/`EXPORT` or
`PREPARE`/`START`/`EXPORT` chain at an ambiguous receipt boundary. `systemctl stop` remains bounded
by `TimeoutStopSec=30s` and `KillMode=control-group` for an explicit operator stop.

## Staged installation checks

Before touching systemd, stage one root-owned release and validate it offline:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src tests
sh -n scripts/codex-dispatcher-v1 scripts/codex-dispatcher-health-v1 \
  scripts/codex-dispatcher-restore-drill-v1
systemd-analyze verify \
  deploy/systemd/codex-dispatcher.service \
  deploy/systemd/codex-dispatcher.timer \
  deploy/systemd/codex-dispatcher-backup.service \
  deploy/systemd/codex-dispatcher-backup.timer \
  deploy/systemd/codex-dispatcher-health.service \
  deploy/systemd/codex-dispatcher-health.timer \
  deploy/systemd/codex-dispatcher-restore-drill.service \
  deploy/systemd/codex-dispatcher-restore-drill.timer
```

Prepare these persistent subdirectories with owner/group `codex-dispatcher` and mode `0700`:

```text
/var/lib/codex-dispatcher/backups
/var/lib/codex-dispatcher/home
/var/lib/codex-dispatcher/mirrors
/var/lib/codex-dispatcher/publisher-temporary
/var/lib/codex-dispatcher/quarantine
/var/lib/codex-dispatcher/repos
/var/lib/codex-dispatcher/source-temporary
```

Do not copy values from an interactive shell history. Create `dispatcher.env` through a protected
root session, using `dispatcher.env.example` only as the field-name template. Never print or commit
the resulting file. The environment must include the SSH write gate and one recognized repository-
scoped GitHub token. Include the Slack gate and bot token only when Slack output is configured.
Create `health.env` separately from `health.env.example`; copy only the Slack gate/token without
printing the token, and do not place the GitHub token or SSH write gate in this file.

Before enabling the timer, run the normal source-tree `ssh-preflight` as the service user with the
same protected config and GitHub credential. It must pass tool pins and SQLite integrity and report
an expected recovery/candidate state or `idle`; its `authorizes_apply` field remains false. Starting
`codex-dispatcher.service` is the first write-enabled action and requires separate approval.

Run `codex-dispatcher-backup.service` once before enabling either timer. Its JSON receipt must name
one file below `/var/lib/codex-dispatcher/backups`, report `integrity=ok`, and the file must be owned
by `codex-dispatcher` with mode `0600`. The backup service receives no environment file, token, or
network namespace. `Persistent=true` lets the daily timer catch up after downtime; the bounded
15-minute random delay avoids synchronized disk work. The service preserves at least the newest
seven good copies plus a distinct oldest anchor. Run and verify the restore-drill service before
enabling its weekly timer; the temporary restored database must be absent after the receipt.

After installation, validate unit expansion and hardening before the first start:

```bash
systemd-analyze verify /etc/systemd/system/codex-dispatcher.service \
  /etc/systemd/system/codex-dispatcher.timer \
  /etc/systemd/system/codex-dispatcher-backup.service \
  /etc/systemd/system/codex-dispatcher-backup.timer \
  /etc/systemd/system/codex-dispatcher-health.service \
  /etc/systemd/system/codex-dispatcher-health.timer \
  /etc/systemd/system/codex-dispatcher-restore-drill.service \
  /etc/systemd/system/codex-dispatcher-restore-drill.timer
systemd-analyze security codex-dispatcher.service
systemd-analyze security codex-dispatcher-backup.service
systemd-analyze security codex-dispatcher-health.service
systemd-analyze security codex-dispatcher-restore-drill.service
systemctl cat codex-dispatcher.service codex-dispatcher.timer \
  codex-dispatcher-backup.service codex-dispatcher-backup.timer \
  codex-dispatcher-health.service codex-dispatcher-health.timer \
  codex-dispatcher-restore-drill.service codex-dispatcher-restore-drill.timer
systemctl list-timers codex-dispatcher.timer codex-dispatcher-backup.timer \
  codex-dispatcher-health.timer codex-dispatcher-restore-drill.timer
```

Do not use `systemctl show-environment`, dump `/proc/<pid>/environ`, or enable shell tracing while
the credential file is loaded.

## Activation, observation, and rollback

For the reviewed `s2` / `codex-runner` layout, `scripts/codex-dispatcher-release-v1` packages the
previous manual activation boundary into one fail-closed transaction. Its command name remains
stable, while its durable receipt format is schema version 2. The operator must first create one
exact `git archive` at
`/var/tmp/codex-dispatcher-release-<40-hex-commit>.tar` and independently record its SHA-256. The
tool accepts only that path shape and exact digest, rejects links and unsafe tar members, creates a
fresh Online Backup, stops all four Control timers, masks service activation while it waits up to the
explicit `--wait-active-seconds` bound for an already-running service to finish naturally, requires
both host locks to be available, and runs the complete tests and compilation from short
service-owned copies under `umask 077`. It never kills an active Dispatcher or maintenance job.

Run the same exact arguments with `--plan` first. Plan validates the immutable inputs, both current
links, candidate absence, configuration compatibility when requested, and current service state; its
JSON always carries `authorizes_apply=false` and `state_writes=0`. Plan is evidence, not permission
to apply. A separate approved invocation replaces `--plan` with `--apply`. Every apply owns one new
commit only: an existing candidate directory or receipt is an error and is never deleted as an
assumed retry artifact.

An optional configuration change must be paired with
`--config /var/tmp/codex-dispatcher-config-<commit>.toml --config-sha256 <digest>`. The candidate must
be root-owned mode `0600`; both the old and candidate release parsers must accept it before any
activation. The old configuration is retained by content digest in the root-only configuration
backup directory, and the candidate is atomically installed only after both releases and units pass
validation.

The identical archive bytes are verified on both hosts. Activation switches the Runner symlink
first and the Control symlink second, both atomically. Any staging, unit-verification, or Runner
capacity failure before handoff reconciles possibly lost switch responses, restores both previous
symlinks and configuration, and removes only candidates proven to have been created by that
invocation and not currently active. Each phase is atomically fsynced to a root-only receipt at
`/opt/codex-dispatcher/release-receipts/<commit>.json`; inspect it with `--status --commit <commit>`.
The tool deliberately does not start the write-enabled Dispatcher or restart timers: after its
`requires_manual_sweep=true` committed receipt, inspect the recorded backup, start exactly one
Dispatcher sweep, require an expected result, then run backup, restore, and health checks before
starting the timers.

Before that first sweep only, `--rollback --commit <commit> --apply` may restore the recorded links,
units, and optional configuration. It rejects rollback if a Control timer/service is active, the
Dispatcher `InvocationID` changed, either current link drifted, or the installed configuration no
longer has the recorded candidate digest. The rollback-bound `InvocationID` is sampled only after
all timers are stopped, active services have finished naturally, and both host locks have been
acquired; sampling it before quiescence creates a false post-activation drift window. After any
sweep begins, external state or schema may have changed and automatic binary-only rollback is
forbidden; use the matching database backup and the documented recovery-first rollback instead.

Treat receipt states as operational state, not progress text:

- `in_progress` means the exact invocation did not reach a durable terminal observation; inspect the
  recorded phase and both exact links, and do not retry the same commit.
- `rolled_back` means the previous links/configuration were observed restored; retain the receipt
  and use a new commit for another attempt.
- `rollback_incomplete` or `recovery_required` requires manual reconciliation from the receipt and
  exact links. Do not delete either candidate or rewrite the receipt.
- `committed` with `handoff_required` means both links and optional configuration are installed but
  no Control sweep or timer restart has been authorized by the tool.
- `rolled_back` with operation `rollback` is the durable result of an explicitly requested,
  pre-sweep automatic rollback.

The state-changing activation sequence is intentionally not automated. Once separately approved,
the operator installs the reviewed units, runs `systemctl daemon-reload`, manually starts exactly
one service sweep, verifies its bounded journal result and SQLite/GitHub/Runner state, and only then
enables the dispatcher timer. It separately starts and verifies one backup before enabling the
backup timer. Start and verify one restore drill before enabling its weekly timer. Enable the four
operational timers before manually starting the health service,
because its systemd assertion intentionally treats a disabled timer as unhealthy.

Observe with `systemctl status`, `systemctl list-timers`, and bounded queries such as
`journalctl -u codex-dispatcher.service -n 100` and
`journalctl -u codex-dispatcher-backup.service -n 20`, plus
`journalctl -u codex-dispatcher-restore-drill.service -n 20` and
`journalctl -u codex-dispatcher-health.service -n 20`. A non-zero sweep or health check remains
visible as a failed service activation; the dispatcher timer will try another recovery-first sweep
after the inactive interval, while the health timer only observes and reports.

Each SSH sweep emits and durably stores bounded `github_api` evidence: total/read/write/failure
command counts shared by the tracker and Actions importer, elapsed milliseconds, sweep outcome, and
a best-effort Core/GraphQL rate-limit snapshot. The lifecycle health check alerts when the latest
metric is missing, failed, older than its bounded grace period, unable to read rate limits, or at
five percent budget; it does not rely on a prior journal line. Terminal WorkItems carrying immutable
Runner archive/absence evidence are omitted from ordinary sweeps and re-audited once per
`scheduler.terminal_full_scan_interval_seconds`; an interrupted audit never advances its cursor,
and health compares the durable cursor age to the configured interval plus the same bounded grace.

Terminal Issues remain open indefinitely. After `ssh_runtime.terminal_branch_retention_seconds`,
only their task branches may be reclaimed. The sweep revalidates the open terminal Issue, exact
merged/closed PR identity, immutable Runner evidence, and exact branch HEAD; it persists a prepared
deletion receipt before the GitHub write and confirms branch absence afterward. A lost write receipt
is reconciled as absent on the next sweep. A changed HEAD or identity blocks cleanup and is surfaced
by lifecycle health. Restoring a deleted branch means pushing only the persisted exact terminal SHA.
An optional quoted RFC 3339 UTC `ssh_runtime.terminal_branch_retention_cutover_at` excludes WorkItems
whose terminal Runner evidence predates the boundary. Use it when first enabling or temporarily
shortening retention so a configuration rollout cannot make historical branches eligible at once.
It requires `terminal_branch_retention_seconds`; removing it deliberately restores the unbounded
historical scan.

The health service also sends a strict protocol-v2 `capacity` read to the forced Runner endpoint.
It reports `runner_capacity_unavailable`, `runner_turn_capacity_low`, or
`runner_provision_capacity_low` through the same deduplicated Slack episode. Capacity canaries must
not consume disk: stop the Dispatcher timer and service, back up the protected Runner config, raise
only `work_item_disk.host_reserve_bytes` above current availability, run one health check, restore
the exact config, then run health twice to prove one threaded recovery and no duplicate. Backup/service
canaries similarly use one controlled invalid input or stopped timer, never delete real backups,
WorkItems, branches, or images; record the alert and recovery permalinks before re-enabling timers.

Emergency stop disables `codex-dispatcher.timer`, `codex-dispatcher-backup.timer`, and
`codex-dispatcher-health.timer`, and `codex-dispatcher-restore-drill.timer`, followed, if necessary,
by stopping their services. Preserve
SQLite, WAL/SHM files, backups, quarantine, mirrors, Runner directories, branches, and PRs. Roll
back code by atomically restoring the previous `/opt/codex-dispatcher/current` release symlink,
re-running unit verification, and starting one manually observed recovery sweep and backup before
re-enabling the timers.
