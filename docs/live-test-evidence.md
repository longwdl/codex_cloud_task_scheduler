# Live test evidence

> The Codex Cloud-oriented sections are retained as historical evidence only. `exec:cloud` and the
> Cloud Environment are not part of the current SSH CLI target architecture.

## Linux Control Host systemd artifact validation — 2026-08-20

The repository's fixed Control Host wrapper, `Type=oneshot` service, inactive-relative timer, and
root-only environment-file template were validated without installing or activating them. Five
offline invariant tests proved the fixed service user and argv, absence of fixture/preflight entry
points, explicit write gate template without credential values, protected state/runtime write
boundary, empty capability sets, argument-free wrapper, and non-overlapping timer shape.

All 324 offline tests passed. `sh -n`, `compileall`, and `git diff --check` also passed. A temporary
credential-free copy was transferred to a `mktemp` directory on the dedicated Linux Fixture host,
where systemd 255 accepted both units with `systemd-analyze verify --recursive-errors=no`. The same
temporary service scored `2.8 OK` under offline `systemd-analyze security`; the remaining exposure
was principally the required Internet/Unix sockets plus syscall/IP filters intentionally deferred
until the actual Control Host is selected. The temporary directory was removed afterward. No unit
was installed, no manager reload/start/enable occurred, and no Dispatcher, GitHub, Slack, Runner,
Publisher, branch, PR, merge, deployment, or release action was invoked.

The serviceization checkpoint then added a credential-free SQLite Online Backup command plus a
network-isolated daily oneshot/timer. Six additional offline tests exercised a real migrated state
database, source and backup integrity checks, mode-`0600` atomic publication, collision refusal,
source-symlink and weak-directory rejection, failed-staging cleanup, CLI assembly without tokens,
and the backup unit/timer invariants. The full suite passed at 330 tests.

All four dispatcher/backup units and timers passed the same temporary Linux systemd 255 verification
without installation. The backup service scored `2.1 OK` in the offline security audit. Its timer
was not enabled; no real Control Host database or backup path was opened, no credential environment
was loaded, and no existing backup was deleted.

## Linux Control Host production initialization and activation — 2026-08-20/21

The dedicated `s2` Control Host was initialized to the reviewed production filesystem and ownership
boundary at checkpoint `2a1be9dd127e08acc4b6460e7a781d5c47ba584c`. A root-owned CPython 3.14.7
was built from the official source checksum below `/opt/codex-python`, without replacing Ubuntu's
system Python. The immutable Dispatcher release, atomic `current` symlink, no-login
`codex-dispatcher` account, protected `/etc/codex-dispatcher` configuration and environment,
mode-`0600` Runner identity and known-host pin, mode-`0700` mutable roots, migrated SQLite state,
and four root-owned system units were installed. Credential values, private-key contents, Prompts,
and full Runner output were not printed or recorded.

The target host passed all 336 offline tests under the service account with the production Python,
minimal environment, `umask 077`, and service-owned temporary directory. Target `compileall`,
configuration loading, SQLite `integrity_check`, and `systemd-analyze verify` also passed. The loaded
Dispatcher and backup services scored `2.8 OK` and `2.1 OK`, respectively. One manually started,
credential-free, network-isolated backup oneshot created a service-owned mode-`0600` 147,456-byte
snapshot; its integrity was `ok` and its WorkItem/Turn counts matched the source database.

A transient service-account `ssh-preflight` loaded the same protected GitHub credential through
systemd, verified the exact Git, `gh`, and OpenSSH pins, and returned `idle`,
`external_writes=false`, and `authorizes_apply=false`. The configured state database SHA-256 was
unchanged before and after preflight. The command did not connect to the Runner or perform a GitHub,
Slack, branch, PR, merge, deployment, release, or tag write.

Initial activation failed closed at the documented host-capacity gate. The host exposed 1 vCPU,
980,152 KiB RAM, no swap, and a 20,747,476,992-byte root filesystem with 12,606,046,208 bytes
available, below the production minimum of 2 vCPU, 4 GiB RAM, and 50 GiB SSD. The write-enabled
service and both timers therefore remained inactive until the maintainer explicitly directed
activation without expanding the host on 2026-08-21. This is an accepted operating exception, not
evidence that the host meets the production baseline; memory exhaustion and disk pressure remain
open risks.

Before the exception was applied, a fresh protected backup again passed `integrity_check`, bringing
the retained backup count to two. SQLite had no active run or Dispatcher lock, and a new read-only
preflight again returned `idle` with an unchanged database SHA-256. One manually observed
`codex-dispatcher.service` start then returned `status=idle` and exited successfully. Independent
SQLite status and another read-only preflight remained idle and unchanged.

Both system timers were then enabled and started. Starting the Dispatcher timer immediately caused
one expected recovery-first sweep because its boot-relative deadline had already passed; that sweep
also returned `idle` and exited successfully. The Dispatcher timer was active with its next
inactive-relative sweep scheduled two minutes later, while the network-isolated backup timer was
active with its next daily run scheduled for the following calendar day. Final SQLite integrity and
GitHub preflight were unchanged and idle. No WorkItem, Turn, Issue, Runner, Slack, branch, PR,
Action, merge, deployment, release, or tag write occurred during activation.

Exact local and remote deployment staging directories, build scripts, and build logs were deleted
after verification; no installed release, configuration, state, or backup was removed. Emergency
rollback remains stopping and disabling both timers while preserving the database, backups,
mirrors, Runner state, branches, and pull requests for reconciliation.

## Runner dedicated account and root-owned SSH boundary — 2026-08-21

The dedicated `s3` Runner was migrated from the interactive `ecs-user` account to the locked,
non-sudo, no-supplementary-group `codex-runner` protocol account. Before mutation, the Control Host
timer was disabled, the service was inactive, a new mode-`0600` SQLite Online Backup passed
integrity validation, read-only preflight returned `idle`, no local active run or exact Runner/Codex
process existed, and the Runner global lock was acquirable. Five Codex SQLite databases returned
`quick_check=ok`; 10 WorkItems, 10 session files, and 13 finished remote Turn records were recorded
without reading credential, session, Prompt, or result contents.

Offline commits `219e084`, `d9f2338`, and `dcadba6` defined the ownership, fixed Docker planning, and
trusted-parent-chain contracts. All 345 tests, `compileall`, JSON parsing, wrapper syntax, and diff
checks passed locally and again on Ubuntu with the production `umask 077`. Runtime release
`dcadba6dd8d7b44c28a684901bf2b2bc4568c3a5` was installed root-owned. The complete Codex 0.147.0
distribution was copied from the administrator-owned Linuxbrew tree into the versioned root-owned
`/srv/codex-runner/tools/` tree with an identical binary SHA-256 and version result. This prevents a
non-sudo administrator account from replacing the configured executable through a writable parent.

The Runner root, releases, tools, wrapper, Schema, configuration, current symlink, and sshd drop-in
are now root-owned. Only `app`, `run`, `work-items`, and `/var/lib/codex-runner/home` are writable by
`codex-runner`. The protected config loads under the new account, ChatGPT login status remains valid,
all five Codex SQLite checks still pass, and the WorkItem/session/finished-Turn counts are unchanged.
The external authorized-key file is `root:codex-runner` mode `0640`: an initial root-only mode
correctly failed authentication because privilege-separated sshd could not read it, so the contract
was fixed in `28e140d` without making the public key writable or relaxing sshd. `sshd -t` and the
effective Match configuration proved public-key-only authentication, the exact force command, and
disabled password, keyboard-interactive, TTY, forwarding, tunnel, agent, X11, and user-rc features.
Only `reload` was used, and a separately held `ecs-user` administrator session survived it.

Before changing the Control Host username, an explicit `codex-runner` connection returned the
existing WorkItem `wi_9eb14638cf6691e8b2a783bb` and Turn
`turn_54ec2bff87594f53bb668ae8bf950bc1` as `finished` through STATUS with no artifact. After the
single protected TOML field was atomically changed, a configuration-derived STATUS returned the same
identity and state. It did not START/RESUME, resend a Prompt, create a session, or export a bundle.

