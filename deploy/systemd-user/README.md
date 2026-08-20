# Rootless Linux Control Host systemd deployment

This directory is a constrained `systemd --user` variant for the private Fixture Control Host when
the operator has no `sudo`. It runs the same recovery-first `ssh-run-once` and protected SQLite
Online Backup commands as `deploy/systemd`, but it cannot provide the production boundary of a
dedicated Unix account, root-owned releases, root-owned configuration, or root-read environment
files. Do not use this variant for production repositories or production credentials.

The units also deliberately omit `RemoveIPC=yes`: `ecs-user` is a shared login identity, so removing
all IPC objects owned by that UID when one service exits could disrupt unrelated user processes.

The user manager must be enabled at boot and remain alive after logout. An administrator must run
this one privileged command before either timer is enabled:

```bash
sudo loginctl enable-linger ecs-user
```

Verify it without `sudo`:

```bash
loginctl show-user ecs-user -p Linger
```

`Linger=yes` is a hard prerequisite. An active user manager during an SSH login is not sufficient.

## Fixed layout and runtime

The reviewed Fixture layout for `ecs-user` is:

```text
/home/ecs-user/.config/codex-dispatcher/config.toml
/home/ecs-user/.config/codex-dispatcher/dispatcher.env
/home/ecs-user/.config/codex-dispatcher/runner_ed25519
/home/ecs-user/.config/codex-dispatcher/runner_known_hosts
/home/ecs-user/.config/systemd/user/*.service
/home/ecs-user/.config/systemd/user/*.timer
/home/ecs-user/.local/opt/codex-dispatcher/releases/<commit>/
/home/ecs-user/.local/opt/codex-dispatcher/current -> releases/<commit>
/home/ecs-user/.local/state/codex-dispatcher/
/run/user/1000/codex-dispatcher/
```

The wrappers deliberately invoke `/home/linuxbrew/.linuxbrew/bin/python3` and set a minimal PATH of
`/home/linuxbrew/.linuxbrew/bin:/usr/bin:/bin`. The executable must be Python 3.12 or newer and must
not be group/world writable. The protected TOML must pin the exact versions returned by its absolute
Git, `gh`, and OpenSSH paths.

All configuration and mutable directories must be owned by `ecs-user`, non-symlink, and mode `0700`.
`config.toml`, `dispatcher.env`, the dedicated Runner identity, and dedicated `known_hosts` must be
regular files with one link and mode `0600`. Do not point the service at the interactive
`~/.ssh/known_hosts`, SSH agent, or an SSH alias. The strict transport uses its configured host,
user, port, identity, and host-key file with `BatchMode=yes`, `IdentitiesOnly=yes`,
`StrictHostKeyChecking=yes`, no proxy, and no forwarding.

Prepare these mode-`0700` state children before preflight:

```text
backups
home
mirrors
publisher-temporary
quarantine
repos
source-temporary
```

Set the TOML paths below `/home/ecs-user/.local/state/codex-dispatcher`, except the lock path, which
must be `/run/user/1000/codex-dispatcher/dispatcher.lock`. The Runner root remains
`/srv/codex-runner/work-items` on the separate Runner host.

## Offline and staged checks

Run locally before copying a release:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src tests
sh -n scripts/codex-dispatcher-user-v1 scripts/codex-dispatcher-user-backup-v1
systemd-analyze --user verify \
  deploy/systemd-user/codex-dispatcher.service \
  deploy/systemd-user/codex-dispatcher.timer \
  deploy/systemd-user/codex-dispatcher-backup.service \
  deploy/systemd-user/codex-dispatcher-backup.timer
```

Stage one immutable-by-convention release directory named by the reviewed commit, then atomically
replace only the `current` symlink. Because both are user-owned, systemd hardening makes them
read-only only inside the service mount namespace; it does not prevent `ecs-user` from altering them
outside the service. Record the release commit and checksums operationally.

Create `dispatcher.env` without shell tracing or terminal output. It must contain the SSH write gate
and exactly one recognized repository-scoped GitHub token. Add the Slack gate and bot token only
when Slack is configured. Never run `systemctl --user show-environment`, dump a service process
environment, or place token values in unit files or argv.

Before the first write-enabled service start:

1. require `Linger=yes` and sufficient free disk;
2. verify the four staged units with the target host's systemd;
3. run the complete tests with the staged Python;
4. create and independently integrity-check one SQLite online backup;
5. run `ssh-preflight` with the same protected config and environment;
6. require an expected `idle`, `ready_candidate`, or `ready_recovery` plan and remember that
   `authorizes_apply=false` never authorizes the later write;
7. obtain separate approval for one manually observed `systemctl --user start
   codex-dispatcher.service`.

Only after that sweep and independent SQLite/GitHub/Runner/Actions checks pass may the Dispatcher
timer be enabled. Start and verify the backup service separately before enabling its timer. The
backup service receives no environment file, has a private network namespace, and never deletes an
older backup automatically.

## Observation and rollback

Use bounded journal reads:

```bash
systemctl --user status codex-dispatcher.service codex-dispatcher-backup.service
systemctl --user list-timers codex-dispatcher.timer codex-dispatcher-backup.timer
journalctl --user -u codex-dispatcher.service -n 100
journalctl --user -u codex-dispatcher-backup.service -n 20
```

Emergency stop disables both timers and then stops either active service. Preserve SQLite,
WAL/SHM files, backups, mirrors, quarantine, Runner directories, branches, and PRs. Roll back code by
atomically restoring the previous `current` symlink, re-verifying units, then manually observing one
recovery-first sweep and one backup before re-enabling timers.
