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
/srv/codex-runner/tools/codex/0.147.0/bin/codex root:root       0755
/srv/codex-runner/tools/codex/0.147.0/bin/codex-code-mode-host root:root 0755
/srv/codex-runner/bin/                    root:root             0755
/srv/codex-runner/bin/codex-runner-v1     root:root             0755
/srv/codex-runner/etc/                    root:codex-runner     0750
/srv/codex-runner/etc/config.json         root:codex-runner     0640
/srv/codex-runner/etc/agent-result.schema.json root:root        0644
/etc/ssh/authorized_keys/                 root:root             0755
/etc/ssh/authorized_keys/codex-runner     root:codex-runner     0640
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
the fixed sibling `codex-code-mode-host`, the output Schema, the work-item root, the active-lock
parent, and an existing lock file are checked before the request frame is accepted. Rootless Docker
configuration carries independent SHA-256 values for both Codex executables; both are rehashed
immediately before each container command. Every configured protected path also requires a complete
root-or-Runner-owned, non-writable parent chain so an administrator account without sudo cannot
replace a trusted executable through a writable package-manager directory. Mutable directories and
lock files must be owned by the executing account with no group or world access.

## Codex policy bundle

For protocol v2 on `rootless_docker`, install the complete reviewed
`config/runner-codex-policy/` tree as root-owned, non-group/world-writable
`/srv/codex-runner/etc/runner-codex-policy/`. Configure its absolute path and
the reviewed `manifest.json` `policy_digest` in the protected Runner
configuration. The Runner rejects a missing, extra, symlinked, writable, or
digest-mismatched policy file before a Turn and rechecks it immediately before
each Codex container command. Do not copy the offline example digest: its all-
zero value is intentionally un-installable.

The policy fields remain optional for a v1-only rootless Runner so an upgraded
Runner package does not change legacy argv or mounts. A v2 request is rejected
before Turn persistence unless the exact bundle is configured and valid.

The container mounts only `config.toml`, `agents/`, and `requirements.toml`
read-only into its Codex paths. It does not mount the complete policy directory
or let `/workspace` supply configuration. The CLI runs with `--strict-config`,
the fixed `gpt-5.6-sol`/`xhigh` primary, and `multi_agent` enabled. Login status
is deliberately outside this policy mount because it is not an agent Turn.

Activate this in two ordered steps: install and validate the Runner bundle and
Runner configuration first, then enable the Control Host `[session_runtime]`
table with the exact same `agent_policy_digest`. Never enable the Control Host
v2 request path against a v1-only Runner.

`rotate_before_final_audit=true` requires every passed Implementation completion
candidate to rotate into a separate fresh Audit generation. The Audit starts from
the exact published HEAD and trusted Handoff/CI evidence, may make bounded fixes,
and must pass the same completion gate before review. Keep enough
`max_session_generations` budget for this mandatory generation; budget exhaustion
blocks instead of silently skipping the Audit.

Before activation, take a Control Host SQLite backup and preserve the previous
Runner config/package. Roll back by stopping new sweeps and restoring both the
database and the two configs to the same pre-activation boundary. Do not merely
remove `[session_runtime]` after a v2 WorkItem has started: the legacy
`work_items.codex_session_id` is intentionally not populated by v2, so a mixed
rollback could start an unrelated v1 session. Generation directories are audit
state and must not be deleted during rollback.

Releases through schema migration 013 add Handoff, Agent-result, verified
publication, delegation, completion-gate, context-failure, archive, disposition, and explicit
absence-reconciliation ledgers. Older
binaries intentionally reject a newer schema. Rolling back such a release
therefore requires the matching pre-migration SQLite Online Backup; changing
only the `current` release symlink is unsafe.

## Terminal WorkItem archive boundary

Control-side automatic reclamation is disabled unless
`ssh_runtime.completed_retention_seconds` is present. The reviewed normal value is `604800` (seven
days). Before sending `ARCHIVE`, Control revalidates the completed Issue and exact merged bound PR,
and persists the immutable request in schema 12.

An operator may instead apply `agent:discard`. Control accepts it only from the repository
maintainer allowlist and persists the exact GitHub timeline event. A WorkItem with no PR becomes
`abandoned`; one with an exact non-merged PR becomes `superseded`. The dispatcher does not close the
PR or delete its branch. Before each disposed archive/STATUS call, Control re-reads the branch PR;
a newly appeared or merged PR blocks before Runner contact. The disposition is permanent and makes
later Turn/generation creation fail. GitHub and Runner are not one transaction, so operators must
not merge a PR after its discard disposition is recorded.