Two manually observed sweeps, final read-only preflight, and the first timer-triggered sweep were all
`idle`. Control SQLite remained `integrity=ok` with no active run; its Slack table retained four
delivered records. The private Fixture remained on default branch `main`, with six existing open PRs;
its five most recent accessible Actions runs were all completed successfully. Runner ownership had
no exceptions, its lock remained acquirable, and no exact Codex process remained. The Dispatcher
timer was re-enabled, the exact temporary release staging directory was removed, and rollback copies
of both Runner and Control Host configurations plus the old wrapper and releases were retained. The
temporary `ecs-user` NOPASSWD bootstrap rule was moved out of the sudo include directory into a
root-only mode-`0600` rollback file; a new ordinary session proved passwordless sudo was no longer
available.

This completes the account, immutable-input, and SSH authorization boundary only. Docker is still
absent on `s3`, as are the rootless helper binaries and user manager. Cgroup v2 and subordinate ID
ranges are available, but host output/forward policy is permissive, private and cloud-service routes
exist, and WorkItems remain on ext4 without project quota. Per-WorkItem auth/session storage, an
allowlisted egress path with internal/metadata denial, and a hard aggregate disk limit remain
mandatory gates before container activation or admission of higher-value repositories.

## Runner CODEX_HOME isolation migration — 2026-08-20

The dedicated `s3` Fixture Runner moved its shared Codex-managed state from
`/srv/codex-runner` to the protected `/srv/codex-runner/app` directory. The business directories
`bin`, `current`, `etc`, `releases`, `run`, and `work-items` remained at the Runner root. No release,
task repository, Runner protocol state, or forced-command path moved.

Migration began only after a read-only Dispatcher preflight returned `idle`, no Codex or Runner
process was active, the global Runner lock was acquired, five Codex SQLite databases returned
`quick_check=ok`, and the source and destination were confirmed to be on the same filesystem. The
first guarded attempt failed closed before any move because the SQLite checks had materialized
additional WAL/SHM sidecars; the root, configuration, and absent destination were independently
verified unchanged. The exact allowlist was extended only for those Codex-owned sidecars.

The successful attempt atomically renamed 28 exact Codex-owned entries while holding the global
lock, atomically changed only `codex_home` in the protected Runner configuration, and retained the
mode-`0600` rollback copy
`/srv/codex-runner/etc/config.json.pre-codex-home-app-20260820T123232Z`. Post-checks found only the
six business entries at the Runner root, `/srv/codex-runner/app` at mode `0700`, `auth.json` at mode
`0600`, all five SQLite checks still `ok`, and the original session-file count unchanged. Credential
and session contents were never read or printed.

With a minimal environment, `CODEX_HOME=/srv/codex-runner/app codex login status` reported the
existing ChatGPT login. A real forced-command `STATUS` request then returned the existing fixture
Turn as `finished` with its original WorkItem, Turn, directory, and Codex session binding. It did not
start or resume Codex, resend a Prompt, export an artifact, or perform a GitHub, Slack, branch, PR,
merge, deployment, or release write.

After the example configuration and current architecture/operations documentation were updated,
all 319 offline tests passed. `compileall`, example JSON parsing, `git diff --check`, and a
credential-pattern diff scan also passed. A final read-only Dispatcher `ssh-preflight` returned
`ok=true`, `status=idle`, and `external_writes=false` with no selected Issue, WorkItem, Turn, or
recovery action.

## Runner audited egress production activation — 2026-08-21

The dedicated `s3` Runner now enforces audited outbound access for the host-level `codex-runner`
account. Before mutation, the `s2` Dispatcher timer and service were inactive, the backup timer
remained active, a new mode-`0600` SQLite Online Backup returned `integrity_check=ok`, and a
transient service-account `ssh-preflight` returned strict `idle` without external writes. The
Runner had no exact Runner/Codex process or pre-existing outbound socket, its global lock was
acquirable, port 3128 was unused, UFW was inactive, and the existing nftables/iptables/ip6tables
state plus Runner configuration and release target were copied into the checksummed root-only
rollback directory.

Commits `66c271d` through `e44b66b` added the standard-library policy validator, canonical
credential-free proxy configuration, complete Squid policy, metadata-only audit format, protected
tmpfiles/logrotate rules, and the host-specific nftables OUTPUT table. Every checkpoint passed all
363 offline tests, `compileall`, and `git diff --check`; the final immutable release passed the same
363 tests and `compileall` on both Linux hosts. Ubuntu Squid `6.14-0ubuntu0.24.04.4` was installed
with its distribution service masked before package installation, and its package AppArmor profile
remained loaded in enforce mode.

The dedicated `inet codex_egress` table changes only OUTPUT handling. UID 1002 can open new TCP
connections only to `127.0.0.1:3128`; UID 13 can use the local resolved stub and public TCP/443 but
is rejected from private, metadata, site-blocked, non-443, and other destinations. Other local UIDs
cannot connect to the proxy. The proxy listens only on IPv4 loopback. Squid's required coordinator
runs as root with only `CAP_SETUID` and `CAP_SETGID`; its UID-13 worker owns the listener and has an
empty effective capability set. The coordinator had no TCP socket, the package default listener
never appeared, INPUT/FORWARD remained unchanged, and administrator SSH survived every step.

Credential-free probes produced one exact audit record per accepted or denied CONNECT request. An
allowlisted `api.openai.com` TLS tunnel reached the origin and returned HTTP 421 without credentials;
`example.com`, private space, metadata, and the public Control Host address were denied. Direct
Runner TCP/443, DNS, UDP/443, and non-proxy loopback connections failed, while another unprivileged
UID was rejected before Squid. Stopping the proxy failed closed and restarting restored the UID-13
listener. A forced real log rotation preserved the original audit inode as the rotated file,
created `proxy:proxy 0640` replacements below a root-owned non-writable directory, and the next
denied request was recorded exactly once. Every access-log line matched the fixed metadata-only
schema, and no Squid AppArmor denial was recorded.

Only after those probes passed was `/srv/codex-runner/etc/config.json` atomically changed to the
fixed `http://127.0.0.1:3128` endpoint. The real Runner configuration loader accepted it as the
locked `codex-runner` account. Both `s2` and `s3` then switched atomically to immutable release
`e44b66bddeda415267674a0590423187fcc45fe4`; two post-switch read-only preflights returned strict
idle. Restoring the Dispatcher timer produced four observed successful idle sweeps with no selected
Issue, WorkItem, or Turn. A final read-only Fixture snapshot found six open pull requests, no active
Actions run, and unchanged `main` SHA `f5037925502905fd3d22a807df7291ba1004bab9`.

No Fixture GitHub, Slack, branch, pull-request, merge, release, tag, or application-data write was
performed by this infrastructure activation. The temporary `s3` sudoers grant was moved into the root-only rollback
directory at mode `0600`, and a new SSH session proved passwordless sudo unavailable. The Dispatcher
and backup timers, proxy, firewall, AppArmor policy, audit retention, previous releases, original
Runner configuration, and SQLite backup remain independently recoverable. The 14-domain bootstrap
allowlist is version-specific evidence from Codex CLI 0.147.0, not an upstream compatibility
guarantee. Container-visible proxy routing and aggregate per-WorkItem disk isolation remain required
before rootless container activation.

## Runner rootless Docker host bootstrap — 2026-08-21

The dedicated `s3` Runner now has an initialized but not yet admitted rootless Docker engine. Before
the host change, the `s2` Dispatcher timer was disabled, the service was inactive, a new protected
SQLite Online Backup returned `integrity_check=ok`, local status had no active run, and a read-only
`ssh-preflight` returned strict `idle` without external writes. The Runner global lock was
acquirable, no exact Runner/Codex/Docker process existed, and the existing Runner configuration,
package inventory, and nftables state were copied into a checksummed root-only rollback directory.

