# Linux Runner production ownership and SSH boundary

This deployment profile separates the interactive host administrator from the account that serves
the fixed Runner protocol. It is the required host-ownership boundary before container execution is
introduced; it does not by itself isolate Codex from other WorkItems or the Runner host.

The administrator remains `ecs-user`. The protocol account is `codex-runner`, has a locked password,
no sudo or supplementary groups, and accepts only one externally managed public key through an
sshd-enforced fixed command. Do not edit or replace `~ecs-user/.ssh/authorized_keys` during this
migration. Interactive administrative access and protocol access must use different Unix accounts.

## Required ownership

Install immutable inputs as root-owned and non-group/world-writable:

```text
/srv/codex-runner                         root:root             0755
/srv/codex-runner/releases/               root:root             0755
/srv/codex-runner/current                 root-owned symlink
/srv/codex-runner/tools/                  root:root             0755
/srv/codex-runner/tools/codex/0.147.0/    root:root             0755
/srv/codex-runner/bin/                    root:root             0755
/srv/codex-runner/bin/codex-runner-v1     root:root             0755
/srv/codex-runner/etc/                    root:codex-runner     0750
/srv/codex-runner/etc/config.json         root:codex-runner     0640
/srv/codex-runner/etc/agent-result.schema.json root:root        0644
/etc/ssh/authorized_keys/                 root:root             0755
/etc/ssh/authorized_keys/codex-runner     root:root             0600
/etc/ssh/sshd_config.d/60-codex-runner.conf root:root           0644
```

Only these persistent roots are writable by the protocol account:

```text
/var/lib/codex-runner/home/               codex-runner:codex-runner 0700
/srv/codex-runner/app/                    codex-runner:codex-runner 0700
/srv/codex-runner/run/                    codex-runner:codex-runner 0700
/srv/codex-runner/work-items/             codex-runner:codex-runner 0700
```

`app/` remains the protected runner-wide `CODEX_HOME` during this ownership-only stage. It contains
the existing ChatGPT login and session state and must be moved only by changing ownership on the
same filesystem. Never copy, print, archive, or inspect credential contents during migration.

The protected Runner configuration may be owned only by root or the executing account. Git, Codex,
the output Schema, the work-item root, the active-lock parent, and an existing lock file are checked
before the request frame is accepted. Every configured protected path also requires a complete
root-or-Runner-owned, non-writable parent chain so an administrator account without sudo cannot
replace a trusted executable through a writable package-manager directory. Mutable directories and
lock files must be owned by the executing account with no group or world access.

## SSH contract

Install `codex-runner-sshd.conf` only after creating the account and fixed wrapper. The key file must
contain exactly one non-comment entry with this option prefix followed by the dedicated Control Host
public key:

```text
restrict,command="/srv/codex-runner/bin/codex-runner-v1" ssh-ed25519 <public-key>
```

The private key remains mode `0600` on the Control Host and is never copied to the Runner. The
root-owned external `AuthorizedKeysFile` prevents the protocol account from authorizing another key.
The key-level `restrict` and fixed command duplicate the sshd `Match User` restrictions so either
layer independently denies an interactive shell, PTY, forwarding, tunnel, user environment, and
user rc files. OpenSSH 9.6 does not allow `PermitUserEnvironment` inside a `Match` block; instead,
the exact root-owned key line contains no `environment=` option and the fixed wrapper discards the
inherited environment with `/usr/bin/env -i`.

The drop-in ends with `Match all`; omitting that reset can accidentally scope later sshd directives
to the Runner account. Keep an already authenticated administrator session open throughout sshd
validation and reload.

## Guarded migration sequence

This is deliberately not an installer. Account creation, ownership changes, sshd reload, and Control
Host configuration changes are real infrastructure mutations and require a separately reviewed
command set for the selected host.

Before changing the Runner:

1. stop and disable the Control Host Dispatcher timer, wait for the service to become inactive, and
   keep the backup timer active;
2. require a fresh mode-`0600` SQLite Online Backup, `integrity_check=ok`, no active local Turn, and
   read-only `ssh-preflight=idle`;
3. require no exact Runner or Codex process and verify that the Runner active lock is acquirable;
4. record only non-secret ownership, release symlink, host-key fingerprint, Control Host public-key
   fingerprint, WorkItem count, session-file count, and SQLite quick-check results;
5. keep a second `ecs-user` administrator session open for rollback.

Create a locked system account with `/bin/sh` only because sshd executes `ForceCommand` through the
account shell. Do not grant sudo or add it to `docker`, `adm`, or other supplementary groups. A
`nologin` shell would reject the forced command before the wrapper starts.

Stage a new root-owned release first. Atomically replace only the `current` symlink after the target
host passes the complete offline test suite, `compileall`, JSON parsing, `sh -n`, and protected-path
checks. Copy the complete pinned Codex distribution into its versioned root-owned `tools/` path;
do not point the protected configuration at an administrator-owned Homebrew/Linuxbrew tree. Install
the wrapper, configuration, Schema, external authorized key, and sshd drop-in with their exact
owners and modes. Change ownership only for `app`, `run`, and `work-items`; do not move or
rewrite their contents.

Before reloading sshd:

```bash
sudo /usr/sbin/sshd -t
sudo /usr/sbin/sshd -T -C user=codex-runner,host=localhost,addr=127.0.0.1
```

The effective configuration must name the external authorized-key file and exact force command and
must disable password, keyboard-interactive, PTY, forwarding, tunnels, and user rc files; separately
verify that the wrapper discards the inherited environment. Reload rather than restart sshd. From
the Control Host, authenticate with the dedicated key
and prove one framed read-only `STATUS` response before changing the configured SSH username.

After the Control Host username is atomically changed to `codex-runner`, run another read-only
preflight, one manually observed recovery-first sweep, SQLite/GitHub/Runner/Slack read-back, and an
immediate repeated idle sweep. Re-enable the Dispatcher timer only after all identities remain
unchanged.

## Rollback

Keep the Dispatcher timer disabled. Restore the previous Control Host configuration and Runner
`current` symlink, restore `app`, `run`, and `work-items` ownership to the previous Runner account,
remove only the new sshd drop-in and external authorized-key file, validate `sshd -t`, and reload
sshd. Do not delete the new account, any WorkItem, session, turn record, lock file, branch, or backup
until the old forced-command path has passed a read-only `STATUS` check.

## Container boundary still required

The dedicated account and root-owned inputs prevent Codex from rewriting the Runner service or SSH
authorization after the later container boundary is active. They do not yet stop a directly
executed Codex process from reading or damaging another WorkItem or the shared `CODEX_HOME`.
Higher-value repositories remain prohibited until per-WorkItem container storage, session/auth
handling, resource controls, and internal-network/metadata denial are implemented and live-tested.
The staged fixed-argv contract and its remaining gates are documented in [DOCKER.md](DOCKER.md).