The Runner exposes `ARCHIVE` and `ARCHIVE_STATUS` only in protocol v2 and requires the exact
published HEAD. Under the global lock it validates the permanent registry identity, clean task
branch, finished Turn records, and inactive v2 containers. It writes
`work-items/.archives/<work-item-id>.json` before staging and deleting the exact workspace/image.
Do not manually remove `.registry`, `.archives`, or a partial `.archive-staging`/image `.archive`
entry: they are recovery evidence. Per-item deletion never includes the policy bundle, tools,
shared auth seed, Control database, or another WorkItem. A lost receipt must be reconciled with
`ARCHIVE_STATUS`; a generic rejected response also remains ambiguous because it carries no
pre-effect/post-effect phase proof. Do not retry deletion with shell commands.

Use the read-only capacity view before admitting or reconstructing an image:

```bash
codex-dispatcher runner-capacity --config /srv/codex-runner/etc/config.json --json
```

It reports capacity, available bytes, fixed reserve, image size, Turn admission, image-provision
admission, and the exact shortfall without reading credentials or WorkItem contents.

Install `scripts/codex-runner-capacity-v1` as the root-owned mode-`0755`
`/srv/codex-runner/bin/codex-runner-capacity-v1`, and install the matching
`codex-runner-capacity.service` and `.timer` as root-owned system units. The credential-free,
network-isolated timer runs every 15 minutes as `codex-runner` and exits nonzero unless one new
bounded WorkItem image can still be provisioned. Its JSON journal record is read-only capacity
evidence; it never deletes an image or inspects a WorkItem repository. Validate with:

```bash
sh -n scripts/codex-runner-capacity-v1
systemd-analyze verify deploy/runner/codex-runner-capacity.service \
  deploy/runner/codex-runner-capacity.timer
systemctl status codex-runner-capacity.timer codex-runner-capacity.service
journalctl -u codex-runner-capacity.service -n 20
```

Legacy directories are eligible only when the exact registry/workspace exists and the disk
classifier proves final, provisioning-staging, archive-staging, and mount state contain no image.
The v2 tombstone binds `bounded_image` or `legacy_directory`; retries may not switch kind.

If old state was already manually removed, ordinary `ARCHIVE` must continue to fail closed. Schema
13 keeps a separate absence-reconciliation ledger. After stopping the dispatcher timer, an operator
may target one exact completed WorkItem with:

```bash
codex-dispatcher ssh-reconcile-absence \
  --config /etc/codex-dispatcher/config.toml \
  --repository OWNER/REPOSITORY --issue-number NUMBER --apply --json
```

Control revalidates the open completed Issue and exact merged bound PR under its global lock, then
persists the normal ARCHIVE request. Protocol-v2 `PROVE_ABSENCE` binds that request SHA plus the
repository, Issue, branch, WorkItem, and expected HEAD. Under the Runner global lock it rejects any
registry, workspace, archive tombstone, workspace/image staging, image, or mount evidence. Only an
all-absent result creates `work-items/.absences/<work-item-id>.json` mode `0600`; an interrupted retry
returns that exact receipt. Control stores its canonical SHA and Runner timestamp. A local JSON
assertion is never accepted, and no Runner `ARCHIVED` receipt is fabricated.

## SSH contract

Install `codex-runner-sshd.conf` only after creating the account and fixed wrapper. The key file must
contain exactly one non-comment entry with this option prefix followed by the dedicated Control Host
public key:

```text
restrict,command="/srv/codex-runner/bin/codex-runner-v1" ssh-ed25519 <public-key>
```

The private key remains mode `0600` on the Control Host and is never copied to the Runner. The
root-owned external `AuthorizedKeysFile` prevents the protocol account from authorizing another key.
Its public-key content is group-readable by `codex-runner` so privilege-separated sshd can read it,
but it is writable only by root.
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

## Container boundary status

The dedicated account and root-owned inputs are now combined with rootless per-WorkItem container
storage, isolated session homes, fixed resource controls, and proxy-only egress. A dedicated private
Fixture has passed one successful container RESUME Turn and recovery read-back. This removes the
original direct-execution boundary for configured Fixture work, but it does not authorize
higher-value repositories: the remaining attack and recovery gates, operational exceptions, and
rollback rules are documented in [DOCKER.md](DOCKER.md).

The deployed Runner keeps the Runner-wide `auth.json` only as an unmounted host seed. Each WorkItem
receives a protected writable copy inside its own session home plus a host-only binding sidecar, so
Codex can atomically refresh without sharing writable authentication state across WorkItems. Issue
`#26` proved the binding and exact START/RESUME reuse before Fixture-only timer activation. A natural
version-specific token refresh remains an admission gate for higher-value repositories.