The official Docker Ubuntu repository key was verified by its full fingerprint before the exact
Ubuntu 24.04 packages were installed. Docker Engine, CLI, and rootless extras are pinned at
`5:29.7.2-1~ubuntu.24.04~noble`; `containerd.io` is pinned at
`2.3.3-1~ubuntu.24.04~noble`. The four packages are held. The root-owned system-level
`docker.service`, `docker.socket`, and `containerd.service` remained masked throughout installation,
stayed inactive afterward, and exposed no rootful socket.

Linger and the user manager were enabled only for the locked `codex-runner` account. Its rootless
`docker.service` is active and enabled on the mode-`0700` runtime directory, with the API socket
owned by that account. The daemon reports Docker 29.7.2, `overlayfs`, cgroup v2, and the `rootless`,
`seccomp`, and `cgroupns` security options. A root-owned drop-in fixes `slirp4netns` and explicitly
permits the RootlessKit host-loopback path required by the future container-visible proxy endpoint;
the protected empty Docker CLI configuration prevents inherited user contexts or credentials from
selecting another daemon.

No image was pulled or built, no container was created, and no non-default Docker network exists.
The Runner remained on immutable release `e44b66bddeda415267674a0590423187fcc45fe4` with the legacy
direct execution mode and the existing audited loopback proxy. The new offline rootless execution
implementation at commit `ec34b19` passed all 371 unit tests, `compileall`, and `git diff --check`,
but was not deployed and was not enabled in production.

After initialization, the Runner lock was still idle and the rootless daemon still had zero images
and zero containers. The existing proxy and nftables services remained active, a normal timer sweep
exited successfully with no active run, and a second read-only `ssh-preflight` again returned
`idle`, `external_writes=false`, and `authorizes_apply=false`. The Dispatcher timer was restored.
No GitHub, Slack, WorkItem, Turn, branch, pull request, Action, merge, deployment, release, or tag
write was performed by this bootstrap. The temporary `s3` sudoers grant was moved into the
root-only rollback directory at mode `0600`; an independent SSH connection then proved that
passwordless sudo was no longer available while the rootless daemon remained active.

This checkpoint proves only package provenance, rootless daemon ownership, rootful exclusion, and
safe coexistence with the direct Runner. It does not admit container Turns. The next separately
authorized gates are an aggregate disk-limit/FUSE proof, immutable image acquisition and digest
verification, dedicated proxy-only Docker networking, and one disposable container boundary test
before any Runner configuration switch. Rollback is to freeze the Dispatcher timer, stop and
disable the user `docker.service`, disable linger, and retain the masked rootful units and persistent
Runner state for reconciliation.

## Slack Web API exact-retry fixture — 2026-08-20

This fixture proves the provider-side contract required before enabling the real outbound Slack
publisher. It targeted only Workspace `T0BQ60N9WH4` and private channel `C0BR2D0MS8Y` with the
installed outbound-only Slack App. Before the write, `auth.test` matched the exact Workspace and a
bot identity, the token reported `chat:write`, the protected Dispatcher configuration parsed, and a
read-only SSH preflight returned `idle` with `external_writes=false`.

The source-tree-only `slack-idempotency-fixture` entry required `--apply`, exact Workspace/channel
arguments, canonical UUIDv4 fixture `38667953-3dfa-4ee8-8c95-16c613ae450d`, and the ephemeral
`CODEX_DISPATCHER_ENABLE_SLACK_FIXTURE_WRITES=1` gate. It sent the exact same root report twice with
stable client message ID `c22ecb14-02ae-5cb0-8078-2fbba4d87a6b`, markup, mention expansion, link
unfurling, and proxy inheritance disabled. The requests were spaced to respect the per-channel
posting limit and had no automatic retry.

