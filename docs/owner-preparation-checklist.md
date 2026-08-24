# Owner preparation checklist

Use this checklist before onboarding a repository, rebuilding a host, or authorizing a live write.
Never place credential values in chat, Issue bodies, Slack, repository files, TOML, receipts, or
logs.

## 1. Repository decision

- Confirm exact `owner/repository`, default branch, repository class, maintainers, allowed/denied
  paths, required Actions workflows, and structured acceptance criteria.
- Use only `exec:ssh-cli`; remove any second `exec:*` label.
- Install the canonical `agent:*`, `priority:*`, and `exec:ssh-cli` labels.
- Grant the Dispatcher/Pubisher credential only the repository scopes actually required.
- Confirm whether server-enforced branch/ruleset protection exists. If not, record repository- and
  date-specific owner risk acceptance; do not describe CODEOWNERS or Actions as equivalent.
- Do not add a higher-value repository to ordinary admission. Use the isolated manual canary until a
  reviewed code release changes the matrix.

## 2. Control Host

Prepare a stable Linux host with Python 3.12+, pinned Git/gh/OpenSSH, SQLite support, systemd, and
outbound GitHub/Slack access. It must not contain production databases, deployment credentials,
Kubernetes/cloud admin credentials, or secrets intended for the Runner.

Create and protect:

```text
/opt/codex-dispatcher/releases/                 root:root 0755
/opt/codex-dispatcher/release-receipts/         root:root 0700
/etc/codex-dispatcher/config.toml               root:root 0600
/etc/codex-dispatcher/dispatcher.env            root:root 0600
/etc/codex-dispatcher/health.env                root:root 0600
/etc/codex-dispatcher/runner_known_hosts        root:root, not writable by service
/etc/codex-dispatcher/runner_ed25519             root:root 0600
/var/lib/codex-dispatcher/                       service owner, not group/world writable
/var/lib/codex-dispatcher/backups/               service owner 0700
/run/codex-dispatcher/dispatcher.lock            service runtime path
```

The protected TOML contains only fixed paths, host/user/port, timeouts, policy, limits, repositories,
and version pins. GitHub/Slack tokens belong only in the root-owned environment files. Keep source,
mirror, quarantine, Publisher, backup, and higher-value-canary roots separate.

Validate before enabling writes:

```bash
PYTHONPATH=src python3 -m codex_dispatcher doctor \
  --config /etc/codex-dispatcher/config.toml --json

PYTHONPATH=src python3 -m codex_dispatcher ssh-preflight \
  --config /etc/codex-dispatcher/config.toml --json
```

Preflight must report one expected candidate/recovery or strict idle, no unexpected rejection, and
`authorizes_apply=false`. It does not replace the write sweep's own validation.

## 3. Runner Host

Prepare a rebuildable Linux host with enough CPU/RAM/disk for the reviewed image size plus host
reserve. Install pinned Git and Codex CLI, rootless Docker, fuse/ext4 support, OpenSSH server, the
fixed egress proxy/firewall policy, and a locked `codex-runner` protocol account.

The account must have no sudo, extra groups, SSH agent forwarding, port forwarding, X11, PTY,
GitHub write credential, Control login key, production secret, private network access, or host
filesystem mount. Its authorized key invokes only `/srv/codex-runner/bin/codex-runner-v1`.

Root protects:

```text
/srv/codex-runner/releases/
/srv/codex-runner/current
/srv/codex-runner/bin/codex-runner-v1
/srv/codex-runner/etc/config.json
/srv/codex-runner/etc/agent-result*.schema.json
/srv/codex-runner/etc/reclamation-rollback-references.json
/srv/codex-runner/reclamation-{plans,receipts}/
/srv/codex-runner/app/auth.json
```

The protocol account owns only bounded runtime data such as WorkItem images/registries, generation
homes/auth copies, mounts, and the active lock. Verify rootless Docker, proxy, firewall, mount
options, resource limits, capacity reply, and zero unexpected containers.

## 4. Codex authentication and agent policy

- Log the Runner-wide seed in using ChatGPT without printing or copying `auth.json` off-host.
- Verify only `codex login status`, ownership/mode/size/hash metadata, and immutable seed behavior.
- Each WorkItem/generation receives its own auth copy and binding; the container never mounts the
  shared seed.
