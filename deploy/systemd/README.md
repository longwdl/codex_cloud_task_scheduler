# Linux Control Host systemd deployment

These units run the existing recovery-first `ssh-run-once` command as a bounded `oneshot`. They do
not create a long-running web service. The timer waits two minutes after the previous sweep becomes
inactive, so systemd does not intentionally overlap sweeps; the Dispatcher process lock and SQLite
constraints remain the authoritative concurrency controls.

The files are deployment artifacts, not an installer. Copying them into `/etc/systemd/system` or
enabling the timer changes a real host and can trigger GitHub, SSH, Publisher, and optional Slack
writes. Perform those steps only after a separately approved deployment command set.

## Required host boundary

- systemd 249 or newer, Python 3.12 or newer, and the exact Git, `gh`, and OpenSSH versions pinned
  in `config.toml`;
- a system account and group both named `codex-dispatcher`, with no login shell;
- root-owned, non-group/world-writable releases below `/opt/codex-dispatcher/releases` and an atomic
  `/opt/codex-dispatcher/current` symlink;
- `/etc/codex-dispatcher` owned by root and not group/world writable;
- `config.toml` readable by the service user but not writable by it;
- `dispatcher.env` owned by root with mode `0600`; systemd reads it before changing to the service
  user, and token values never appear in the unit or `ExecStart` argv;
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
sh -n scripts/codex-dispatcher-v1
systemd-analyze verify \
  deploy/systemd/codex-dispatcher.service \
  deploy/systemd/codex-dispatcher.timer
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

Before enabling the timer, run the normal source-tree `ssh-preflight` as the service user with the
same protected config and GitHub credential. It must pass tool pins and SQLite integrity and report
an expected recovery/candidate state or `idle`; its `authorizes_apply` field remains false. Starting
`codex-dispatcher.service` is the first write-enabled action and requires separate approval.

After installation, validate unit expansion and hardening before the first start:

```bash
systemd-analyze verify /etc/systemd/system/codex-dispatcher.service \
  /etc/systemd/system/codex-dispatcher.timer
systemd-analyze security codex-dispatcher.service
systemctl cat codex-dispatcher.service codex-dispatcher.timer
systemctl list-timers codex-dispatcher.timer
```

Do not use `systemctl show-environment`, dump `/proc/<pid>/environ`, or enable shell tracing while
the credential file is loaded.

## Activation, observation, and rollback

The state-changing activation sequence is intentionally not automated. Once separately approved,
the operator installs the reviewed units, runs `systemctl daemon-reload`, manually starts exactly
one service sweep, verifies its bounded journal result and SQLite/GitHub/Runner state, and only then
enables the timer.

Observe with `systemctl status`, `systemctl list-timers`, and bounded queries such as
`journalctl -u codex-dispatcher.service -n 100`. A non-zero sweep remains visible as a failed
service activation; the timer will try another recovery-first sweep after the inactive interval.

Emergency stop is `systemctl disable --now codex-dispatcher.timer` followed, if necessary, by
`systemctl stop codex-dispatcher.service`. Preserve SQLite, WAL/SHM files, quarantine, mirrors,
Runner directories, branches, and PRs. Roll back code by atomically restoring the previous
`/opt/codex-dispatcher/current` release symlink, re-running unit verification, and starting one
manually observed recovery sweep before re-enabling the timer.