Both calls returned channel `C0BR2D0MS8Y`, message timestamp `1787213067.081109`, and the same
[permalink](https://codex-nt54555.slack.com/archives/C0BR2D0MS8Y/p1787213067081109).
The maintainer independently inspected the private channel and confirmed that the Fixture ID was
visible in exactly one message. The fixture write gate was absent in a new login shell afterward.

Immediately before the fixture, all 308 offline tests passed; `compileall`, CLI help, and
`git diff --check` also passed. The fixture did not open or mutate SQLite and did not access GitHub,
the Runner, Actions, branches, pull requests, Issues, merges, releases, or deployment facilities.
It proves the real `chat.postMessage` exact-retry receipt contract for this App/Workspace/channel;
it does not replace a later end-to-end WorkItem/Turn/Slack-thread acceptance test.

## Slack outbound normal end-to-end fixture — 2026-08-20

This fixture exercised the optional real Slack publisher through the normal recovery-first
`ssh-run-once` entry point rather than a fault hook. The maintainer created and separately moved
private Fixture Issue
[`#16`](https://github.com/longwdl/codex-dispatcher-fixture/issues/16) from `agent:paused` to
`agent:ready`. Its strict task specification allowed only one existing README fixture value to
change from `p1-ssh-transport-01` to `p1-slack-e2e-01`; Codex was forbidden to push, open a PR,
merge, deploy, release, delete a branch, or modify another file.

Before the write, SQLite passed `integrity_check`, no WorkItem or Turn was active, protected config
with `[slack_runtime]` parsed, and read-only preflight uniquely selected Issue `#16` as
`ready_candidate`. The first normal invocation failed closed at `trusted mirror Git stage failed:
base_fetch` before claim: the Issue remained ready with no comment, and SQLite contained no Issue
`#16` WorkItem or Turn. Direct GitHub connectivity then recovered, a second mode-`0600` online
backup passed `integrity_check`, and preflight again uniquely selected the same Issue. The accepted
retry used the normal SSH and Slack write opt-ins and returned `review`.

The follow-up fix retries only this fixed read-only `base_fetch` once, with both attempts sharing
the original 120-second deadline. It does not retry mirror/config/SHA validation, does not classify
or expose provider stderr, and leaves persistent failures generic. Offline tests prove transient
success, exactly two persistent-failure attempts, and no second attempt after budget exhaustion;
the existing pre-claim ordering still proves no Issue, SQLite, Runner, or Prompt write can precede
the fetch.

After the fix was committed, the real protected refresher fetched only
`longwdl/codex-dispatcher-fixture` `main` and resolved exact current SHA
`f5037925502905fd3d22a807df7291ba1004bab9`; it performed no remote write. This confirms the live
credential, fixed argv, protected mirror, and SHA-verification path, but deliberately does not
manufacture a network outage. The original provider-level failure subtype remains unknown because
raw Git stderr is intentionally neither persisted nor exposed.

### Stable identities and receipts

- WorkItem: `wi_a6e9f94abfd96a11e9e70ec6`.
- Turn: `turn_2dbdc421755144498012f0cf73947d01`, number `1`, finished/completed.
- Codex session: `01a01e6d-5836-7be3-8d41-817d78370746`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-16`.
- Base SHA: `790c3e0b361f727863e3e6d86ee6e2dce16b4faf`.
- Published/output SHA: `77504f36c59c2448a6704cdf0e80c3bd099f3345`.
- Task branch: `codex/issue-16-a6e9f94abfd9`.
- Slack [root](https://codex-nt54555.slack.com/archives/C0BR2D0MS8Y/p1787216871827959),
  timestamp `1787216871.827959`.
- Slack [result reply](https://codex-nt54555.slack.com/archives/C0BR2D0MS8Y/p1787216946860319?thread_ts=1787216871.827959&cid=C0BR2D0MS8Y),
  timestamp `1787216946.860319`.
- Draft PR
  [`#17`](https://github.com/longwdl/codex-dispatcher-fixture/pull/17).
- GitHub Actions run
  [`32352400905`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32352400905).

Independent Runner STATUS returned `finished` with the same WorkItem, Turn, session, output SHA,
completed result, and only `README.md` in changed paths. SQLite contained exactly one WorkItem, one
Turn, and two delivered Slack outbox records. GitHub contained one fixed Issue status comment with
the Slack root link and one open CLEAN Draft PR. The PR had one commit and changed only `README.md`,
one insertion and one deletion. The task branch, PR head, SQLite publication SHA, Runner head, and
successful Actions head were identical; `main` remained at the base SHA.

After the result reply existed, a fresh `chat.getPermalink` for the root appended
`thread_ts=1787216871.827959&cid=C0BR2D0MS8Y` to the original bare root URL. The channel, path, and
message timestamp were unchanged, the persisted bare root URL remained valid, and the result reply
permalink remained byte-for-byte identical. The runtime intentionally preserves the original
receipt rather than rewriting durable state for this provider presentation change.

Three attempt/idle-boundary SQLite online backups were retained; each was owned by the Dispatcher
user, mode `0600`, and passed `integrity_check`. Final preflight returned `idle`. An immediate normal
write-enabled sweep also returned `idle`; post-checks still found one WorkItem, one Turn, two Slack
deliveries, one comment, one Draft PR, and one successful Actions run. No second session, branch,
message delivery, PR, or workflow run was created. No merge, default-branch write, deployment,
release, tag, branch deletion, or production access occurred.

## Slack root/result receipt-loss recovery fixture — 2026-08-20

This fixture exercised both real Slack receipt-loss windows through the normal Dispatcher outbox,
using only private Fixture Issue
[`#18`](https://github.com/longwdl/codex-dispatcher-fixture/issues/18) and the already proven
Workspace/channel contract. Before each fault, `auth.test` matched Workspace `T0BQ60N9WH4` and an
installed bot; protected config, exact tool pins, SQLite integrity, a read-only preflight, the SSH
write gate, the Slack write gate, and the repository-valued fault gate all passed. The source-tree
fault path was committed separately before the live writes.

The root stage accepted only a new `ready_candidate`. After the Runner had durably prepared the
WorkItem but before any Turn existed, Slack returned root timestamp `1787223231.513909` and its
[permalink](https://codex-nt54555.slack.com/archives/C0BR2D0MS8Y/p1787223231513909).
The fixture discarded that complete receipt. SQLite then contained WorkItem
`wi_887b852ac5834765ac2571a7` in `ready`, no Turn or Codex session, no branch checkpoint or PR, no
Slack thread binding, and exactly one `prepared` root outbox record. The Issue was
`agent:dispatching`; read-only preflight selected only `start_claimed_turn`. The verified online
backup `state.pre-slack-root-receipt-i11tk0y3.db` was mode `0600` and passed `integrity_check`.

The terminal stage was admitted only from that exact root-recovery state. The same root request
returned the original timestamp/permalink and was atomically bound before execution. The one Turn
then finished, the exact checkpoint was published, Draft PR
[`#19`](https://github.com/longwdl/codex-dispatcher-fixture/pull/19) was bound, and the fixed Issue
comment was updated. Slack returned result timestamp `1787223348.057679` and its
[reply permalink](https://codex-nt54555.slack.com/archives/C0BR2D0MS8Y/p1787223348057679?thread_ts=1787223231.513909&cid=C0BR2D0MS8Y);
the fixture discarded only that complete receipt. SQLite retained the WorkItem in `review`, one
finished/completed Turn, the root as `delivered`, and only the result as `prepared`; the Issue stayed
`agent:dispatching`. Read-only preflight selected only `sync_tracker_state`. The second verified
online backup `state.pre-slack-terminal-receipt-3fmok59u.db` was mode `0600` and passed
`integrity_check`.

One ordinary `ssh-run-once` retried the same result outbox identity and returned
`state_synchronized`. Its durable timestamp and permalink were byte-for-byte equal to the discarded
receipt, and the Issue moved to `agent:review`. Final identities were:

- one WorkItem `wi_887b852ac5834765ac2571a7`;
- one Turn `turn_347bda0de02442899af7f203d5c117d2` and Codex session
  `01a01ecf-60b4-7121-8ca4-ceeb62fb6d4e`;
- base/main SHA `790c3e0b361f727863e3e6d86ee6e2dce16b4faf` and checkpoint
  `7a90afd4dbbec378dbfb7cad45bfa3c72452e8a2`;
- one branch `codex/issue-18-887b852ac583`, one Draft PR `#19`, one fixed Issue comment, and two
  delivered Slack records in the original thread;
- one successful exact-head Actions run
  [`32361317694`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32361317694).

Independent Runner `STATUS` returned `finished` for the same WorkItem, Turn, and session with no
artifact. Fixture `main` remained at the base SHA. A final read-only preflight and an immediate
write-enabled sweep both returned `idle`; counts and receipts were unchanged. The Dispatcher Slack
App still has no history/search or inbound scope. Exact-retry equality plus the separately
human-confirmed provider idempotency contract proves recovery without a second delivery; visual
inspection of this private thread remains a maintainer-only cross-check. No merge, deployment,
release, branch deletion, tag, Issue close, or production access occurred.

## GitHub completion comment/label receipt-loss recovery fixture — 2026-08-20

This fixture completed the lifecycle of the same private Issue
[`#18`](https://github.com/longwdl/codex-dispatcher-fixture/issues/18). After explicit authorization
to operate the disposable Fixture repository, the operator re-read the one-file README diff,
confirmed Actions run
[`32361317694`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32361317694)
was successful for exact head `7a90afd4dbbec378dbfb7cad45bfa3c72452e8a2`, marked Draft PR
[`#19`](https://github.com/longwdl/codex-dispatcher-fixture/pull/19) ready, and merged it using that
SHA as an optimistic-concurrency guard. This was an authorized operator action outside the
Dispatcher; the Dispatcher still has no merge operation. Fixture `main` advanced to merge commit
`f5037925502905fd3d22a807df7291ba1004bab9`, while the task branch remained at the checkpoint.

The live-only mechanism was committed before the writes and all 317 offline tests, `compileall`,
`git diff --check`, and the staged sensitive-pattern check passed. Read-only preflight then selected
`complete_merged_work_item` for only Issue `#18`, WorkItem
`wi_887b852ac5834765ac2571a7`, and PR `#19`.

The first live attempt failed closed before any target write because recovery planning legitimately
audited an older completed WorkItem first. Its online backup was valid, but
`fault_triggered=false` and the target stayed in review. The fixture wrapper was narrowed so
non-target historical PR reads cannot arm a fault, while every non-target write remains rejected;
the focused and full 317-test suites passed again and the correction was committed separately.

The accepted comment stage first persisted the irreversible local `completed` tombstone, then
updated only the exact fixed comment and discarded its successful response. It left the Issue at
`agent:review`, with no active Turn and no Source, Runner, Git Publisher, or Slack Publisher call.
Backup `state.pre-completion-comment-receipt-kdn_qmp5.db` was mode `0600` and passed
`integrity_check`; preflight then selected only `sync_tracker_state`.

The label stage idempotently updated the same comment, applied `agent:completed`, read the exact
Issue back from GitHub, and only then discarded that verified response. Backup
`state.pre-completion-label-receipt-20gig5xq.db` was also mode `0600` with successful integrity.
The one fixed comment was updated at `2026-08-20T11:50:50Z`; the completed-label timeline event was
later, at `11:53:25Z`. The Issue remained open with exactly one dispatcher comment and retained its
Slack execution link.

Independent final reads proved one completed WorkItem, one finished/completed Turn, the original
session, Runner directory, branch, checkpoint, merged PR, successful Actions run, and two delivered
Slack records. Runner `STATUS` returned `finished` for the same WorkItem, Turn, and session with no
artifact. Final preflight and two ordinary write-enabled sweeps were all `idle`; no new Turn,
session, Prompt, branch, PR, comment, workflow run, Slack delivery, deployment, release, tag, branch
deletion, or Issue close occurred.

## SSH CLI human-merge completion projection fixture — 2026-08-19

This fixture completes the lifecycle of Fixture Issue
[`#12`](https://github.com/longwdl/codex-dispatcher-fixture/issues/12) after its START-receipt
recovery. The dispatcher did not merge the pull request. The maintainer independently reviewed the
README-only diff, confirmed Actions run
[`32169064603`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32169064603) was
successful at exact checkpoint `41e67598b506dcbfeac00e5871a812e6e9874078`, marked Draft PR
[`#13`](https://github.com/longwdl/codex-dispatcher-fixture/pull/13) ready, and explicitly merged it.

Before the completion sweep, read-only preflight selected `complete_merged_work_item` for the exact
Issue, WorkItem `wi_594a1305a087ff78a0ab32f8`, and PR `#13`. The normal double-opt-in sweep then:

- required repository `longwdl/codex-dispatcher-fixture`, base `main`, task branch
  `codex/issue-12-594a1305a087`, and PR head SHA to match the persisted binding;
- committed the irreversible local WorkItem state `completed` before projecting GitHub state;
- updated the one fixed status comment to `agent:completed` at `2026-08-19T01:47:34Z`;
- applied the `agent:completed` label at `2026-08-19T01:47:37Z`, after the comment update;
- left the Issue open and retained the task branch.

Independent read-back found exactly one WorkItem and one finished/completed Turn. SQLite retained
PR `#13` and the exact checkpoint. The task branch still resolved to that checkpoint, while `main`
resolved to merge commit `7fe0a9a5d51f4438423744ffb199563a0bcd4d9a`. No second workflow run,
Turn, session, branch, or PR appeared. A subsequent read-only preflight and a repeated write-enabled
sweep both returned idle.

This proves the normal AC-055 completion path and ordered projection. It does not prove lost-receipt
recovery for the completion comment or label, automatic merging, Issue closing, branch deletion, or
deployment; those actions remain forbidden or separately covered only by offline fault tests.

## SSH CLI exact Dispatcher process-kill recovery fixture — 2026-08-19

This fixture covers the post-claim/pre-persistence operating-system boundary with a real
`SIGKILL`. It does not select a process by name. A dedicated parent starts one exact child argv in a
new session, waits for an identity-bound private-pipe handshake emitted only after GitHub claim, and
kills only that verified still-running child.

### Kill boundary

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#10`](https://github.com/longwdl/codex-dispatcher-fixture/issues/10),
  node ID `I_kwDOT3NfX88AAAABNQOefw`.
- The successful process fixture returned `child_exit_code=-9`, `termination_signal=SIGKILL`,
  `local_work_item_persisted=false`, and `runner_reached=false`.
- SQLite passed `integrity_check`, contained no WorkItem for Issue `#10` and no active Turn, while
  the Issue was exactly `agent:dispatching`.
- Read-only preflight selected `ready_recovery/recover_orphan_claim` for that exact Issue.
- The accepted pre-kill online backup
  `state.pre-claim-acquired-process-kill-0ukms_yw.db` was mode `0600` and passed
  `integrity_check`.

Two earlier pre-claim attempts encountered the then-active 120-second GitHub Git transport timeout.
The first exposed that a shorter parent handshake timeout could terminate Python while its isolated
Git process group was still running; the exact group was stopped, the parent deadline was raised
above the longest bounded pre-claim Git operation, and a second timeout verified clean teardown.
Neither attempt reached claim or changed Issue `#10`. For the successful kill injection only, the
fixture required GitHub REST `main` and the protected cached mirror ref to equal exact SHA
`b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`; this did not alter normal dispatcher fetch behavior.

### Ordinary recovery

When Git HTTPS recovered, one normal double-opt-in `ssh-run-once` consumed the orphan claim without
manual label repair:

- WorkItem: `wi_56cfb4bd6efc095beabb0852`;
- Turn: `turn_44612d44d99b4ac89947b19afd19655f`, number `1`;
- Codex session: `01a01605-beca-7040-adb8-ca3a6e6bd06c`;
- task branch: `codex/issue-10-56cfb4bd6efc`;
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-10`;
- exact checkpoint: `a17ae709a111cd84d7a08050afa975351190fa73`;
- Draft PR [`#11`](https://github.com/longwdl/codex-dispatcher-fixture/pull/11).

SQLite contained exactly one WorkItem and one finished/completed Turn for the Issue. The PR changed
only `README.md`, one insertion and one deletion, to exact marker `p1-process-kill-v1`; its head and
SQLite publication SHA matched. The fixed Issue comment existed once and the Issue entered
`agent:review`. GitHub Actions run
[`32168464039`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32168464039)
completed successfully for that exact branch/SHA. `main` remained at the base SHA, final preflight
was idle, and an immediate second write-enabled sweep also returned idle.

## SSH CLI START receipt STATUS-only recovery fixture — 2026-08-19

This fixture covers AC-036 with two guarded source-tree stages. The first stage delegates a real
START and discards its response only after strict parsing proves the same WorkItem/Turn identity and
a remote state of `running` or `finished`. The second stage rejects PREPARE, START, and RESUME before
delegation and records the exact recovery operation order.

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#12`](https://github.com/longwdl/codex-dispatcher-fixture/issues/12),
  node ID `I_kwDOT3NfX88AAAABNQlrSw`.
- WorkItem: `wi_594a1305a087ff78a0ab32f8`.
- Turn: `turn_be25135c133b47c6a580ee585d71cd18`, number `1`.
- Task branch: `codex/issue-12-594a1305a087`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-12`.

Immediately after `start-receipt`, the command reported `fault_triggered=true`,
`recovery_required=true`, and `runner_active`. Independent SQLite read-back found exactly one
running WorkItem and one `reconciling` Turn; local `codex_session_id`, `last_published_sha`,
`pr_number`, result status, and output SHA were all unset. The Issue was `agent:running`, and
preflight selected only `reconcile_active_turn` for the exact WorkItem/Turn.

The `start-status-recovery` command then reported `recovery_guarded=true`,
`fault_triggered=false`, `runner_operations=["status","export"]`, and `review`. It reused the same
WorkItem and Turn, bound Codex session `01a0160b-4cb9-7522-9b32-41dff7ab73b6`, and published exact
checkpoint `41e67598b506dcbfeac00e5871a812e6e9874078` to the existing task identity. Draft PR
[`#13`](https://github.com/longwdl/codex-dispatcher-fixture/pull/13) changed only `README.md`, one
insertion and one deletion, to exact marker `p1-start-status-v1`. The fixed Issue comment existed
once, the Issue entered `agent:review`, and GitHub Actions run
[`32169064603`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32169064603)
completed successfully for the same branch/SHA.

Both stage backups were mode `0600` and passed `integrity_check`. Final SQLite contained one
WorkItem and one finished/completed Turn, `main` remained at
`b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`, final preflight was idle, and an immediate normal
write-enabled sweep also returned idle. A brief Issue-list visibility delay after creation was
resolved by an explicit maintainer ready-label transition; no fault write ran until the list read and
preflight both selected the exact Issue. This proves STATUS-only protocol recovery and does not claim
a physical network-link or SSH-daemon interruption.

## SSH CLI exact transport-process interruption fixture — 2026-08-19

This fixture covers AC-021 and AC-056 with a real OpenSSH client-process `SIGKILL`. It did not
change the SSH daemon, firewall, routes, host networking, or any unrelated connection. The guarded
entry received only the exact primary child capability; it had no process-name lookup or bulk-kill
path.

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#14`](https://github.com/longwdl/codex-dispatcher-fixture/issues/14),
  node ID `I_kwDOT3NfX88AAAABNUW04w`.
- WorkItem: `wi_b7edba3be957aa3d4a851c56`.
- Turn: `turn_7e8f8db322764d579c51595e4b2725ab`, number `1`.
- Task branch: `codex/issue-14-b7edba3be957`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-14`.
- Pre-live implementation commit: `3221884`.

Before the live write, all 286 offline tests, `compileall`, and `git diff --check` passed. Read-only
preflight selected only Issue `#14`; SQLite had no binding for it, the derived branch and PR did not
exist, and `main` was `7fe0a9a5d51f4438423744ffb199563a0bcd4d9a`. The independently created
pre-live online backup and both command-created backups were mode `0600` and passed
`integrity_check`.

After the local WorkItem and Turn were durable, a separate hook-free SSH transport issued only
STATUS. Attempt 2 observed the Runner's durable executing signature
`state=unknown,error_code=turn_outcome_unresolved`; the local WorkItem was still running and the
same Turn was still starting. The hook freshly verified immutable argv and exact
`PID=PGID=SID=80465`, then sent `SIGKILL` only to that process group. Independent read-back found the
PID gone, SQLite integrity `ok`, one running WorkItem, and the same Turn in `reconciling` with no
local session, output SHA, publication SHA, or PR. Read-only preflight selected exactly
`reconcile_active_turn`.

The guarded recovery rejected PREPARE, START, and RESUME before delegation and reported exact
`runner_operations=["status","export"]`. It reused the same WorkItem and Turn, bound the existing
Runner session (local and remote SHA-256 fingerprint prefix `0926e3c0ccb6901c`), and published exact
checkpoint `fb2fb166a74984298a56811f3e3e52c4676df82c`. Draft PR
[`#15`](https://github.com/longwdl/codex-dispatcher-fixture/pull/15) changed only the allowed Fixture
path. The one fixed Issue comment existed, the Issue entered `agent:review`, and the one GitHub
Actions run
[`32213983342`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32213983342)
completed successfully at that exact SHA.

Final SQLite contained exactly one WorkItem and one finished/completed Turn; direct Runner STATUS
returned the same Turn, session fingerprint, checkpoint, and completed result. The task branch,
SQLite publication record, PR head, and Actions head all matched. Before human merge, `main` did not
move. Two ordinary write-enabled sweeps and the read-only preflight all returned idle, so no second
Turn, session, branch, PR, comment, or workflow run was created.

### Human merge and completed projection

After explicit review confirmed the README-only diff, exact head SHA, and successful Actions run,
PR `#15` was marked ready and merged without deleting its task branch. GitHub created merge commit
`790c3e0b361f727863e3e6d86ee6e2dce16b4faf`; the PR closed as merged while the task branch remained
at checkpoint `fb2fb166a74984298a56811f3e3e52c4676df82c`.

Read-only preflight then selected `complete_merged_work_item` for the exact Issue, WorkItem, and PR.
One normal double-opt-in sweep first committed the local `completed` tombstone, updated the single
fixed status comment at `2026-08-19T06:32:21Z`, and applied `agent:completed` at
`2026-08-19T06:32:24Z`. The Issue stayed open. SQLite retained the original single finished Turn,
session, PR, and published SHA; no Runner or Publisher work was introduced. A subsequent normal
write-enabled sweep and read-only preflight were idle.

## SSH CLI same-Issue needs-input resume fixture — 2026-08-19

This section records the first end-to-end Issue lifecycle with two Turns in one persistent Codex
session. It proves the GitHub `needs_input → ready` path in addition to the earlier direct Runner
resume fixture. Slack remained disabled, and no merge or completion transition was performed.

### Turn 1: deliberate missing input

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#6`](https://github.com/longwdl/codex-dispatcher-fixture/issues/6).
- WorkItem: `wi_e5c93ce564fa68c4be09cc5c`.
- Task branch identity: `codex/issue-6-e5c93ce564fa`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-6`.
- Codex session: `01a015c4-4053-7812-bed0-d0b39db406be`.

The reviewed Issue intentionally omitted the exact README marker value and required the first Turn
to ask rather than guess. Read-only preflight selected exactly Issue `#6`; the write-enabled sweep
returned `needs_input` for Turn `turn_d550ef3560864379981d58304582f975`. SQLite recorded Turn number
`1`, `result_status=needs_input`, and identical input/output HEAD
`b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`. The WorkItem entered `waiting_input`, and the Issue
entered `agent:needs_input`. Independent read-back found no task ref, checkpoint, or PR; `main` did
not move.

### Maintainer context and Turn 2

The maintainer added one reviewed comment:

```text
/codex-context
Use the exact fixture marker value: p1-needs-input-resume-v1
```

Its immutable comment ID was `IC_kwDOT3NfX88AAAABPcWHXQ`. The Issue was then explicitly returned to
`agent:ready`. During this manual transition, the repository's old setup label
`agent:needs-input` was found to conflict with the canonical runtime label `agent:needs_input`, and
the Issue briefly had both `ready` and `needs_input`. Preflight returned idle and no Runner call was
made. The stale hyphenated repository label was unused and removed, the canonical underscore label
was retained, and the missing canonical `agent:completed` label was created. Only after the Issue
had exactly one state label did preflight select it again.

The second write-enabled sweep returned `review` for Turn
`turn_d3879f60603945f8bb1cd605541a3be4`. SQLite recorded Turn number `2`, included only the reviewed
context comment ID, and preserved the original WorkItem, branch, Runner directory, and session. The
checkpoint was `d25edd9d2b3287a596e773c28210f1d9cefa1f05`; Draft PR
[`#7`](https://github.com/longwdl/codex-dispatcher-fixture/pull/7) was the only PR for the task branch
and changed exactly `README.md` by one insertion and one deletion. The resulting marker was exactly
`p1-needs-input-resume-v1`.

GitHub Actions run [`32162425453`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32162425453)
completed successfully for the exact checkpoint and task branch. Final preflight returned idle;
SQLite contained exactly one WorkItem and two ordered Turns for Issue `#6`; `main` remained at its
original SHA. Both pre-Turn SQLite Online Backup API snapshots were mode `0600` and passed
`integrity_check`.

## SSH CLI recorded-publication recovery fixture — 2026-08-19

This fixture covers AC-047's narrow durability window: the exact checkpoint SHA was committed to
SQLite after a successful task-branch publication, but the Turn had not yet advanced from
`checkpointing` to `published`. The source-tree-only fault hook raises at that exact boundary. This
is deterministic process-level exception injection; it does not claim to be an operating-system
process kill or an SSH disconnect.

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#8`](https://github.com/longwdl/codex-dispatcher-fixture/issues/8).
- WorkItem: `wi_9eb14638cf6691e8b2a783bb`.
- Turn: `turn_54ec2bff87594f53bb668ae8bf950bc1`.
- Codex session: `01a015d2-3a31-7bd1-b847-b892efdbb795`.
- Task branch: `codex/issue-8-9eb14638cf66`.
- Exact checkpoint: `bd7ac54774d9d098c35f9731b18a34d732094736`.

The `publication-recorded` stage returned `process_interrupted` only after
`record_published_sha()` committed. Independent read-back then found the WorkItem still `running`,
the Turn `checkpointing/completed`, the Issue `agent:dispatching`, no Issue comment, and no PR. The
remote task branch and SQLite `last_published_sha` both resolved to the exact checkpoint. Read-only
preflight selected `resume_publication` for the same WorkItem and Turn.

The `recorded-publication-recovery` stage wrapped both the Runner transport and Publisher in
fail-before-delegate guards. Recovery completed successfully, which proves that neither port was
invoked: it used only the durable checkpoint record, then created and bound Draft PR
[`#9`](https://github.com/longwdl/codex-dispatcher-fixture/pull/9), wrote the fixed Issue status
comment, and projected `agent:review`. SQLite ended with exactly one WorkItem and one finished Turn;
the branch, session, output SHA, and PR binding were unchanged. The README marker was exactly
`p1-recorded-publication-v1`, `main` did not move, and GitHub Actions run
[`32163520437`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32163520437)
completed successfully for the exact checkpoint.

Both accepted stages created a mode `0600` SQLite Online Backup API snapshot and passed
`integrity_check`. A brief GitHub Issue-list visibility delay was observed immediately after the
initial label write; no write was attempted until direct read-back and a later preflight agreed on
the exact ready candidate.

## SSH CLI lost-receipt fixture — 2026-08-19

This section records the bounded, three-stage live fault sequence for Publisher, Draft PR, and
Issue-comment receipts. It used the source-tree-only triple-opt-in fault entry and the normal
Dispatcher recovery path. This run itself does not prove recovery from an actual SSH disconnect or
process kill, and it does not exercise Slack; the later Issue `#10` and `#12` sections above record
the separate process and START-receipt boundaries.

### Admission and initial state

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#4`](https://github.com/longwdl/codex-dispatcher-fixture/issues/4).
- The strict task spec allowed only `README.md`, changing the marker to
  `ssh-lost-receipt-phase-d-v1`.
- GitHub Issue node ID: `I_kwDOT3NfX88AAAABNPw7tg`.
- WorkItem: `wi_80df527531e34d4f039aa16f`.
- Task branch: `codex/issue-4-80df527531e3`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-4`.
- Fixture `main` was `b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`; neither the task branch nor
  a matching PR existed.

The first `publisher-receipt` attempt created and verified a private SQLite backup, then stopped at
the trusted-mirror `base_fetch` stage before claim, Runner invocation, or external write. A
restricted read-only retry of the same Git ref succeeded, confirming a transient transport failure
rather than an authentication or repository-state conflict. The exact fault stage was then retried.

### Three discarded receipts

1. `publisher-receipt` started exactly one Codex Turn and discarded the successful new-branch push
   receipt. The command returned `awaiting_publication`; Issue `#4` remained `agent:running`, and
   read-back found the task branch at checkpoint
   `00199ebe3d565048eb6118827aaef9e54ab450cf` with no PR. Preflight then required
   `resume_publication`.
2. `draft-pr-receipt` recovered the same checkpoint, reused the existing remote branch, created
   Draft PR [`#5`](https://github.com/longwdl/codex-dispatcher-fixture/pull/5), and discarded its
   receipt. SQLite had reached `review` with the exact published SHA but had no PR binding; Issue
   `#4` remained `agent:running` and still had no status comment.
3. The following read-only preflight exposed a recovery-order defect: a remote `running` Issue was
   classified as orphaned before its existing terminal WorkItem was considered. No third-stage
   write was attempted while that result was ambiguous. Commit `f42974b` moved the orphan check
   after the persisted binding and terminal-state checks and added a combined Publisher/PR receipt
   regression. All 255 tests passed; live preflight then returned
   `ready_recovery/sync_tracker_state` for the same WorkItem.
4. `issue-comment-receipt` found and bound the existing PR, created the one fixed status comment,
   and discarded that receipt. Preflight again required `sync_tracker_state`. One normal
   double-opt-in `ssh-run-once` returned `state_synchronized`, projected the Issue to
   `agent:review`, and left the subsequent preflight `idle`.

Each accepted stage made an Online Backup API snapshot before entering the sweep. The failed
pre-write Publisher attempt plus the three accepted fault stages left four retained backups; every
file was mode `0600` and passed `PRAGMA integrity_check`. The failed first attempt's backup was
retained rather than silently deleted.

### Independent final read-back

- SQLite passed `integrity_check` and contained exactly one WorkItem and one finished Turn for Issue
  `#4`. WorkItem state was `review`, PR binding was `5`, and both the stored publication SHA and Turn
  output SHA were the exact checkpoint.
- The only Codex session remained `01a015ae-1b9d-7ee3-957a-0a90b21629cd`; no second Turn or session
  was created. Recovery repeated only the exact checkpoint export needed after the ambiguous
  Publisher receipt; it did not PREPARE or START Codex again.
- GitHub contained exactly one open Draft PR for the deterministic branch and exactly one fixed
  Issue status comment. The Issue had `agent:review`, `priority:p1`, and `exec:ssh-cli`.
- The PR changed exactly `README.md`, with one insertion and one deletion; the fixed dispatcher
  marker was present in the only Issue comment.
- The task branch and PR head both resolved to the checkpoint SHA. `main` remained at its original
  SHA; no merge, deployment, release, tag, force-push, or ref deletion occurred.
- GitHub Actions run [`32160041932`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32160041932)
  was `completed/success` for workflow `fixture`, event `pull_request`, run attempt `1`, the exact
  task branch, and the exact checkpoint SHA.

The fine-grained PAT again denied the REST Checks endpoint with HTTP `403`; the permitted Actions
runs endpoint supplied the CI evidence. This run proves the live Publisher lost-receipt read-back
path and the Draft PR and Issue-comment recovery contracts. The narrower post-publication-record,
actual Dispatcher termination, and START-receipt recovery are recorded in the later Fixture
sections above. A physical SSH link/daemon interruption and Slack provider receipt loss remain
separate acceptance work.

## SSH CLI Dispatcher and Publisher fixture — 2026-08-18

This section records the first bounded write-enabled happy-path sweep. It is evidence for the
specific observed run, not authorization for unattended production use or evidence that live crash
recovery and Slack delivery have been proven.

### Admission and immutable identity

- Private Fixture Issue: [`longwdl/codex-dispatcher-fixture#2`](https://github.com/longwdl/codex-dispatcher-fixture/issues/2).
- Initial labels were `agent:ready`, `exec:ssh-cli`, and `priority:p1`.
- The strict task spec allowed only the README marker value to change to
  `ssh-publisher-phase-d-v1`; Codex was explicitly forbidden to push, create a PR, merge, or deploy.
- GitHub Issue node ID: `I_kwDOT3NfX88AAAABNPZJlw`.
- WorkItem: `wi_70da53f08c0e7d96a08901b8`.
- Task branch: `codex/issue-2-70da53f08c0e`.
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-2`.

Before the write, `ssh-preflight` selected exactly Issue `#2` as `ready_candidate`, reported no
rejections, and stated `authorizes_apply=false`. The task branch and matching PR did not exist;
Fixture `main` was `b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`. The existing SQLite database passed
`integrity_check`, and a mode-`0600` online backup preserved the earlier WorkItem/session/Turn
history before additive migrations or new state were written.

### One write-enabled sweep

The exact entry point required both `--apply` and
`CODEX_DISPATCHER_ENABLE_SSH_WRITES=1`. It returned success for one Turn:

- Turn: `turn_c4e2249059de4b139deff19867c68a27`, number `1`;
- Codex session: `01a01583-2133-7a82-bd40-62278e57bd0d`;
- Runner result: `finished/completed`;
- checkpoint: `071ec769c63b8ab594865611cdc8af46ddd07b7f`;
- resulting WorkItem state: `review`.

The Dispatcher then published only that exact checkpoint to the deterministic task branch, created
Draft PR [`#3`](https://github.com/longwdl/codex-dispatcher-fixture/pull/3), updated the one fixed
Issue status comment, and projected Issue `#2` to `agent:review`. The PR uses base `main`, the exact
task branch as head, and the same checkpoint SHA. SQLite binds PR number `3` and the published SHA
to the same WorkItem and contains exactly one finished Turn for this Issue.

### Independent read-back

- The remote task branch resolved to the checkpoint SHA; `main` remained at its original SHA.
- The PR diff contained only `README.md`, with one insertion and one deletion inside the allowed
  marker; the marker boundaries were unchanged.
- GitHub Actions run [`32155421239`](https://github.com/longwdl/codex-dispatcher-fixture/actions/runs/32155421239)
  was `completed/success` for workflow `fixture`, event `pull_request`, the exact task branch, and
  the exact checkpoint SHA.
- A following read-only `ssh-preflight` returned `idle` with no candidate or recovery action.
- No merge, deployment, release, main-branch update, Slack delivery, or second Codex Turn occurred.

### Second write-enabled sweep

After a new mode-`0600` online SQLite backup passed `integrity_check`, the same double-opt-in
`ssh-run-once` command was executed again. It completed in 3.36 seconds with `status=idle` and null
Issue, WorkItem, and Turn identifiers.

Independent before/after reads proved:

- SQLite remained at two WorkItems and four total Turns;
- Issue `#2` retained the same `review` WorkItem row, PR binding, published SHA, session ID, and
  `updated_at` value;
- it retained exactly one finished Turn with the same ID, number, result, checkpoint, and
  `updated_at` value;
- Issue labels, comment count, and `updated_at` value did not change;
- exactly one Draft PR remained, with the same `updated_at` value and head SHA;
- task-branch and `main` refs did not move;
- the Actions query still returned exactly the original successful workflow run;
- a final read-only `ssh-preflight` again returned `idle`.

This proves the completed happy-path is idle on an immediate repeated sweep. It does not by itself
prove the separate crash/lost-receipt recovery paths.

The fine-grained PAT could list Actions runs but could not read check runs through either the
GraphQL `statusCheckRollup` field or the REST Checks endpoint. CI success is therefore evidenced by
the accessible Actions workflow run, not inferred from those denied check APIs. The separate
controlled Publisher/PR/comment lost-receipt evidence is recorded above.

## SSH CLI Runner fixture — 2026-08-18

This section records the first real SSH CLI protocol run. The Fixture Issue remained open with
`agent:paused`, `exec:cloud`, and `priority:p2`; the test invoked the new protocol directly and did
not claim that end-to-end SSH scheduler label routing exists.

### Release and transport

- Current Runner release: `057226b185dfef70aa1b09bb54561b357f094743`.
- Fixed remote entrypoint: `/srv/codex-runner/bin/codex-runner-v1`.
- Fixed configuration selected Git `/usr/bin/git`, Codex CLI 0.147.0, shared
  `CODEX_HOME=/srv/codex-runner`, and `/srv/codex-runner/work-items`.
- The real `SshRunnerTransport` used the protected host key and identity plus the explicitly
  approved Mac Fixture `assh` proxy shape. An unknown STATUS request was definitively rejected.
- `codex login status` returned `Logged in using ChatGPT`; no API-key billing path was used.
- The release, wrapper, and configuration are user-owned in this Fixture because `ecs-user` has no
  passwordless sudo. Older releases remain available for symlink rollback. This is not the
  production ownership model.

### Migrated 1:1:1 binding and PREPARE

The legacy SQLite anchor, local mirror, GitHub `main`, and the exact remote task ref all agreed on
SHA `b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`. The existing branch was imported rather than
re-derived:

- WorkItem: `wi_3a97e3d99c30bdcbb50501dd`;
- GitHub Issue: `longwdl/codex-dispatcher-fixture#1`;
- task branch: `codex/issue-1-8e3775879000` with `task_branch_source=migrated`;
- Runner directory:
  `/srv/codex-runner/work-items/longwdl__codex-dispatcher-fixture/issue-1`.

Real PREPARE transferred a 4,804-byte self-contained Git bundle, verified its SHA-256, and created
one clean local repository with no remote. The repository branch and HEAD matched the migrated
binding and base SHA.

### START and resume evidence

All three Turns used the same WorkItem, branch, directory, input HEAD, and Codex session
`01a01486-0eee-7511-85ee-abe46c8bfb8b`:

1. Turn 1 created the session. Its Schema-valid Agent output used `status=blocked` with one question,
   conflicting with the stricter domain rule, so the Runner returned `agent_result_invalid` and no
   checkpoint.
2. Prompt and Schema descriptions were aligned without weakening the parser. Turn 2 resumed the
   exact session and produced a domain-valid final result in the rollout, but the bounded JSONL
   parser returned `codex_output_invalid`. No raw provider output was persisted or forwarded.
3. Static JSONL failure classification was added and deployed. Turn 3 again resumed the exact
   session; it completed with a parsed business result `blocked`, an unchanged output HEAD, and no
   error code. The blocker accurately reported that the isolated Runner cannot prove GitHub
   Timeline and dispatcher dry-run acceptance evidence.

Turn 2's JSONL anomaly did not reproduce in Turn 3, so it remains an observed transient rather
than a confirmed root cause. Future occurrences return a specific bounded JSONL error code.

Post-checks proved the worktree clean, no Git remote, the global lock acquirable, the GitHub task
branch unchanged at the base SHA, `last_published_sha` unset, and no Publisher, PR, Slack, merge, or
deployment action triggered by the task. The
[official Codex non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode)
confirms that `codex exec --json` emits JSONL, `--output-schema` constrains the final JSON, and an
explicit session ID can be used with `codex exec resume`.

## Historical Codex Cloud fixture — 2026-08-13

Snapshot date: 2026-08-13. This file records non-secret evidence from the dedicated private
fixture. It is not a substitute for repeatable automated tests.

## Repository initialization

- Source repository: `longwdl/codex_cloud_task_scheduler`, private, default branch `main`.
- Fixture repository: `longwdl/codex-dispatcher-fixture`, private, default branch `main`.
- Fixture baseline commit: `b877bdf801afbc1f6edcdca687c8aea8c0532a66`.
- Safe Markdown Issue template commit: `b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`.
- Fixture workflow run `31701519356`: completed successfully.
- All 13 `agent:*`, `exec:cloud`, and `priority:*` labels were present.

## Phase 2 admission contract

Fixture Issue `#1` was created with `agent:paused`, `exec:cloud`, and `priority:p2`.

1. With `agent:paused`, live `run-once --dry-run` returned no selected tasks.
2. Maintainer `longwdl` replaced `agent:paused` with `agent:ready`.
3. GitHub Timeline recorded `longwdl` as the Ready-label actor.
4. Live dry-run selected exactly Fixture Issue `#1`.
5. The Issue was restored to `agent:paused`; live dry-run again returned an empty queue.

No branch, pull request, Codex Cloud task, merge, or deployment was created by this test.

## Local task-branch recovery contract

The branch publication protocol is tested against a temporary local bare Git repository. The test
simulates a process failure after the remote branch is created but before SQLite advances the run,
then verifies that retry reuses the exact persisted branch and Base SHA without another write. A
second test advances `main` before recovery and verifies that the persisted Base SHA remains the
task-branch anchor. These tests do not access either configured GitHub repository.

## Phase 3 GitHub write primitive contract

Using the real adapter against Fixture Issue `#1`:

1. The adapter replaced the single state with `agent:dispatching` and verified the reread state.
2. It created one marker-based run comment and then updated the same comment idempotently.
3. It restored the Issue to `agent:paused` and verified the reread state.
4. The temporary test comment was deleted; no matching dispatcher test comment remains.

The test did not invoke Codex Cloud, create a branch, or create a pull request.

## Phase 3 task-branch publication contract

The recoverable branch service ran against Fixture Issue `#1` using deterministic run ID
`fixture-issue-1-branch-contract-v1`. It recorded Base SHA
`b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`, created branch
`codex/issue-1-8e3775879000` at that exact commit, then reran and reused the existing branch. The
persisted run ended in `branch_prepared`, `head_sha` equalled `base_sha`, and SQLite
`PRAGMA integrity_check` returned `ok`.

No Issue label/comment, pull request, Codex Cloud task, merge, or deployment was created. The branch
is intentionally retained as the stable input for the later Cloud submission contract test.

## Local Git workspace contract

The hardened Git workspace component cloned the private Fixture through HTTPS into a temporary
local mirror, fetched `main` without tags or recursive submodules, resolved base SHA
`b992e1e52c8f11ed2e6776f78ec20bb1667a8fb5`, and created local branch
`agent/local-read-only-contract` in an isolated worktree. No branch was pushed and no remote Git
reference changed.

## Accepted fixture risk

GitHub rulesets are unavailable for this private personal-account repository on the current plan.
The owner accepted that residual risk for this credential-free fixture only. This exception does
not apply to a production-connected repository.