- The Runner checks login status immediately before every START/RESUME. Failure requires operator
  re-login; do not edit tokens or build a separate Dispatcher refresh mechanism.
- Install the reviewed primary Sol policy and direct-child profiles. Fix role/model/reasoning limits
  in Runner configuration, not in the Issue.
- Confirm the metadata-only delegation receipt can read only the isolated generation state and
  rejects unknown roles, indirect delegation, version drift, count rollback, or policy mismatch.

## 5. GitHub and Slack credentials

Preferred GitHub model is a short-lived installation token scoped to explicitly selected
repositories. Minimum operations are Metadata read, Contents read/write for the fixed task branch,
Issues read/write, Pull requests read/write, and Actions read. Do not grant Administration, Secrets,
Environments, Deployments, Actions write, bypass, force-push, tag, merge, or arbitrary ref deletion.

Slack should be restricted to the configured workspace/channel and only the read/write scopes used
for root/reply recovery and health alert projection. Slack never supplies task input.

Before live operation, exercise credential failure with redacted errors and prove credentials are
absent from argv, TOML, Git config, prompt, Runner mount, Git objects, Slack text, and receipts.

## 6. systemd installation

Use the reviewed units in `deploy/systemd/` and fixed wrappers in `scripts/`. Run:

```bash
systemd-analyze verify \
  deploy/systemd/codex-dispatcher.service \
  deploy/systemd/codex-dispatcher.timer \
  deploy/systemd/codex-dispatcher-health.service \
  deploy/systemd/codex-dispatcher-backup.service \
  deploy/systemd/codex-dispatcher-restore-drill.service
```

Manually run backup and restore-drill services before enabling timers. Require mode-`0600` backup,
integrity `ok`, zero foreign-key violations, exact migration ledger, and removal of the temporary
restore. Confirm dispatcher/health/backup/restore timers and Runner capacity timer are enabled and
active only after the release handoff is observed.

## 7. Release authorization packet

Before a release apply, list:

- exact source commit and archive SHA-256;
- current and immediate rollback commits on both hosts;
- protected configuration hashes and any required config edit;
- target hosts, service accounts, units, symlinks, and receipt path;
- database backup and integrity result;
- active service/Turn/container/lock prechecks;
- test, compile, wrapper, and systemd verification results;
- exact apply command, expected effect, blast radius, rollback command, and post-checks.

After apply, keep timers stopped while running `ssh-preflight`, one write-enabled recovery-first
sweep, lifecycle health, release read-back, and backup/restore checks. Only then restore timers.

## 8. First Issue canary

Use a credential-free Fixture Issue with one allowed file and one deterministic required Actions
workflow. Before the write, record exact Issue node ID, labels, Ready actor, base SHA, absence of task
branch/PR, database integrity, Runner capacity, and current releases.

Acceptance requires:

- one WorkItem/branch/directory/Slack thread;
- one primary Sol Turn and policy-conformant delegation evidence;
- exact bundle/HEAD and only allowed paths;
- one Draft PR and exact-head Actions success;
- structured AC pass and fresh Audit when configured;
- no base-branch change, merge, release, deployment, secret leak, second session, or second PR;
- repeated preflight/sweep idle.

Fault injection, process kill, host restart, rollback, archive, branch cleanup, and disk reclamation
are separate authorization checkpoints. Do not combine them into the first happy-path approval.

## 9. Ongoing operations

- Review health alerts, GitHub API budget/cursor age, backup age, restore-drill receipts, Runner
  capacity, follow-up backlog, archive/branch backlog, and timer state.
- Generate exact release/image reclamation plans before space becomes urgent; never use broad prune.
- Preserve current and immediate rollback releases plus every referenced image/receipt.
- Keep terminal Issues open when they are used as durable audit anchors.
- Add live evidence only for a new boundary or provider behavior, not for routine idle sweeps.

## 10. Stop conditions

Stop the Dispatcher and keep state intact if identity conflicts, unknown Turn state, provider receipt
ambiguity, credential leakage, ruleset/permission drift, database corruption, Runner isolation drift,
capacity failure, or unexpected external writes occur. Revoke affected credentials if needed. Do not
delete SQLite, backups, WorkItems, registries, tombstones, task branches, PRs, or release receipts
until recovery planning identifies an exact target and rollback boundary.
