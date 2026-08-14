# Owner preparation checklist

This checklist reflects the remote Linux Codex CLI architecture agreed on 2026-08-14. Do not send
credential values in chat, Issue bodies, Slack, repository files, or TOML configuration.

## Already prepared

- Source repository: `longwdl/codex_cloud_task_scheduler`, private, default branch `main`.
- Fixture repository: `longwdl/codex-dispatcher-fixture`, private, intended default branch `main`.
- GitHub maintainer: `longwdl`.
- `gh` is installed and authenticated with access restricted to the two repositories.
- Slack workspace `codex-nt54555` and the initial project channel exist.
- A Codex Cloud Environment ID was supplied earlier, but it is no longer used by the target
  architecture.

## Prepare before the SSH fixture

### 1. Linux Control Host

Prepare a non-production Linux host or VM with:

- 2 vCPU, 4 GiB RAM, and 50 GiB SSD minimum;
- Python 3.12+, Git, OpenSSH client, SQLite support, and systemd;
- outbound access to GitHub and Slack;
- a stable hostname and backups for `/var/lib/codex-dispatcher`;
- no production database, deployment, Kubernetes, cloud, or personal credentials.

Do not install Publisher credentials until its fixed-parameter contract and offline tests pass.

### 2. Dedicated Linux Runner

Prepare a rebuildable Linux host with:

- 4 vCPU, 8 GiB RAM, and 100 GiB SSD minimum;
- Python/build tools needed by the fixture, Git, OpenSSH server, and a pinned Codex CLI;
- outbound access required for Codex authentication and the fixture task;
- no GitHub write credential;
- no Control Host login key, SSH agent, production secret, personal data, or host filesystem mount.

The first fixture intentionally runs without Docker or per-task operating-system restrictions. The
owner accepts loss or corruption of Runner-local task directories and Codex session state. Do not
place a higher-value repository on this Runner until the Docker hardening phase is complete.

Return later, without secrets:

- Runner hostname or address;
- SSH port;
- dedicated Runner username;
- SSH host-key fingerprint;
- installed Git and Codex CLI versions;
- absolute work-item root, normally `/srv/codex-runner/work-items`.

### 3. Codex authentication on Runner

Use a dedicated, low-blast-radius Codex/OpenAI credential suitable for unattended `codex exec`.
Keep it outside task repositories, prompts, logs, GitHub, and Slack. The first fixture must prove
that generated child commands do not receive unrelated Control Host or GitHub credentials.

### 4. GitHub execution label

The new backend label is:

```text
exec:ssh-cli
```

Keep `exec:cloud` only for historical evidence. Adding or changing labels is a GitHub remote-state
operation and should occur when the new Tracker contract is ready for a live fixture.

### 5. Publisher authentication

Preferred long-term option: a GitHub App installed only on explicitly selected repositories.

- Dispatcher operations: Metadata read, Contents read, Issues write, Pull requests write.
- Publisher operation: temporary Contents write token.
- No Administration, Secrets, Environments, Deployments, Actions write, or bypass permission.
- The App must not merge, force-push, delete refs, write tags, or bypass default-branch protection.

For the private fixture, the previously accepted absence of a personal-account ruleset remains a
known residual risk. This does not permit exposing the write token to Codex; Publisher parameter
restrictions remain mandatory.

### 6. Slack outbound app

The existing official Codex Slack binding is not the Dispatcher integration. A custom outbound-only
Slack app will eventually need:

- permission to post in the selected private project channel;
- a bot token stored only on the Control Host;
- no Events API subscription, Socket Mode, slash commands, interactions, message-history input, or
  task-control capability.

The GitHub Issue will store a direct Slack thread link. Human task input remains in GitHub only.

## Keep out of scope

- Codex Cloud execution;
- Slack-to-Codex input;
- automatic merge, release, or deployment;
- production repositories or credentials during the unrestricted Runner phase;
- multiple active Turns or multiple Dispatcher instances;
- Docker until the SSH Runner fixture and offline contracts are proven.
