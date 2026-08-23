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
result. It writes only the schema-14 health outbox and active-episode row, sends one deduplicated
Slack alert per stable episode plus one threaded recovery, and never contacts GitHub or the Runner.
The health command does not run migrations; activate schema 14 through the normal recovery-first
Dispatcher path before enabling Slack health delivery.

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

Emergency stop disables `codex-dispatcher.timer`, `codex-dispatcher-backup.timer`, and
`codex-dispatcher-health.timer`, and `codex-dispatcher-restore-drill.timer`, followed, if necessary,
by stopping their services. Preserve
SQLite, WAL/SHM files, backups, quarantine, mirrors, Runner directories, branches, and PRs. Roll
back code by atomically restoring the previous `/opt/codex-dispatcher/current` release symlink,
re-running unit verification, and starting one manually observed recovery sweep and backup before
re-enabling the timers.
